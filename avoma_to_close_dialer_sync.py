#!/usr/bin/env python3
"""
Avoma → Close dialer call enrichment.

Avoma rebuild of attention_to_close_dialer_sync_1.py. For each recent Close
dialer call (recording_url + duration ≥ MIN_DURATION):
  1. Look up the Avoma call resource by external_id (= Close call activity
     ID, set when the Close→Avoma upload step POSTs to /v1/calls/)
  2. Resolve the linked meeting and, if analysis is complete, create a
     Custom Activity on the matched Close lead with all analysis fields
     populated.

WHY THIS IS SIMPLER THAN THE ATTENTION VERSION:
The Attention build had to "invert the loop" — iterate Close call IDs and
ask Attention "do you have a conversation for this one?" — because
Attention's API stored applicationExternalID on import but never returned
it in list/GET responses, so there was no reliable reverse lookup.

Avoma's `POST /v1/calls/` (used by the Close→Avoma upload step, not in
this script) sets `external_id` directly on a first-class "calls" resource,
and per the rebuild plan `GET /v1/calls/{external_id}/` is a direct lookup
key — no workaround needed. Also: dialer-originated meetings carry a
native `is_call: true` flag, replacing the old "Close Dialer Call" title
string that close_to_attention_sync.py used to bake into imported titles.

============================================================================
READ THIS FIRST — assumptions pending Stephen's confirmation (full write-up
in avoma-migration-rebuild-plan.md in the project docs):

1. `GET /v1/calls/{external_id}/` RESPONSE SHAPE — UNCONFIRMED. `POST
   /v1/calls/` was tested end-to-end 2026-08-28 and returned at least
   {"state": "created", ...} while auto-creating a linked meeting (verified
   separately via GET /v1/meetings/ showing is_call: true). The GET-by-id
   lookup itself has not been directly captured. extract_meeting_uuid()
   below defensively checks several plausible keys
   (meeting_uuid/uuid/conversation_uuid, or a nested "meeting" object) —
   confirm against a real response and simplify once known.

2. CLOSE-SIDE NAMING — CUSTOM_ACTIVITY_TYPE_NAME / CLOSE_FIELD_NAMES below
   default to reusing the existing "Attention - Close Dialer Call
   Analysis" CA type/fields.

3. NOTES → FIELD MAPPING — Pain Points ≈ Doubt, Key Takeaways ≈ Call
   Summary. UNVERIFIED against a real sales call.

4. QA SCORE SHAPE — extract_qa_score()'s field-name guesses are unverified.

5. AVOMA WEB LINK FORMAT — assumed https://app.avoma.com/meetings/{uuid}.
============================================================================

Required GitHub secrets:
  CLOSE_API_KEY         Close API key (Basic auth)
  AVOMA_API_KEY         Avoma org API key (Bearer auth)
  ANTHROPIC_API_KEY     Anthropic API key (for Claude Haiku enrichment)

Optional env vars:
  HOURS_BACK            Window of recent Close calls to consider (default: 24)
  MIN_DURATION          Skip Close calls shorter than this in seconds (default: 180)
  DRY_RUN                If "1", log payloads without writing to Close
"""

import os
import sys
import time
import json
import base64
import re
import requests
from datetime import datetime, timezone, timedelta

# ===== Config =====
CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]
AVOMA_API_KEY = os.environ["AVOMA_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
HOURS_BACK = int(os.environ.get("HOURS_BACK", "24"))
MIN_DURATION = int(os.environ.get("MIN_DURATION", "180"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

CLOSE_API_BASE = "https://api.close.com/api/v1"
AVOMA_API_BASE = "https://api.avoma.com/v1"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ---- Close-side naming (see assumption #2) ----
CUSTOM_ACTIVITY_TYPE_NAME = "Attention - Close Dialer Call Analysis"
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
    "close_call_activity_id": "Close Call Activity ID",
}

OBJECTION_CHOICES = ("Timing", "Investment", "Fit", "Other")
LOSS_OUTCOME_MARKERS = ("disqualified", "lost", "not interested", "closed lost")

# ---- Notes category mapping (see assumption #3) ----
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


def normalize_field_name(name):
    return re.sub(r"^[^a-zA-Z]+", "", name).strip()


def html_wrap(text):
    """Unchanged from the Attention build — see the original dialer sync's
    docstring for the debugging history of Close's XHTML Textarea format."""
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


def avoma_get_call_by_external_id(external_id):
    """
    UNCONFIRMED RESPONSE SHAPE — see assumption #1. Looks up the Avoma
    "calls" resource created by the Close→Avoma upload step's
    POST /v1/calls/ (external_id = Close call activity ID).
    """
    resp = avoma_get(f"/calls/{external_id}/")
    if resp.status_code == 404:
        return None
    if not resp.ok:
        log(f"⚠️  Avoma calls lookup failed for {external_id}: {resp.status_code}: {resp.text[:200]}", indent=1)
        return None
    return resp.json()


def extract_meeting_uuid(call_obj):
    """Defensive extraction of the linked meeting's UUID — see assumption #1."""
    if not call_obj:
        return None
    for key in ("meeting_uuid", "uuid", "conversation_uuid"):
        if call_obj.get(key):
            return call_obj[key]
    nested = call_obj.get("meeting")
    if isinstance(nested, dict):
        return nested.get("uuid") or nested.get("id")
    return None


def avoma_get_meeting(meeting_uuid):
    resp = avoma_get(f"/meetings/{meeting_uuid}/")
    if not resp.ok:
        log(f"⚠️  Could not fetch meeting {meeting_uuid}: {resp.status_code}: {resp.text[:200]}", indent=1)
        return None
    return resp.json()


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


# ===== Avoma notes parsing (assumption #3) =====
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
    """UNVERIFIED SHAPE (see assumption #4)."""
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

    data = resp.json()
    for activity_type in data.get("data", []):
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
                raw_name = field.get("name", "")
                normalized = normalize_field_name(raw_name)
                if not normalized:
                    continue
                field_ids[normalized] = field["id"]

            if not field_ids:
                log("WARNING: no fields found in Custom Activity Type response.", indent=1)
                log(f"Top-level keys returned: {list(activity_type.keys())}", indent=1)
                log("Sample (first 2000 chars):", indent=1)
                log(json.dumps(activity_type, indent=2)[:2000], indent=2)

            return {"id": type_id, "fields": field_ids}

    raise Exception(
        f"Custom Activity Type '{CUSTOM_ACTIVITY_TYPE_NAME}' not found in Close. "
        f"Verify it exists at Settings → Custom Activities."
    )


# ===== Close call iteration (unchanged — still Close API) =====
def find_recent_close_calls(since_dt):
    eligible = []
    inspected = 0
    skipped_short = 0
    skipped_no_recording = 0
    skipped_no_lead = 0

    skip = 0
    pages = 0
    max_pages = 200
    while pages < max_pages:
        resp = close_get("/activity/call/", params={"_skip": skip, "_limit": 100})
        if not resp.ok:
            raise Exception(f"Close /activity/call/ returned {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        items = data.get("data", [])
        if not items:
            break

        window_ended = False
        for call in items:
            inspected += 1
            date_str = call.get("date_created", "")
            try:
                call_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if call_dt < since_dt:
                window_ended = True
                break

            if not call.get("recording_url"):
                skipped_no_recording += 1
                continue
            if (call.get("duration") or 0) < MIN_DURATION:
                skipped_short += 1
                continue
            if not call.get("lead_id"):
                skipped_no_lead += 1
                continue

            eligible.append(call)

        if window_ended or not data.get("has_more"):
            break
        skip += 100
        pages += 1

    log(f"Inspected {inspected} Close calls in the {HOURS_BACK}h window")
    log(f"  Skipped (no recording):       {skipped_no_recording}")
    log(f"  Skipped (under {MIN_DURATION}s): {skipped_short}")
    log(f"  Skipped (no lead_id):         {skipped_no_lead}")
    log(f"  Eligible:                     {len(eligible)}")
    return eligible


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
def enrich_call(close_call, type_info):
    """
    Process one Close call: look up its Avoma call resource by external_id,
    resolve the linked meeting, and if analysis is complete, create a
    Custom Activity on the matched lead. Returns the new Custom Activity
    ID, or None if skipped for any reason.
    """
    call_id = close_call["id"]
    duration = close_call.get("duration") or 0
    lead_id = close_call.get("lead_id")

    log(f"\n[{call_id}] duration={duration}s, lead={lead_id}")

    # 1. Look up the Avoma call resource by external_id (direct key —
    #    no more "invert the loop"; see module docstring).
    avoma_call = avoma_get_call_by_external_id(call_id)
    if not avoma_call:
        log("→ No Avoma call yet (still importing or never uploaded), skip", indent=1)
        return None

    meeting_uuid = extract_meeting_uuid(avoma_call)
    if not meeting_uuid:
        log("→ Avoma call resource found but no linked meeting UUID could be extracted (unexpected response shape — see assumption #1), skip", indent=1)
        log(f"  Raw response keys: {list(avoma_call.keys())}", indent=2)
        return None

    meeting = avoma_get_meeting(meeting_uuid)
    if not meeting:
        log(f"→ Could not fetch meeting {meeting_uuid}, skip", indent=1)
        return None

    title = meeting.get("subject", "")
    log(f"Avoma meeting: {meeting_uuid}", indent=1)
    log(f"Subject:       {title!r}", indent=1)

    # 2. Check processing completeness
    evaluations = avoma_get_scorecard_evaluations(meeting_uuid)
    notes_raw = avoma_get_notes(meeting_uuid)
    notes = parse_avoma_notes(notes_raw)
    if not evaluations and not notes:
        log("→ Avoma analysis not yet complete, skip (will retry next run)", indent=1)
        log(f"  scorecard evaluations: {len(evaluations)}, parsed notes categories: {len(notes)}", indent=2)
        return None
    if not notes:
        log("Note: no parsed notes (likely setter-style scorecard or unrecognized notes shape); proceeding with scorecard-only fields", indent=1)

    # 3. Idempotency check
    field_ids = type_info["fields"]
    call_id_field_id = field_ids.get(CLOSE_FIELD_NAMES["call_id"])
    if not call_id_field_id:
        log(f"→ '{CLOSE_FIELD_NAMES['call_id']}' field not found in Custom Activity Type, abort", indent=1)
        return None
    if custom_activity_already_exists(lead_id, type_info["id"], meeting_uuid, call_id_field_id):
        log("→ Custom Activity already exists for this meeting, skip", indent=1)
        return None

    # 4. Pull analysis fields
    qa_score = extract_qa_score(evaluations)
    doubt_text = get_note_value(notes, DOUBT_ANALOG_CATEGORY)
    call_summary = get_note_value(notes, CALL_SUMMARY_ANALOG_CATEGORY)

    # 5. Claude Haiku enrichment
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

    # 6. Build Custom Activity payload
    avoma_link = f"https://app.avoma.com/meetings/{meeting_uuid}"  # ASSUMPTION — unconfirmed URL format
    field_mapping = {
        CLOSE_FIELD_NAMES["call_link"]: avoma_link,
        CLOSE_FIELD_NAMES["call_id"]: meeting_uuid,
        CLOSE_FIELD_NAMES["call_title"]: title,
        CLOSE_FIELD_NAMES["qa_score"]: qa_score,
        CLOSE_FIELD_NAMES["primary_objection"]: primary_objection,
        CLOSE_FIELD_NAMES["key_concern"]: html_wrap(key_concern),
        CLOSE_FIELD_NAMES["lost_reason"]: html_wrap(lost_reason),
        CLOSE_FIELD_NAMES["call_summary"]: html_wrap(call_summary),
        CLOSE_FIELD_NAMES["call_duration"]: meeting.get("duration") or duration,
        CLOSE_FIELD_NAMES["close_call_activity_id"]: call_id,
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
        return None

    resp = close_post("/activity/custom/", payload)
    if not resp.ok:
        raise Exception(f"Failed to create Custom Activity: {resp.status_code}: {resp.text[:500]}")

    activity_id = resp.json().get("id")
    log(f"✅ Created Custom Activity {activity_id} on lead {lead_id}", indent=1)
    return activity_id


# ===== Main =====
def main():
    section(
        f"Avoma → Close dialer call enrichment "
        f"(HOURS_BACK={HOURS_BACK}, MIN_DURATION={MIN_DURATION}s, DRY_RUN={DRY_RUN})"
    )

    section("Resolving Close Custom Activity Type")
    type_info = find_custom_activity_type()
    log(f"Type:   {CUSTOM_ACTIVITY_TYPE_NAME}")
    log(f"ID:     {type_info['id']}")
    log(f"Fields ({len(type_info['fields'])}):")
    for name, field_id in sorted(type_info["fields"].items()):
        log(f"  {name}: {field_id}", indent=1)

    since_dt = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    section(f"Finding recent Close calls since {since_dt.isoformat()}")
    close_calls = find_recent_close_calls(since_dt)

    section("Enriching matched meetings")
    stats = {"enriched": 0, "skipped": 0, "failed": 0}

    for call in close_calls:
        try:
            activity_id = enrich_call(call, type_info)
            if activity_id:
                stats["enriched"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            stats["failed"] += 1
            log(f"❌ Error enriching {call.get('id')}: {e}", indent=1)

    section("Done")
    log(f"Enriched: {stats['enriched']}")
    log(f"Skipped:  {stats['skipped']}")
    log(f"Failed:   {stats['failed']}")

    sys.exit(0 if stats["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
