#!/usr/bin/env python3
"""
Avoma → Close first-meeting analysis sync (Custom Activity edition).

Avoma rebuild of attention_to_close_first_meeting_sync.py. Captures first
sales calls and writes them to a Custom Activity, plus updates the lead-
level Qualified field (honoring its override field).

UPDATED 2026-09-24 — MEETING OUTCOME IS NOW THE SOURCE OF TRUTH:
  * NEW: write_meeting_outcome() maps Avoma's native Outcome tag
    (Show / No-Show / Rescheduled / Qualified / Disqualified / ...) onto
    the Close MEETING's native Meeting Outcome (outcome_id):
        No-Show-ish            -> No Show
        Rescheduled-ish        -> Rescheduled
        Canceled-ish           -> Cancelled
        any other real tag     -> Completed  (a tagged conversation happened)
    Runs on every pass — including when the Custom Activity already
    exists — because Avoma's tag can land hours after analysis. Never
    overwrites an existing terminal outcome (human edits win).
  * REMOVED: the direct First Call Show Up field write and the
    duration/speaker-diarization show heuristic (derive_show_value /
    meeting_segments). outcome_sync.py's field projection now maintains
    First Call Show Up FROM the Meeting Outcome — single writer, and the
    First Call Show (Override) field remains untouched by automation.
    Qualified handling is unchanged.

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
2. NOTES → FIELD MAPPING — CONFIRMED 2026-09-04 (via avoma_to_close_dialer_
   sync.py, which hit the same /v1/notes/ endpoint against real analyzed
   calls). parse_avoma_notes() now walks the real flat Slate block
   structure (header-2 blocks name each category, followed by content
   blocks until the next header) instead of the old flat category/header
   guess. Call Summary is now every parsed category concatenated (see
   build_full_call_summary), not just "Key Takeaways" alone. "Pain
   Points" isn't guaranteed to exist on every call — comes through blank
   when a call has no discovery objections, which is expected.
3. QA SCORE SHAPE — extract_qa_score()'s field-name guesses are unverified;
   no live scorecard has scored a real call yet.
4. ATTENDANCE / SHOW-UP — REARCHITECTED 2026-09-24 (see UPDATED note at
   top). Avoma's Outcome tag now writes the Close Meeting Outcome
   directly; the lead field follows via outcome_sync.py's projection.
5. QUALIFIED DERIVATION — derive_qualified_value() reads Avoma's native
   `outcome` field. CONFIRMED WORKING 2026-09-16 against a real
   "Disqualified" tagged call.
6. AVOMA WEB LINK FORMAT — assumed https://app.avoma.com/meetings/{uuid}.
7. AVOMA MEETING START FIELD — avoma_meeting_start() tries several key
   names; the first real meeting processed logs which one matched. If
   none parse, the outcome write is skipped with a log line (we never
   guess which Close meeting to stamp).
============================================================================

Required GitHub secrets:
  CLOSE_API_KEY         Close API key (Basic auth)
  AVOMA_API_KEY         Avoma org API key (Bearer auth)
  ANTHROPIC_API_KEY     Anthropic API key (for Claude Haiku enrichment)

Optional env vars:
  HOURS_BACK                  Window of Avoma meetings to consider (default: 24)
  DRY_RUN                      If "1", log payloads without writing to Close
  ALLOW_INCOMPLETE_ANALYSIS    If "1", create the Custom Activity even when
                                Avoma analysis isn't ready yet, using only
                                the fields available without it. Test-only
                                — added 2026-09-04 while Avoma's call
                                intelligence pipeline was stalled org-wide
                                (pending a CSM reply on why). A CA created
                                this way will NOT be auto-enriched later —
                                the idempotency check sees it already
                                exists once real analysis lands and skips.
                                (The Meeting Outcome write is NOT affected:
                                it re-runs on every pass regardless.)
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
ALLOW_INCOMPLETE_ANALYSIS = os.environ.get("ALLOW_INCOMPLETE_ANALYSIS", "0") == "1"

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

# ---- Close native Meeting Outcome ids (SYNC WITH outcome_sync.py) ----
OUTCOMES = {
    "scheduled":   "outcome_032DjlzDKpdXJZOzK4f7q3",
    "completed":   "outcome_032Djn4dfeNuEoCunojA7K",
    "rescheduled": "outcome_032Djo72GJ2Lvw3Q296wxH",
    "no_show":     "outcome_032DjoyPo9BgPBdOF6DzqH",
    "cancelled":   "outcome_032DjpoQ9otqb8rGb7SIYt",
}
MEETING_MATCH_HOURS = 4  # Close meeting must start within this of the Avoma meeting

# Lead-level field IDs — Qualified only. (First Call Show Up is now
# maintained by outcome_sync.py's projection FROM the Meeting Outcome;
# this script must not also write it — two writers would fight.)
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


# ===== Avoma notes parsing (assumption #2 — CONFIRMED 2026-09-04, see below) =====
def _slate_node_text(node):
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        # Join with a space, not "" — sibling nodes at this level are
        # usually distinct text runs or nested sub-items (e.g. a bullet's
        # own text followed by a nested sub-list), and joining with no
        # separator glues them into unreadable run-ons like "JosephResend
        # the proposal..." (confirmed against a real avoma_to_close_dialer_
        # sync.py payload 2026-09-04, same underlying bug here).
        parts = [_slate_node_text(n) for n in node]
        return " ".join(p for p in parts if p)
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            return node["text"]
        children = node.get("children")
        if children:
            return _slate_node_text(children)
    return ""


def _slate_block_to_lines(children):
    """Extract one text line per top-level child of a content block,
    instead of flattening the whole block into one run-on string."""
    if not isinstance(children, list):
        text = _slate_node_text(children).strip()
        return [text] if text else []
    lines = []
    for child in children:
        text = _slate_node_text(child).strip()
        if text:
            lines.append(text)
    return lines


def _find_block_list(obj, depth=0, max_depth=6):
    """Hunt for a nested list of Slate-style blocks (dicts with a "type"
    key) inside an arbitrarily-nested /notes/ record, so we don't depend
    on a hardcoded key path that might not be stable."""
    if depth > max_depth:
        return None
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:5]):
        if any(("type" in x or "object" in x) for x in obj[:5]):
            return obj
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_block_list(v, depth + 1, max_depth)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_block_list(v, depth + 1, max_depth)
            if found:
                return found
    return None


def parse_avoma_notes(notes_response):
    """
    CONFIRMED 2026-09-04 against real analyzed calls (via
    avoma_to_close_dialer_sync.py, same /v1/notes/ endpoint). Avoma's
    /notes/ endpoint returns a paginated envelope
    ({"count","next","previous","results"}); "results" holds one
    (occasionally more) wrapper record(s), and the actual note content is
    a FLAT Slate-style block list nested somewhere inside each record
    (found dynamically via _find_block_list rather than a hardcoded key
    path, since the exact wrapper key wasn't confirmed and may not be
    stable). Blocks alternate: a "header-2"-type block whose extracted
    text is the category name (e.g. "Key Takeaways", "Pain Points",
    "Action Items", plus many call-specific categories like "Situational
    Analysis (current vs desired state)"), followed by one or more
    content blocks (typically "unordered-list") whose extracted text is
    that category's content, until the next header block.
    """
    if not notes_response:
        return {}

    records = notes_response
    if isinstance(notes_response, dict):
        records = (
            notes_response.get("results")
            or notes_response.get("data")
            or notes_response.get("notes")
            or []
        )
        if isinstance(records, dict):
            records = [records]
    if not isinstance(records, list):
        return {}

    blocks = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("type") or record.get("object"):
            blocks.append(record)
            continue
        nested = _find_block_list(record)
        if nested:
            blocks.extend(b for b in nested if isinstance(b, dict))

    out = {}
    current_category = None
    for block in blocks:
        btype = str(block.get("type") or block.get("object") or "").lower()
        if btype.startswith("header"):
            header_text = _slate_node_text(block.get("children")).strip()
            if header_text:
                current_category = NOTES_CATEGORY_ALIASES.get(header_text.lower(), header_text)
            continue
        if not current_category:
            continue
        lines = _slate_block_to_lines(block.get("children"))
        if not lines:
            continue
        bulleted = "\n".join(f"• {line}" for line in lines)
        out[current_category] = (
            (out[current_category] + "\n" + bulleted)
            if current_category in out
            else bulleted
        )
    return out


def build_full_call_summary(notes):
    """Combine every parsed category into one Call Summary block, in the
    order Avoma's notes document presents them — not just one section."""
    if not notes:
        return ""
    return "\n\n".join(f"{category}\n{content}" for category, content in notes.items())


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


def extract_outcome_label(meeting):
    """
    CONFIRMED 2026-09-16 against a real tagged call: Avoma's `outcome`
    field is an OBJECT, not a plain string — {"label": "Disqualified",
    "uuid": "..."} — unlike Attention's labels.Outcome, which was a bare
    string. An untagged meeting returns outcome: None. This normalizes
    both shapes to a plain string so the substring-matching logic below
    (and in derive_meeting_outcome / is_lost_outcome) doesn't need to
    know or care which shape it got. Always call this instead of reading
    meeting.get("outcome") directly.
    """
    outcome = meeting.get("outcome")
    if isinstance(outcome, dict):
        return outcome.get("label") or ""
    if isinstance(outcome, str):
        return outcome
    return ""


def derive_meeting_outcome(outcome_label):
    """
    Avoma Outcome tag -> Close Meeting Outcome key (or None = don't write).

    Negative/branch tags first; then ANY other real (non-empty) tag means
    the meeting happened with enough substance for the AI/rep to classify
    it at all -> Completed. This mirrors the Show/No-Show definitions
    configured in Avoma (Settings > Purposes and Outcomes): "Show",
    "Qualified", "Disqualified", "One Call Close", even "Not Interested"
    all imply a real two-way conversation took place.
    """
    if not outcome_label:
        return None
    lower = outcome_label.lower()
    if "no-show" in lower or "no show" in lower or "ghost" in lower:
        return "no_show"
    if "reschedul" in lower:
        return "rescheduled"
    if "cancel" in lower:
        return "cancelled"
    return "completed"


def derive_qualified_value(outcome_label):
    """
    Maps Avoma's native `outcome` field (see extract_outcome_label()) to
    'Yes'/'No' for the Qualified field. CONFIRMED WORKING 2026-09-16
    against a real "Disqualified" tagged call.
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


# ===== Close Meeting Outcome write (NEW 2026-09-24) =====
AVOMA_START_KEYS = ("start_at", "started_at", "start_time",
                    "scheduled_start_at", "meeting_start_time")
_logged_start_key = [False]


def avoma_meeting_start(meeting):
    """Parse the Avoma meeting's start datetime, trying known key names
    (assumption #7). Logs which key matched, once, so the winner gets
    confirmed against real data on the first run."""
    for key in AVOMA_START_KEYS:
        raw = meeting.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if not _logged_start_key[0]:
                log(f"[start-key] Avoma meeting start parsed from '{key}'", indent=1)
                _logged_start_key[0] = True
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def find_close_meeting_activity(lead_id, avoma_start):
    """The Close meeting activity on this lead nearest the Avoma meeting's
    start, within MEETING_MATCH_HOURS. Prefers non-canceled meetings."""
    resp = close_get("/activity/meeting/", params={
        "lead_id": lead_id, "_limit": 100,
        "_fields": "id,title,starts_at,status,outcome_id"})
    if not resp.ok:
        return None
    best, best_gap = None, None
    for m in resp.json().get("data", []):
        raw = m.get("starts_at")
        if not raw:
            continue
        try:
            st = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if m.get("status") == "canceled" or \
                (m.get("title") or "").strip().lower().startswith("canceled"):
            continue
        gap = abs((st - avoma_start).total_seconds())
        if gap <= MEETING_MATCH_HOURS * 3600 and (best_gap is None or gap < best_gap):
            best, best_gap = m, gap
    return best


def write_meeting_outcome(lead_id, meeting, outcome_label):
    """
    Write Avoma's verdict onto the Close MEETING's native Outcome — the
    source of truth for show rates. Runs every pass (Avoma tags can land
    hours after analysis), is idempotent, and NEVER overwrites an existing
    terminal outcome (human edits in the Close UI always win).
    """
    outcome_key = derive_meeting_outcome(outcome_label)
    if not outcome_key:
        log("No Avoma Outcome tag yet — Close Meeting Outcome untouched", indent=1)
        return
    avoma_start = avoma_meeting_start(meeting)
    if not avoma_start:
        log("⚠️  Could not parse Avoma meeting start time — outcome write skipped "
            f"(keys tried: {AVOMA_START_KEYS})", indent=1)
        return
    close_m = find_close_meeting_activity(lead_id, avoma_start)
    if not close_m:
        log(f"⚠️  No Close meeting within ±{MEETING_MATCH_HOURS}h of Avoma start "
            f"{avoma_start:%Y-%m-%d %H:%M}Z — outcome write skipped", indent=1)
        return
    current = close_m.get("outcome_id")
    if current == OUTCOMES[outcome_key]:
        log(f"Close Meeting Outcome already '{outcome_key}' — in sync", indent=1)
        return
    if current and current != OUTCOMES["scheduled"]:
        log(f"Close Meeting Outcome already terminal ({current}) — leaving it "
            f"(Avoma says '{outcome_label}'; review if they disagree)", indent=1)
        return
    if DRY_RUN:
        log(f"DRY_RUN — would set Meeting Outcome '{outcome_key}' on "
            f"{close_m['id']} ('{(close_m.get('title') or '')[:40]}') "
            f"from Avoma tag '{outcome_label}'", indent=1)
        return
    resp = close_put(f"/activity/meeting/{close_m['id']}/",
                     {"outcome_id": OUTCOMES[outcome_key]})
    if resp.ok:
        log(f"✅ Meeting Outcome set to '{outcome_key}' on {close_m['id']} "
            f"(from Avoma tag '{outcome_label}')", indent=1)
    else:
        log(f"⚠️  Meeting Outcome write failed: {resp.status_code}: {resp.text[:200]}",
            indent=1)


# ===== Lead Qualified update (show-field write removed 2026-09-24) =====
def get_lead_overrides(lead_id):
    fields = (
        f"id,"
        f"custom.{QUALIFIED_OVERRIDE_FIELD},"
        f"custom.{QUALIFIED_FIELD}"
    )
    resp = close_get(f"/lead/{lead_id}/", params={"_fields": fields})
    if not resp.ok:
        return {"qualified_override": None, "qualified_current": None}
    data = resp.json()
    return {
        "qualified_override": data.get(f"custom.{QUALIFIED_OVERRIDE_FIELD}"),
        "qualified_current": data.get(f"custom.{QUALIFIED_FIELD}"),
    }


def update_lead_qualified(lead_id, outcome_label):
    qualified_value = derive_qualified_value(outcome_label)
    if qualified_value is None:
        return {}

    overrides = get_lead_overrides(lead_id)
    if (overrides["qualified_override"] or "").lower() == "yes":
        log("Qualified Override is 'Yes' — leaving field untouched", indent=1)
        return {}
    if overrides["qualified_current"]:
        log(
            f"Qualified already set to {overrides['qualified_current']!r} — leaving field untouched (rep judgment wins)",
            indent=1,
        )
        return {}

    payload = {f"custom.{QUALIFIED_FIELD}": qualified_value}
    if DRY_RUN:
        log(f"DRY_RUN — would PUT lead {lead_id} with: {payload}", indent=1)
        return payload

    resp = close_put(f"/lead/{lead_id}/", payload)
    if not resp.ok:
        log(f"⚠️  Failed to update lead {lead_id}: {resp.status_code}: {resp.text[:300]}", indent=1)
        return {}
    return {"Qualified": qualified_value}


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

    # 2. Resolve Close lead (needed for the outcome write even when the
    #    Custom Activity turns out to be a duplicate)
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

    # 3. Meeting Outcome write — EVERY pass, before any dedupe/skip logic.
    #    Avoma's tag can land hours after the Custom Activity was created,
    #    so duplicates must still get their outcome synced. Never
    #    overwrites a terminal outcome, so re-running is free.
    outcome_label = extract_outcome_label(meeting)
    log(f"Outcome: {outcome_label!r}", indent=1)
    write_meeting_outcome(lead_id, meeting, outcome_label)
    update_lead_qualified(lead_id, outcome_label)

    # 4. Require completed analysis for the Custom Activity portion
    evaluations = avoma_get_scorecard_evaluations(uuid)
    notes_raw = avoma_get_notes(uuid)
    notes = parse_avoma_notes(notes_raw)
    log(f"Parsed notes categories: {list(notes.keys())}", indent=1)
    if not evaluations and not notes:
        if not ALLOW_INCOMPLETE_ANALYSIS:
            log("→ Avoma analysis not yet complete (no scorecard evaluations, no notes), skip CA", indent=1)
            return ("skipped", "not-analyzed")
        log(
            "→ Avoma analysis not yet complete, but ALLOW_INCOMPLETE_ANALYSIS=1 — "
            "proceeding with only the fields available now (round-trip test mode). "
            "NOTE: this Custom Activity will NOT be auto-enriched later once real "
            "analysis exists — the idempotency check will see it already exists "
            "and skip creating an updated one. Test-only, not for the scheduled cron.",
            indent=1,
        )
    if not notes:
        log("Note: no parsed notes (likely setter-style scorecard or unrecognized notes shape); proceeding with scorecard-only fields", indent=1)

    # 5. Idempotency (Custom Activity only — outcome sync already done above)
    field_ids = type_info["fields"]
    call_id_field_id = field_ids.get(CLOSE_FIELD_NAMES["call_id"])
    if not call_id_field_id:
        log(f"→ '{CLOSE_FIELD_NAMES['call_id']}' field not found in Custom Activity Type, abort", indent=1)
        return ("skipped", "missing-field")
    if custom_activity_already_exists(lead_id, type_info["id"], uuid, call_id_field_id):
        log("→ Custom Activity already exists for this meeting, skip CA (outcome still synced above)", indent=1)
        return ("skipped", "duplicate")

    # 6. Pull analysis fields
    qa_score = extract_qa_score(evaluations)
    doubt_text = get_note_value(notes, DOUBT_ANALOG_CATEGORY)
    # Call Summary = every parsed category concatenated, not just one
    # section — see build_full_call_summary().
    call_summary = build_full_call_summary(notes)
    log(f"Call summary length: {len(call_summary)} chars across {len(notes)} categories", indent=1)

    # 7. Haiku enrichment
    log("Classifying Primary Objection (Haiku)...", indent=1)
    primary_objection = haiku_classify_objection(doubt_text)
    log(f"→ {primary_objection}", indent=2)

    log("Summarizing Key Concern (Haiku)...", indent=1)
    key_concern = haiku_summarize_concern(doubt_text)
    log(f"→ {key_concern[:120]}", indent=2)

    lost_reason = ""
    if is_lost_outcome(outcome_label):
        log("→ Indicates loss; summarizing Lost Reason (Haiku)...", indent=2)
        deal_summary = get_note_value(notes, DEAL_SUMMARY_ANALOG_CATEGORY)
        lost_reason = haiku_summarize_lost_reason(deal_summary, call_summary, doubt_text)
        log(f"→ {lost_reason[:120]}", indent=2)

    # 8. Build payload
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
        return ("skipped", "dry-run")

    resp = close_post("/activity/custom/", payload)
    if not resp.ok:
        raise Exception(f"Failed to create Custom Activity: {resp.status_code}: {resp.text[:500]}")

    activity_id = resp.json().get("id")
    log(f"✅ Created Custom Activity {activity_id} on lead '{lead_name}'", indent=1)

    return ("enriched", activity_id)


# ===== Main =====
def main():
    section(
        f"Avoma → Close first-meeting sync (HOURS_BACK={HOURS_BACK}, DRY_RUN={DRY_RUN}, "
        f"ALLOW_INCOMPLETE_ANALYSIS={ALLOW_INCOMPLETE_ANALYSIS})"
    )

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
