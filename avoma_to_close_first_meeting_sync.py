#!/usr/bin/env python3
"""
Avoma → Close first-meeting analysis sync (Custom Activity edition).

Avoma rebuild of attention_to_close_first_meeting_sync.py. Captures first
sales calls and writes them to a Custom Activity, plus updates the lead-
level First Call Show Up / Qualified fields (honoring their override
fields, same as the Attention build).

Filter (must satisfy ALL):
  - Subject contains "vendingpren" (the first-sale marker)
  - Subject does NOT contain any FIRST_SALE_EXCLUSION_KEYWORDS
  This mirrors avoma_to_close_meeting_sync.py's is_first_sale_title()
  exactly — keep the two in sync.

============================================================================
READ THIS FIRST — same assumption set as avoma_to_close_meeting_sync.py
(full write-up in that file's docstring and in avoma-migration-rebuild-
plan.md in the project docs):

1. CLOSE-SIDE NAMING — CUSTOM_ACTIVITY_TYPE_NAME / CLOSE_FIELD_NAMES below
   default to reusing the existing Attention CA type/field names.
2. NOTES → FIELD MAPPING — Pain Points ≈ Doubt, Key Takeaways ≈ Call
   Summary. UNVERIFIED against a real sales call.
3. QA SCORE SHAPE — extract_qa_score()'s field-name guesses are unverified;
   no live scorecard has scored a real call yet.
4. ATTENDANCE / SHOW-UP — derive_show_value() is a duration + speaker-
   talk-time heuristic, not a confirmed Avoma field.
5. QUALIFIED DERIVATION — derive_qualified_value() reads Avoma's native
   `outcome` field, which was confirmed NULL on every real meeting so far.
   Will not populate anything until reps/setters tag outcomes in Avoma.
6. AVOMA WEB LINK FORMAT — assumed https://app.avoma.com/meetings/{uuid}.
============================================================================

Required GitHub secrets:
  CLOSE_API_KEY         Close API key (Basic auth)
  AVOMA_API_KEY         Avoma org API key (Bearer auth)
  ANTHROPIC_API_KEY     Anthropic API key (for Claude Haiku enrichment)

Optional env vars:
  HOURS_BACK            Window of Avoma meetings to consider (default: 24)
  DRY_RUN                If "1", log payloads without writing to Close
"""

import os
import sys
import re
import time
import json
import base64
import requests
from datetime import datetime, timezone, timedelta

# ===== Config =====
CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]
AVOMA_API_KEY = os.environ["AVOMA_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
HOURS_BACK = int(os.environ.get("HOURS_BACK", "24"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

CLOSE_API_BASE = "https://api.close.com/api/v1"
AVOMA_API_BASE = "https://api.avoma.com/v1"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

INTERNAL_DOMAIN = "@modern-amenities.com"

# ---- Close-side naming (see assumption #1) ----
CUSTOM_ACTIVITY_TYPE_NAME = "Attention - First Meeting Analysis"
CLOSE_FIELD_NAMES = {
    "call_link": "Attention Call Link",
    "call_id": "Attention Call ID",
    "call_title": "Attention Call Title",
    "qa_score": "QA Score",
    "primary_objection": "Primary Objection",
    "key_concern": "Key Concern",
    "lost_reason": "Lost Reason",
    "call_summary": "Call Summary",
    "call_duration": "Call Duration",
}

FIRST_SALE_EXCLUSION_KEYWORDS = (
    "quick discovery",
    "discovery call",
    "setter",
    "follow-up",
    "follow up",
    "rescheduled",
    "reschedule",
    "next steps",
)
FIRST_SALE_TITLE_MARKER = "vendingpren"

OBJECTION_CHOICES = ("Timing", "Investment", "Fit", "Other")
LOSS_OUTCOME_MARKERS = ("disqualified", "lost", "not interested", "closed lost")

# Lead-level field IDs — unchanged from the Attention build (Close side,
# untouched by the vendor swap).
FIRST_CALL_SHOW_FIELD = "cf_OPyvpU45RdvjLqfm8V1VWwNxrGKogEH2IBJmfCj0Uhq"
FIRST_CALL_SHOW_OVERRIDE_FIELD = "cf_CJMktLJShTyA86PdBqNUP59ZfJh0WpdB1tEt76Y3HEy"
QUALIFIED_FIELD = "cf_ZDx7NBQaDzV1yYrFcBMzt6cIYj81dAcswpNN0CQzCPS"
QUALIFIED_OVERRIDE_FIELD = "cf_nizevVbDT00CqdjfqQY9NSiBvCtuRl1mT1VxQie6zpc"

# ---- Notes category mapping (see assumption #2) ----
NOTES_CATEGORY_ALIASES = {
    "participants": "Participants",
    "key takeaways": "Key Takeaways",
    "action items": "Action Items",
    "follow-up meeting": "Follow-up Meeting",
    "followup meeting": "Follow-up Meeting",
    "pain points": "Pain Points",
    "features interested": "Features Interested",
    "positive moments": "Positive Moments",
    "timeline": "Timeline",
}
DOUBT_ANALOG_CATEGORY = "Pain Points"
CALL_SUMMARY_ANALOG_CATEGORY = "Key Takeaways"
DEAL_SUMMARY_ANALOG_CATEGORY = "Timeline"

CLOSE_REQUEST_DELAY = 0.5
AVOMA_REQUEST_DELAY = 0.2

_close_auth_b64 = base64.b64encode(f"{CLOSE_API_KEY}:".encode()).decode()
CLOSE_HEADERS = {"Authorization": f"Basic {_close_auth_b64}"}
AVOMA_HEADERS = {
    "Authorization": f"Bearer {AVOMA_API_KEY}",
    "Content-Type": "application/json",
}
ANTHROPIC_HEADERS = {
    "x-api-key": ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "Content-Type": "application/json",
}


# ===== Logging =====
def log(msg, indent=0):
    print(f"{'  ' * indent}{msg}", flush=True)


def section(label):
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}", flush=True)


# ===== Text helpers =====
def normalize_field_name(name):
    return re.sub(r"^[^a-zA-Z]+", "", name).strip()


def clean_title(title):
    if not title:
        return title
    return re.sub(
        r"\s*-\s*\d{4}[_\-]\d{2}[_\-]\d{2}[\s_]\d{2}[_\-]\d{2}.*$",
        "",
        title,
    ).strip()


def html_wrap(text):
    if not text:
        return text
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    paragraphs = [p for p in escaped.split("\n\n") if p.strip()]
    if not paragraphs:
        return f"<body><p>{escaped}</p></body>"
    inner = "".join(
        f"<p>{p.replace(chr(10), '<br/>')}</p>" for p in paragraphs
    )
    return f"<body>{inner}</body>"


# ===== Close API =====
def close_get(path, params=None):
    url = path if path.startswith("http") else f"{CLOSE_API_BASE}{path}"
    for attempt in range(6):
        resp = requests.get(url, headers=CLOSE_HEADERS, params=params)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            log(f"[Close] 429 rate limited, waiting {wait}s...", indent=1)
            time.sleep(wait)
            continue
        time.sleep(CLOSE_REQUEST_DELAY)
        return resp
    raise Exception(f"Close GET {path} exhausted retries")


def close_post(path, json_data):
    url = path if path.startswith("http") else f"{CLOSE_API_BASE}{path}"
    headers = {**CLOSE_HEADERS, "Content-Type": "application/json"}
    for attempt in range(6):
        resp = requests.post(url, headers=headers, json=json_data)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            time.sleep(wait)
            continue
        time.sleep(CLOSE_REQUEST_DELAY)
        return resp
    raise Exception(f"Close POST {path} exhausted retries")


def close_put(path, json_data):
    url = path if path.startswith("http") else f"{CLOSE_API_BASE}{path}"
    headers = {**CLOSE_HEADERS, "Content-Type": "application/json"}
    for attempt in range(6):
        resp = requests.put(url, headers=headers, json=json_data)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            time.sleep(wait)
            continue
        time.sleep(CLOSE_REQUEST_DELAY)
        return resp
    raise Exception(f"Close PUT {path} exhausted retries")


# ===== Avoma API =====
def avoma_get(url, params=None):
    full_url = url if url.startswith("http") else f"{AVOMA_API_BASE}{url}"
    for attempt in range(6):
        resp = requests.get(full_url, headers=AVOMA_HEADERS, params=params, timeout=60)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            log(f"[Avoma] 429 rate limited, waiting {wait}s...", indent=1)
            time.sleep(wait)
            continue
        if resp.status_code in (502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        time.sleep(AVOMA_REQUEST_DELAY)
        return resp
    raise Exception(f"Avoma GET {url} exhausted retries")


def avoma_list_meetings(since_dt, until_dt):
    """CONFIRMED 2026-08-28: from_date/to_date required, ISO8601 UTC."""
    params = {
        "from_date": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to_date": until_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_size": 100,
    }
    log(f"Fetching Avoma meetings {params['from_date']} → {params['to_date']}...")

    meetings = []
    url = "/meetings/"
    next_params = params
    for _ in range(500):
        resp = avoma_get(url, params=next_params)
        if not resp.ok:
            raise Exception(f"Avoma meetings list returned {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        meetings.extend(body.get("results", []))
        next_url = body.get("next")
        if not next_url:
            break
        url = next_url
        next_params = None
    return meetings


def avoma_get_scorecard_evaluations(meeting_uuid):
    resp = avoma_get("/scorecard_evaluations/", params={"meeting_uuid": meeting_uuid})
    if not resp.ok:
        return []
    return resp.json().get("results", [])


def avoma_get_notes(meeting_uuid):
    resp = avoma_get("/notes/", params={"meeting_uuid": meeting_uuid})
    if not resp.ok:
        return None
    return resp.json()


def avoma_get_meeting_segments(meeting_uuid):
    resp = avoma_get("/meeting_segments/", params={"uuid": meeting_uuid})
    if not resp.ok:
        return None
    return resp.json()


# ===== Avoma notes parsing (assumption #2) =====
def _slate_node_text(node):
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_slate_node_text(n) for n in node)
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            return node["text"]
        children = node.get("children")
        if children:
            return _slate_node_text(children)
    return ""


def parse_avoma_notes(notes_response):
    if not notes_response:
        return {}

    items = notes_response
    if isinstance(notes_response, dict):
        items = (
            notes_response.get("results")
            or notes_response.get("data")
            or notes_response.get("notes")
            or []
        )
        if isinstance(items, dict):
            items = [{"category": k, "content": v} for k, v in items.items()]

    out = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        header = None
        for key in ("category", "header", "title", "name", "label", "type"):
            if item.get(key):
                header = str(item[key]).strip()
                break
        if not header:
            continue
        content = None
        for key in ("content", "children", "value", "body", "text", "notes"):
            if key in item:
                content = item[key]
                break
        text = _slate_node_text(content).strip()
        if not text:
            continue
        canonical = NOTES_CATEGORY_ALIASES.get(header.lower(), header)
        out[canonical] = (out[canonical] + "\n\n" + text).strip() if canonical in out else text
    return out


def get_note_value(notes_dict, category_name):
    return notes_dict.get(category_name, "")


def extract_qa_score(evaluations):
    """UNVERIFIED SHAPE (see assumption #3)."""
    if not evaluations:
        return None
    ev = evaluations[0]
    for path in (
        ("average_score",),
        ("score",),
        ("total_score",),
        ("overall_score",),
        ("summary", "averageScore"),
        ("summary", "average_score"),
    ):
        node = ev
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                node = None
                break
        if node is not None:
            return node
    return None


# ===== Title filter =====
def is_first_sale_title(title):
    if not title:
        return False
    lower = clean_title(title).lower()
    if FIRST_SALE_TITLE_MARKER not in lower:
        return False
    if any(kw in lower for kw in FIRST_SALE_EXCLUSION_KEYWORDS):
        return False
    return True


# ===== Avoma data extraction =====
def get_prospect_email(meeting):
    """UNVERIFIED against the meetings endpoint — see the sibling meeting
    sync's docstring for the same caveat."""
    attendees = meeting.get("attendees") or meeting.get("participants") or []
    for p in attendees:
        if not isinstance(p, dict):
            continue
        email = (p.get("email") or "").lower()
        if not email:
            continue
        if "is_rep" in p:
            if not p["is_rep"]:
                return email
            continue
        if INTERNAL_DOMAIN not in email:
            return email
    return None


def extract_prospect_name_from_title(title):
    """
    First-sale titles follow the pattern "<Name> and Vendingpreneur(s)
    Consultation". Unchanged from the Attention build — this is a title
    convention reps/calendar invites use, not an Avoma API detail, so it
    should transfer as-is as long as reps keep naming meetings the same way.
    """
    if not title:
        return None
    lower = clean_title(title).lower()
    for separator in (" and vending", "and vendingpren"):
        idx = lower.find(separator)
        if idx > 0:
            name = title[:idx].strip()
            if len(name) > 3:
                return name
    return None


def derive_show_value(meeting, segments):
    """BEST-EFFORT / UNVERIFIED (see assumption #4)."""
    speaker_segments = (segments or {}).get("speaker_segments") if segments else None
    if isinstance(speaker_segments, dict) and speaker_segments:
        attendees = meeting.get("attendees") or meeting.get("participants") or []
        internal_emails = {
            (p.get("email") or "").lower()
            for p in attendees
            if isinstance(p, dict) and (p.get("is_rep") or INTERNAL_DOMAIN in (p.get("email") or "").lower())
        }
        for speaker_key, ranges in speaker_segments.items():
            if not ranges:
                continue
            if str(speaker_key).lower() in internal_emails:
                continue
            return "Yes"
        return "No"

    duration = meeting.get("duration") or 0
    if duration > 60:
        return "Yes"
    if duration == 0:
        return "No"
    return None


def derive_qualified_value(outcome_label):
    """
    Maps Avoma's native `outcome` field to 'Yes'/'No' for the Qualified
    field. UNVERIFIED (assumption #5) — outcome is null on every real
    meeting pulled so far, so this will not fire until reps/setters start
    tagging outcomes in Avoma (via "Set meeting outcome" or the UI).
    """
    if not outcome_label:
        return None
    lower = outcome_label.lower()
    if "disqualified" in lower:
        return "No"
    if "one call close" in lower:
        return "Yes"
    if "qualified" in lower:
        return "Yes"
    if "lost" in lower or "not interested" in lower:
        return "No"
    return None


def get_lead_overrides(lead_id):
    fields = (
        f"id,"
        f"custom.{FIRST_CALL_SHOW_OVERRIDE_FIELD},"
        f"custom.{QUALIFIED_OVERRIDE_FIELD},"
        f"custom.{QUALIFIED_FIELD}"
    )
    resp = close_get(f"/lead/{lead_id}/", params={"_fields": fields})
    if not resp.ok:
        return {"show_override": None, "qualified_override": None, "qualified_current": None}
    data = resp.json()
    return {
        "show_override": data.get(f"custom.{FIRST_CALL_SHOW_OVERRIDE_FIELD}"),
        "qualified_override": data.get(f"custom.{QUALIFIED_OVERRIDE_FIELD}"),
        "qualified_current": data.get(f"custom.{QUALIFIED_FIELD}"),
    }


def update_lead_show_and_qualified(lead_id, meeting, segments, outcome_label):
    show_value = derive_show_value(meeting, segments)
    qualified_value = derive_qualified_value(outcome_label)

    if show_value is None and qualified_value is None:
        log("No interpretable attendance/outcome signal; skipping lead update", indent=1)
        return {}

    overrides = get_lead_overrides(lead_id)
    payload = {}

    if show_value is not None:
        if (overrides["show_override"] or "").lower() == "yes":
            log("First Call Show Up Override is 'Yes' — leaving field untouched", indent=1)
        else:
            payload[f"custom.{FIRST_CALL_SHOW_FIELD}"] = show_value

    if qualified_value is not None:
        if (overrides["qualified_override"] or "").lower() == "yes":
            log("Qualified Override is 'Yes' — leaving field untouched", indent=1)
        elif overrides["qualified_current"]:
            log(
                f"Qualified already set to {overrides['qualified_current']!r} — leaving field untouched (rep judgment wins)",
                indent=1,
            )
        else:
            payload[f"custom.{QUALIFIED_FIELD}"] = qualified_value

    if not payload:
        return {}

    if DRY_RUN:
        log(f"DRY_RUN — would PUT lead {lead_id} with: {payload}", indent=1)
        return payload

    resp = close_put(f"/lead/{lead_id}/", payload)
    if not resp.ok:
        log(f"⚠️  Failed to update lead {lead_id}: {resp.status_code}: {resp.text[:300]}", indent=1)
        return {}

    friendly = {}
    for k, v in payload.items():
        if k == f"custom.{FIRST_CALL_SHOW_FIELD}":
            friendly["First Call Show Up"] = v
        elif k == f"custom.{QUALIFIED_FIELD}":
            friendly["Qualified"] = v
    return friendly


# ===== Anthropic (Claude Haiku) — unchanged =====
def haiku_classify_objection(doubt_text):
    if not doubt_text or len(doubt_text.strip()) < 20:
        return "Other"
    prompt = f"""Classify the prospect's primary objection from this sales call into EXACTLY ONE category:

- Timing: Not ready yet, busy season, want to wait, need more time
- Investment: Cost, budget, financing, can't afford, too expensive
- Fit: Wrong product/service for them, doesn't match their needs, unsuitable
- Other: Anything not matching the above

Objection text:
{doubt_text[:3000]}

Respond with ONLY ONE WORD: Timing, Investment, Fit, or Other."""
    payload = {"model": HAIKU_MODEL, "max_tokens": 10, "messages": [{"role": "user", "content": prompt}]}
    resp = requests.post(ANTHROPIC_API_URL, headers=ANTHROPIC_HEADERS, json=payload)
    if not resp.ok:
        log(f"Haiku classify failed: {resp.status_code}: {resp.text[:300]}", indent=2)
        return "Other"
    answer = resp.json()["content"][0]["text"].strip()
    for valid in OBJECTION_CHOICES:
        if valid.lower() in answer.lower():
            return valid
    return "Other"


def haiku_summarize_concern(doubt_text):
    if not doubt_text or len(doubt_text.strip()) < 20:
        return ""
    prompt = f"""Summarize the prospect's biggest concern from this sales call in 20 words or fewer. Be specific about what they actually doubt or worry about. Do not editorialize.

Doubt text:
{doubt_text[:3000]}

Respond with ONLY the summary, no preamble."""
    payload = {"model": HAIKU_MODEL, "max_tokens": 60, "messages": [{"role": "user", "content": prompt}]}
    resp = requests.post(ANTHROPIC_API_URL, headers=ANTHROPIC_HEADERS, json=payload)
    if not resp.ok:
        log(f"Haiku summarize failed: {resp.status_code}: {resp.text[:300]}", indent=2)
        return ""
    return resp.json()["content"][0]["text"].strip()


def is_lost_outcome(outcome_label):
    if not outcome_label:
        return False
    lower = outcome_label.lower()
    return any(marker in lower for marker in LOSS_OUTCOME_MARKERS)


def haiku_summarize_lost_reason(deal_summary, call_summary, doubt_text):
    context = "\n\n".join(s for s in (deal_summary, call_summary, doubt_text) if s)
    if not context.strip() or len(context.strip()) < 20:
        return ""
    prompt = f"""This sales call ended with the prospect NOT moving forward with the deal. Summarize the specific reason the deal was lost in 20 words or fewer. Be concrete about what actually killed it (e.g. competing solution, price, timing they can't change, fit issue). Do not editorialize or speculate.

Call context:
{context[:5000]}

Respond with ONLY the summary, no preamble. If the loss reason is unclear from the context, respond with an empty string."""
    payload = {"model": HAIKU_MODEL, "max_tokens": 60, "messages": [{"role": "user", "content": prompt}]}
    resp = requests.post(ANTHROPIC_API_URL, headers=ANTHROPIC_HEADERS, json=payload)
    if not resp.ok:
        log(f"Haiku lost-reason failed: {resp.status_code}: {resp.text[:300]}", indent=2)
        return ""
    return resp.json()["content"][0]["text"].strip()


# ===== Custom Activity Type resolution =====
def find_custom_activity_type():
    resp = close_get("/custom_activity/")
    if not resp.ok:
        raise Exception(f"Could not list custom activity types: {resp.status_code}: {resp.text[:300]}")

    for activity_type in resp.json().get("data", []):
        if activity_type.get("name") == CUSTOM_ACTIVITY_TYPE_NAME:
            type_id = activity_type["id"]
            fields_list = (
                activity_type.get("fields")
                or activity_type.get("custom_fields")
                or activity_type.get("field_definitions")
                or []
            )
            field_ids = {}
            for field in fields_list:
                normalized = normalize_field_name(field.get("name", ""))
                if normalized:
                    field_ids[normalized] = field["id"]
            return {"id": type_id, "fields": field_ids}

    raise Exception(
        f"Custom Activity Type '{CUSTOM_ACTIVITY_TYPE_NAME}' not found in Close. "
        f"Verify it exists at Settings → Custom Activities."
    )


# ===== Close lead matching =====
def find_close_lead_by_email(email):
    if not email:
        return None
    resp = close_get(
        "/lead/",
        params={"query": f"email_address:{email}", "_fields": "id,display_name,contacts", "_limit": 5},
    )
    if not resp.ok:
        return None
    leads = resp.json().get("data", [])
    return leads[0] if leads else None


def find_close_lead_by_title(title):
    name = extract_prospect_name_from_title(title)
    if not name:
        return None
    resp = close_get(
        "/lead/",
        params={"query": name, "_fields": "id,display_name,contacts", "_limit": 5},
    )
    if not resp.ok:
        return None
    leads = resp.json().get("data", [])
    if not leads:
        return None
    name_lower = name.lower()
    for lead in leads:
        if name_lower in (lead.get("display_name") or "").lower():
            return lead
    if len(leads) == 1:
        return leads[0]
    return None


# ===== Idempotency =====
def custom_activity_already_exists(lead_id, type_id, avoma_meeting_uuid, call_id_field_id):
    resp = close_get(
        "/activity/custom/",
        params={"lead_id": lead_id, "custom_activity_type_id": type_id},
    )
    if not resp.ok:
        return False
    for activity in resp.json().get("data", []):
        if activity.get(f"custom.{call_id_field_id}") == avoma_meeting_uuid:
            return True
    return False


# ===== Enrichment =====
def process_meeting(meeting, type_info):
    uuid = meeting.get("uuid", "")
    title = meeting.get("subject", "")

    log(f"\n[{uuid}] '{title}'")

    # 1. Title filter — keep only first sales calls
    if not is_first_sale_title(title):
        log("→ Not a first sales call (handled by another sync), skip", indent=1)
        return ("skipped", "title-filter")

    # 2. Require completed analysis
    evaluations = avoma_get_scorecard_evaluations(uuid)
    notes_raw = avoma_get_notes(uuid)
    notes = parse_avoma_notes(notes_raw)
    if not evaluations and not notes:
        log("→ Avoma analysis not yet complete (no scorecard evaluations, no notes), skip", indent=1)
        return ("skipped", "not-analyzed")
    if not notes:
        log("Note: no parsed notes (likely setter-style scorecard or unrecognized notes shape); proceeding with scorecard-only fields", indent=1)

    # 3. Resolve Close lead
    prospect_email = get_prospect_email(meeting)
    matched_lead = None
    match_method = None
    if prospect_email:
        matched_lead = find_close_lead_by_email(prospect_email)
        if matched_lead:
            match_method = f"email ({prospect_email})"
    if not matched_lead:
        matched_lead = find_close_lead_by_title(title)
        if matched_lead:
            extracted = extract_prospect_name_from_title(title)
            match_method = f"title-name fallback ('{extracted}')"
    if not matched_lead:
        log(f"→ No Close lead found (email={prospect_email or 'none'}, title-extract failed), skip", indent=1)
        return ("skipped", "no-match")

    lead_id = matched_lead["id"]
    lead_name = matched_lead.get("display_name", "Unknown")
    log(f"Matched lead: {lead_name} ({lead_id}) via {match_method}", indent=1)

    # 4. Idempotency
    field_ids = type_info["fields"]
    call_id_field_id = field_ids.get(CLOSE_FIELD_NAMES["call_id"])
    if not call_id_field_id:
        log(f"→ '{CLOSE_FIELD_NAMES['call_id']}' field not found in Custom Activity Type, abort", indent=1)
        return ("skipped", "missing-field")
    if custom_activity_already_exists(lead_id, type_info["id"], uuid, call_id_field_id):
        log("→ Custom Activity already exists for this meeting, skip", indent=1)
        return ("skipped", "duplicate")

    # 5. Pull analysis fields
    qa_score = extract_qa_score(evaluations)
    doubt_text = get_note_value(notes, DOUBT_ANALOG_CATEGORY)
    call_summary = get_note_value(notes, CALL_SUMMARY_ANALOG_CATEGORY)

    # 6. Haiku enrichment
    log("Classifying Primary Objection (Haiku)...", indent=1)
    primary_objection = haiku_classify_objection(doubt_text)
    log(f"→ {primary_objection}", indent=2)

    log("Summarizing Key Concern (Haiku)...", indent=1)
    key_concern = haiku_summarize_concern(doubt_text)
    log(f"→ {key_concern[:120]}", indent=2)

    outcome_label = meeting.get("outcome") or ""
    log(f"Outcome: {outcome_label!r}", indent=1)
    lost_reason = ""
    if is_lost_outcome(outcome_label):
        log("→ Indicates loss; summarizing Lost Reason (Haiku)...", indent=2)
        deal_summary = get_note_value(notes, DEAL_SUMMARY_ANALOG_CATEGORY)
        lost_reason = haiku_summarize_lost_reason(deal_summary, call_summary, doubt_text)
        log(f"→ {lost_reason[:120]}", indent=2)

    # 7. Build payload
    avoma_link = f"https://app.avoma.com/meetings/{uuid}"  # ASSUMPTION — unconfirmed URL format
    field_mapping = {
        CLOSE_FIELD_NAMES["call_link"]: avoma_link,
        CLOSE_FIELD_NAMES["call_id"]: uuid,
        CLOSE_FIELD_NAMES["call_title"]: clean_title(title),
        CLOSE_FIELD_NAMES["qa_score"]: qa_score,
        CLOSE_FIELD_NAMES["primary_objection"]: primary_objection,
        CLOSE_FIELD_NAMES["key_concern"]: html_wrap(key_concern),
        CLOSE_FIELD_NAMES["lost_reason"]: html_wrap(lost_reason),
        CLOSE_FIELD_NAMES["call_summary"]: html_wrap(call_summary),
        CLOSE_FIELD_NAMES["call_duration"]: meeting.get("duration"),
    }

    payload = {"custom_activity_type_id": type_info["id"], "lead_id": lead_id}
    for name, value in field_mapping.items():
        if name not in field_ids:
            log(f"⚠️  Field '{name}' not present in Custom Activity Type; skipping that field", indent=1)
            continue
        if value is None or value == "":
            continue
        payload[f"custom.{field_ids[name]}"] = value

    if DRY_RUN:
        log("DRY_RUN — would POST payload:", indent=1)
        log(json.dumps(payload, indent=2)[:1500], indent=2)
        segments = avoma_get_meeting_segments(uuid)
        update_lead_show_and_qualified(lead_id, meeting, segments, outcome_label)
        return ("skipped", "dry-run")

    resp = close_post("/activity/custom/", payload)
    if not resp.ok:
        raise Exception(f"Failed to create Custom Activity: {resp.status_code}: {resp.text[:500]}")

    activity_id = resp.json().get("id")
    log(f"✅ Created Custom Activity {activity_id} on lead '{lead_name}'", indent=1)

    segments = avoma_get_meeting_segments(uuid)
    updates = update_lead_show_and_qualified(lead_id, meeting, segments, outcome_label)
    if updates:
        log(f"Updated lead fields: {updates}", indent=1)

    return ("enriched", activity_id)


# ===== Main =====
def main():
    section(f"Avoma → Close first-meeting sync (HOURS_BACK={HOURS_BACK}, DRY_RUN={DRY_RUN})")

    section("Resolving Close Custom Activity Type")
    type_info = find_custom_activity_type()
    log(f"Type:   {CUSTOM_ACTIVITY_TYPE_NAME}")
    log(f"ID:     {type_info['id']}")
    log(f"Fields ({len(type_info['fields'])}):")
    for name, field_id in sorted(type_info["fields"].items()):
        log(f"  {name}: {field_id}", indent=1)

    until_dt = datetime.now(timezone.utc)
    since_dt = until_dt - timedelta(hours=HOURS_BACK)
    section(f"Fetching Avoma meetings since {since_dt.isoformat()}")
    meetings = avoma_list_meetings(since_dt, until_dt)
    log(f"Total returned: {len(meetings)}")

    section("Processing meetings")
    stats = {"enriched": 0, "skipped": 0, "failed": 0}
    skip_reasons = {}

    for meeting in meetings:
        try:
            outcome, detail = process_meeting(meeting, type_info)
            if outcome == "enriched":
                stats["enriched"] += 1
            else:
                stats["skipped"] += 1
                skip_reasons[detail] = skip_reasons.get(detail, 0) + 1
        except Exception as e:
            stats["failed"] += 1
            meeting_id = meeting.get("uuid", "?")
            log(f"❌ Error processing {meeting_id}: {e}", indent=1)

    section("Done")
    log(f"Enriched: {stats['enriched']}")
    log(f"Skipped:  {stats['skipped']}")
    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        log(f"  ({reason}: {count})", indent=1)
    log(f"Failed:   {stats['failed']}")

    sys.exit(0 if stats["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
