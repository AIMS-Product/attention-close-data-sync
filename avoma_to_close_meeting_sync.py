#!/usr/bin/env python3
"""
Avoma → Close meeting analysis sync (everything not a first-sale or dialer).

Avoma rebuild of attention_to_close_meeting_sync.py. Captures every analyzed
Avoma meeting that ISN'T already handled by:
  - avoma_to_close_first_meeting_sync.py (first sales calls — subject
    contains "vendingpren" and lacks any follow-up exclusion keyword)
  - the dialer-call sync (meetings created via POST /v1/calls/ during the
    Close→Avoma upload step, identified by the native `is_call` flag)

Everything else falls to this sync — follow-ups, discovery calls, setter
calls, next-steps reviews, generic "Call with X" titles, "X Vending
Consultation" calls that don't carry the "vendingpren" suffix, etc.

============================================================================
READ THIS FIRST — assumptions carried over from the rebuild, pending
Stephen's confirmation (see avoma-migration-rebuild-plan.md in the project
docs for the full write-up):

1. CLOSE-SIDE NAMING (CUSTOM_ACTIVITY_TYPE_NAME / CLOSE_FIELD_NAMES below):
   defaults to REUSING the exact Custom Activity Type + field names the
   Attention integration already writes to, on the theory that swapping
   the call-intelligence vendor shouldn't require new Close config. If you
   want Avoma-sourced activities to live in their own CA type/fields
   instead, change the two blocks below — nothing else in this script
   needs to change.

2. NOTES → FIELD MAPPING (NOTES_CATEGORY_ALIASES, DOUBT_ANALOG_CATEGORY,
   CALL_SUMMARY_ANALOG_CATEGORY, DEAL_SUMMARY_ANALOG_CATEGORY below):
   CONFIRMED 2026-09-04 (via avoma_to_close_dialer_sync.py, which hit the
   same /v1/notes/ endpoint against real analyzed calls, replacing the
   earlier "Adam's toy demo meeting" guess). Avoma's /v1/notes/ endpoint
   returns a paginated envelope whose "results" holds a wrapper record
   containing a FLAT Slate-style block list: "header-2" blocks name each
   category (Participants, Key Takeaways, Action Items, Pain Points, plus
   many call-specific categories that vary per call — e.g. "Situational
   Analysis", "Gap Analysis"), followed by content blocks until the next
   header. parse_avoma_notes() now walks this real shape. Call Summary is
   now every parsed category concatenated (see build_full_call_summary),
   not just "Key Takeaways" alone. Pain Points ≈ Attention's "Doubt"
   field — not guaranteed on every call (comes through blank when a call
   has no discovery objections, which is expected, not a bug). Attention's
   "Money/Finances" and "Why Now?" still have no obvious Avoma analog.

3. QA SCORE SHAPE (extract_qa_score below): /v1/scorecard_evaluations/ has
   only been observed as an empty result set — no live scorecard has
   scored a real call yet. The field-name guesses here are UNVERIFIED.

4. ATTENDANCE / SHOW-UP (derive_show_value below): Attention had a
   labels.Attendance classification with no confirmed Avoma analog. This
   uses a duration + speaker-talk-time heuristic via the confirmed
   /v1/meeting_segments/ endpoint. Needs validation against real calls.

5. OUTCOME / LOST DETECTION: Avoma meetings have a native `outcome` field,
   but it was confirmed NULL on every real meeting pulled so far (nothing
   is tagging calls yet). This sync still checks it defensively, but in
   practice Lost Reason will not populate until reps/setters start using
   Avoma's outcome tagging.

6. AVOMA WEB LINK FORMAT (avoma_link below): assumed
   https://app.avoma.com/meetings/{uuid} — not independently confirmed,
   easy one-line fix if wrong.
============================================================================

Required GitHub secrets:
  CLOSE_API_KEY         Close API key (Basic auth)
  AVOMA_API_KEY         Avoma org API key (Bearer auth — confirmed working
                         as-is; the colon in the key is part of the opaque
                         token, no splitting needed)
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

# ---- Close-side naming (see assumption #1 in the module docstring) ----
CUSTOM_ACTIVITY_TYPE_NAME = "Attention - Meeting Analysis"
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
    "meeting_type": "Meeting Type",
}

# Keywords that disqualify a title from being a "first sales call". Mirrors
# avoma_to_close_first_meeting_sync.py's filter — keep in sync.
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

# Primary Objection dropdown values. Must match Close field config exactly.
OBJECTION_CHOICES = ("Timing", "Investment", "Fit", "Other")

# Meeting Type dropdown values. Must match Close field config exactly.
MEETING_TYPE_CHOICES = ("Follow-up", "Discovery", "Next Steps", "Setter", "Other")

# Substrings (case-insensitive) in Avoma's `outcome` field that indicate a
# lost deal. See assumption #5 — outcome is null on every real meeting so
# far, so this rarely fires today. Kept so Lost Reason starts working
# automatically once reps tag outcomes.
LOSS_OUTCOME_MARKERS = ("disqualified", "lost", "not interested", "closed lost")

# Lead-level field IDs for the three "Follow Up Call Show N" slots (Close
# custom fields — unchanged from the Attention build, since these live on
# the Close lead, not on anything Avoma touches).
FOLLOW_UP_CALL_SHOW_FIELDS = (
    "cf_dObuoBvyXtiJr8DD1cwCJroonvji5Bsyog48xig7vBr",  # Slot 1
    "cf_MDhIC6P8CFyRxwGgEaOygkhgDp2VZeNXNAZKHDUD5Ob",  # Slot 2
    "cf_AepH7zN22aSBceUoSBZuiYL68wl8CEc5zlGK54bKAjA",  # Slot 3
)

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
DEAL_SUMMARY_ANALOG_CATEGORY = "Timeline"  # weakest guess of the three

CLOSE_REQUEST_DELAY = 0.5
AVOMA_REQUEST_DELAY = 0.2

# Auth setup
_close_auth_b64 = base64.b64encode(f"{CLOSE_API_KEY}:".encode()).decode()
CLOSE_HEADERS = {"Authorization": f"Basic {_close_auth_b64}"}
# Confirmed 2026-08-28: `Authorization: Bearer <key>` works as-is against
# the public Avoma API — unlike Attention, no header-format quirks.
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
    """Strip leading decorative chars (emoji, whitespace) before the first ASCII letter."""
    return re.sub(r"^[^a-zA-Z]+", "", name).strip()


def clean_title(title):
    """Strip recording upload suffixes — mirrors the Attention-side sync."""
    if not title:
        return title
    return re.sub(
        r"\s*-\s*\d{4}[_\-]\d{2}[_\-]\d{2}[\s_]\d{2}[_\-]\d{2}.*$",
        "",
        title,
    ).strip()


def html_wrap(text):
    """
    Wrap plain text for Close Custom Activity Textarea fields. Unchanged
    from the Attention build — Close's XHTML Textarea requirement doesn't
    care what vendor the text came from. See the original dialer sync's
    docstring for the debugging history of this format.
    """
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
    """
    Generic Avoma GET with retry. `url` may be a full URL (used when
    following a paginated `next` link) or a bare path under AVOMA_API_BASE.
    """
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
    """
    Fetch Avoma meetings in [since_dt, until_dt]. CONFIRMED 2026-08-28:
    GET /v1/meetings/ requires `from_date`/`to_date` (ISO8601 UTC) as
    required query params — not documented in Adam's handoff or the
    OpenAPI excerpt we could pull. Paginated; follows the DRF-style `next`
    link (the {"results": [...], "count": N} shape was confirmed via the
    /v1/scorecard_evaluations/ probe) until exhausted.
    """
    params = {
        "from_date": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to_date": until_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_size": 100,
    }
    log(f"Fetching Avoma meetings {params['from_date']} → {params['to_date']}...")

    meetings = []
    url = "/meetings/"
    next_params = params
    for _ in range(500):  # hard cap on pages
        resp = avoma_get(url, params=next_params)
        if not resp.ok:
            raise Exception(f"Avoma meetings list returned {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        meetings.extend(body.get("results", []))
        next_url = body.get("next")
        if not next_url:
            break
        url = next_url
        next_params = None  # `next` already carries the full query string
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
    """Confirmed live 2026-08-28: returns per-category time ranges plus a
    `speaker_segments` map of per-speaker talk-time. Used only for the
    attendance heuristic (see derive_show_value) so it's fetched lazily,
    not for every meeting."""
    resp = avoma_get("/meeting_segments/", params={"uuid": meeting_uuid})
    if not resp.ok:
        return None
    return resp.json()


# ===== Avoma notes parsing (assumption #2 — CONFIRMED 2026-09-04, see below) =====
def _slate_node_text(node):
    """Recursively pull plain text out of a Slate.js-style node/tree."""
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
    Walk Avoma's /v1/notes/ response and return {category_name: plain_text}.

    CONFIRMED 2026-09-04 against real analyzed calls (via
    avoma_to_close_dialer_sync.py, same /v1/notes/ endpoint — replaces the
    earlier "Adam's toy demo meeting" guess). Avoma's /notes/ endpoint
    returns a paginated envelope ({"count","next","previous","results"});
    "results" holds one (occasionally more) wrapper record(s), and the
    actual note content is a FLAT Slate-style block list nested somewhere
    inside each record (found dynamically via _find_block_list rather
    than a hardcoded key path, since the exact wrapper key wasn't
    confirmed and may not be stable). Blocks alternate: a "header-2"-type
    block whose extracted text is the category name (e.g. "Key
    Takeaways", "Pain Points", "Action Items", plus many call-specific
    categories like "Situational Analysis (current vs desired state)"),
    followed by one or more content blocks (typically "unordered-list")
    whose extracted text is that category's content, until the next
    header block.
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
    """
    UNVERIFIED SHAPE (see assumption #3). No live scorecard has evaluated a
    real call yet, so /v1/scorecard_evaluations/ has only been observed
    empty. Tries several plausible field-name/path guesses in priority
    order; returns None (safe no-op — field omitted from the Close
    payload) if nothing matches.
    """
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


# ===== Title classification & filtering =====
def is_first_sale_title(title):
    """Mirror of avoma_to_close_first_meeting_sync.py's filter. Keep in sync."""
    if not title:
        return False
    lower = clean_title(title).lower()
    if FIRST_SALE_TITLE_MARKER not in lower:
        return False
    if any(kw in lower for kw in FIRST_SALE_EXCLUSION_KEYWORDS):
        return False
    return True


def is_meeting_candidate(meeting):
    """
    Inverse selection: capture every Avoma meeting that ISN'T a first sales
    call and ISN'T a dialer-originated call. `is_call` is a native boolean
    on the meeting object — confirmed present (POST /v1/calls/ testing
    showed `is_call: true` on the auto-created meeting) — and replaces the
    old "Close Dialer Call" title-substring hack from the Attention build.
    """
    if meeting.get("is_call"):
        return False
    title = meeting.get("subject", "")
    if is_first_sale_title(title):
        return False
    return True


def classify_meeting_type(title):
    """Same precedence logic as the Attention build — operates on Avoma's
    `subject` field instead of Attention's `title`."""
    if not title:
        return "Other"
    lower = title.lower()
    if "follow up" in lower or "follow-up" in lower:
        return "Follow-up"
    if "discovery" in lower:
        return "Discovery"
    if "next steps" in lower:
        return "Next Steps"
    if "setter" in lower:
        return "Setter"
    return "Other"


# ===== Anthropic (Claude Haiku) — unchanged from the Attention build =====
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


# ===== Avoma data extraction =====
def get_prospect_email(meeting):
    """
    UNVERIFIED against the meetings endpoint specifically (see assumption
    in module docstring's item 2 area). Assumes `attendees` (or
    `participants`) list with `email` and, when present, `is_rep` — mirrors
    the CONFIRMED /v1/transcriptions/ `speakers[]` shape (email, name,
    is_rep, id). Prefers `is_rep` when present (more robust); falls back to
    INTERNAL_DOMAIN exclusion otherwise.
    """
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
    """Title-only fallback — unchanged pattern from the Attention build."""
    if not title:
        return None
    m = re.search(r"\bwith\s+((?:[A-Z][a-zA-Z\.]*\s*)+)", title)
    if m:
        return m.group(1).strip()
    return None


def derive_show_value(meeting, segments):
    """
    BEST-EFFORT / UNVERIFIED (see assumption #4 in module docstring).
    Avoma has no confirmed direct analog to Attention's
    labels.Attendance ("Shown"/"No-show"/"Late"/"Ghost"). Heuristic:
      1. If meeting_segments' `speaker_segments` map shows ANY talk-time
         for a non-internal speaker → "Yes".
      2. If segments are available but show only internal speakers
         talking (or nobody) → "No".
      3. If segments are unavailable, fall back to a bare duration
         threshold (>60s → "Yes", 0s → "No").
      4. Otherwise → None (unknown; caller leaves the field untouched,
         same safe default as the Attention build).
    """
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
            speaker_lower = str(speaker_key).lower()
            if speaker_lower in internal_emails:
                continue
            # Any external speaker with recorded talk-time counts as shown
            return "Yes"
        return "No"

    duration = meeting.get("duration") or 0
    if duration > 60:
        return "Yes"
    if duration == 0:
        return "No"
    return None


def count_followup_cas_for_lead(lead_id, type_id, meeting_type_field_id):
    resp = close_get(
        "/activity/custom/",
        params={"lead_id": lead_id, "custom_activity_type_id": type_id},
    )
    if not resp.ok:
        return 0
    count = 0
    for activity in resp.json().get("data", []):
        if activity.get(f"custom.{meeting_type_field_id}") == "Follow-up":
            count += 1
    return count


def update_followup_slot(lead_id, slot_number, show_value):
    if slot_number < 1 or slot_number > len(FOLLOW_UP_CALL_SHOW_FIELDS):
        return False
    field_id = FOLLOW_UP_CALL_SHOW_FIELDS[slot_number - 1]
    payload = {f"custom.{field_id}": show_value}

    if DRY_RUN:
        log(f"DRY_RUN — would PUT lead {lead_id} with: {payload}", indent=1)
        return True

    resp = close_put(f"/lead/{lead_id}/", payload)
    if not resp.ok:
        log(f"⚠️  Failed to update Follow Up Call Show {slot_number}: {resp.status_code}: {resp.text[:300]}", indent=1)
        return False
    return True


# ===== Custom Activity Type resolution (unchanged — still Close API) =====
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


# ===== Close lead matching (unchanged — still Close API) =====
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


# ===== Idempotency (unchanged — still Close API) =====
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
    """
    Process one Avoma meeting. Returns ('enriched', activity_id) on
    success, ('skipped', reason) when filtered out or unmatched, and
    raises on Close API errors so the caller's try/except can record it as
    failed.
    """
    uuid = meeting.get("uuid", "")
    title = meeting.get("subject", "")

    log(f"\n[{uuid}] '{title}'")

    # 1. Title/native-flag filter
    if not is_meeting_candidate(meeting):
        log("→ First-sale or dialer-originated meeting (handled by another sync), skip", indent=1)
        return ("skipped", "title-filter")

    # 2. Require completed analysis
    evaluations = avoma_get_scorecard_evaluations(uuid)
    notes_raw = avoma_get_notes(uuid)
    notes = parse_avoma_notes(notes_raw)
    log(f"Parsed notes categories: {list(notes.keys())}", indent=1)
    if not evaluations and not notes:
        if not ALLOW_INCOMPLETE_ANALYSIS:
            log("→ Avoma analysis not yet complete (no scorecard evaluations, no notes), skip", indent=1)
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

    # 3. Resolve Close lead — email primary, title-name fallback
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
    # Call Summary = every parsed category concatenated, not just one
    # section — see build_full_call_summary().
    call_summary = build_full_call_summary(notes)
    log(f"Call summary length: {len(call_summary)} chars across {len(notes)} categories", indent=1)

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

    # 7. Classify Meeting Type from title
    meeting_type = classify_meeting_type(title)
    log(f"Meeting Type: {meeting_type}", indent=1)

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
        CLOSE_FIELD_NAMES["meeting_type"]: meeting_type,
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
        if meeting_type == "Follow-up":
            segments = avoma_get_meeting_segments(uuid)
            show_value = derive_show_value(meeting, segments)
            if show_value is None:
                log("Attendance unclear; would skip Follow Up Call Show update", indent=1)
            else:
                meeting_type_field_id = field_ids.get(CLOSE_FIELD_NAMES["meeting_type"])
                existing_count = count_followup_cas_for_lead(lead_id, type_info["id"], meeting_type_field_id)
                projected_slot = existing_count + 1
                if projected_slot > len(FOLLOW_UP_CALL_SHOW_FIELDS):
                    log(f"Lead would have {projected_slot} follow-ups; past slot 3, would skip", indent=1)
                else:
                    log(f"Would update Follow Up Call Show {projected_slot} = {show_value!r}", indent=1)
        else:
            log(f"Meeting Type is {meeting_type!r}; no follow-up slot update applies", indent=1)
        return ("skipped", "dry-run")

    resp = close_post("/activity/custom/", payload)
    if not resp.ok:
        raise Exception(f"Failed to create Custom Activity: {resp.status_code}: {resp.text[:500]}")

    activity_id = resp.json().get("id")
    log(f"✅ Created Custom Activity {activity_id} on lead '{lead_name}'", indent=1)

    if meeting_type == "Follow-up":
        segments = avoma_get_meeting_segments(uuid)
        show_value = derive_show_value(meeting, segments)
        if show_value is None:
            log("Attendance unclear; skipping Follow Up Call Show update", indent=1)
        else:
            meeting_type_field_id = field_ids.get(CLOSE_FIELD_NAMES["meeting_type"])
            count = count_followup_cas_for_lead(lead_id, type_info["id"], meeting_type_field_id)
            if count > len(FOLLOW_UP_CALL_SHOW_FIELDS):
                log(f"Lead has {count} Follow-up CAs; past slot 3, no further slots to update", indent=1)
            else:
                ok = update_followup_slot(lead_id, count, show_value)
                if ok:
                    log(f"Updated Follow Up Call Show {count} = {show_value!r}", indent=1)

    return ("enriched", activity_id)


# ===== Main =====
def main():
    section(
        f"Avoma → Close meeting analysis sync (HOURS_BACK={HOURS_BACK}, DRY_RUN={DRY_RUN}, "
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
