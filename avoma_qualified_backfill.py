#!/usr/bin/env python3
"""
ONE-OFF BACKFILL — Qualified field, 2026-09-01 through today.

Context: Stephen suspects some September meetings never got the Close
"Qualified" lead field set, because the Qualified-writing logic in the
live pipeline only fully matured mid-month (the outcome-object bug fix,
then extending Qualified-writing beyond the first call — see
avoma_to_close_meeting_sync.py's module docstring for that history). Only
5 real meetings in the window actually have an Avoma Outcome tag; this
script also asks Claude Haiku to classify the rest directly from each
meeting's parsed Avoma notes, using Stephen's exact Qualified/Disqualified
criteria (the same text configured as the Outcome descriptions in Avoma's
Settings > Purposes and Outcomes, so AI-inferred calls are judged by the
identical bar as the 5 that already have a real Outcome tag).

SCOPE (per Stephen 2026-09-16): every non-dialer meeting in the window —
first-sale, follow-up, discovery, setter, next-steps, other. Dialer calls
(native `is_call: true`) are excluded — same call-intelligence pipeline,
just out of scope for this backfill.

WRITE RULE: only fills a BLANK Qualified field. If Qualified already has
any value, or Qualified Override = "Yes", the lead is left untouched and
logged as skipped — this backfill is for filling gaps, not re-litigating
values the live pipeline (or a rep) already set. Ambiguous AI verdicts
("Unclear") are skipped, not written — per explicit instruction, no
human-review staging step, but nothing gets written on a guess either.

This is NOT wired into the hourly pipeline and has no schedule — it's
meant to be run once (or a couple of times while tuning), by hand, via
workflow_dispatch. Meetings are processed oldest-first so that if a lead
had multiple calls in the window, the earliest one with a usable signal
is the one that fills the field — mirroring what would have happened had
the automation been running correctly all month.

Safe by default: DRY_RUN=1 unless explicitly set to "0". Always writes a
full audit CSV (every meeting considered, not just the ones written) so
the run can be sanity-checked before and after.

Required GitHub secrets:
  CLOSE_API_KEY, AVOMA_API_KEY, ANTHROPIC_API_KEY

Optional env vars:
  DRY_RUN            "1" (default) logs without writing to Close
  BACKFILL_SINCE      ISO8601 UTC, default "2026-09-01T00:00:00Z"
  BACKFILL_UNTIL       ISO8601 UTC, default: now
"""

import os
import sys
import csv
import re
import time
import json
import base64
import requests
from datetime import datetime, timezone

# ===== Config =====
CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]
AVOMA_API_KEY = os.environ["AVOMA_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"

BACKFILL_SINCE = os.environ.get("BACKFILL_SINCE", "2026-09-01T00:00:00Z")
BACKFILL_UNTIL = os.environ.get("BACKFILL_UNTIL", "")  # blank = now

CLOSE_API_BASE = "https://api.close.com/api/v1"
AVOMA_API_BASE = "https://api.avoma.com/v1"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

INTERNAL_DOMAIN = "@modern-amenities.com"

QUALIFIED_FIELD = "cf_ZDx7NBQaDzV1yYrFcBMzt6cIYj81dAcswpNN0CQzCPS"
QUALIFIED_OVERRIDE_FIELD = "cf_nizevVbDT00CqdjfqQY9NSiBvCtuRl1mT1VxQie6zpc"

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

CLOSE_REQUEST_DELAY = 0.5
AVOMA_REQUEST_DELAY = 0.2

# Stephen's exact criteria, 2026-09-16 — same text as the live Avoma
# Outcome descriptions (Settings > Purposes and Outcomes), so AI-inferred
# calls here are judged identically to the 5 real Avoma-tagged ones.
QUALIFIED_CRITERIA = (
    "Qualified requires: (1) prospect is engaged and interested, (2) ready "
    "to start within 30-60 days (beyond 60 days disqualifies regardless of "
    "finances), and (3) realistic financial capacity. Capacity scale: "
    "credit under 600 needs $5-10k liquid; 600-650 needs $3-5k; 650+ needs "
    "$2-3k; $6k+ liquid qualifies regardless of credit. Access to funds "
    "via a loan, partner, family member, or another person counts, even "
    "if not the prospect's own money. Don't require exact figures - use "
    "reasonable judgment from context (e.g. \"great credit, plenty of "
    "cash\" is enough on its own). Don't disqualify just for lack of "
    "precision."
)
DISQUALIFIED_CRITERIA = (
    "Disqualified applies when the prospect lacks realistic financial "
    "capacity or timeline, per the Qualified criteria - not just when "
    "info is missing. Specifically: credit and liquid capital both fall "
    "below the tiered minimums (under 600 credit with less than $5k "
    "liquid, 600-650 with less than $3k, 650+ with less than $2k) AND no "
    "accessible alternate funding (loan, partner, family, other person) "
    "is mentioned; OR the prospect states a start timeline beyond 60 days "
    "out. Also applies if the prospect shows no real interest or "
    "disengages. Do not disqualify solely because exact figures weren't "
    "stated - only when the conversation gives genuine reason to doubt "
    "capacity or timeline."
)

# Auth setup
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


# ===== Close API (copied verbatim from the production scripts) =====
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


# ===== Avoma API (copied verbatim from the production scripts) =====
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


def avoma_get_notes(meeting_uuid):
    resp = avoma_get("/notes/", params={"meeting_uuid": meeting_uuid})
    if not resp.ok:
        return None
    return resp.json()


# ===== Avoma notes parsing (copied verbatim from the production scripts —
# see avoma_to_close_meeting_sync.py's parse_avoma_notes docstring for the
# full "how this shape was confirmed" history) =====
def _slate_node_text(node):
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
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
    if not notes:
        return ""
    return "\n\n".join(f"{category}\n{content}" for category, content in notes.items())


# ===== Outcome shape (see the live scripts' 2026-09-16 dict-shape fix) =====
def extract_outcome_label(meeting):
    outcome = meeting.get("outcome")
    if isinstance(outcome, dict):
        return outcome.get("label") or ""
    if isinstance(outcome, str):
        return outcome
    return ""


def derive_qualified_value(outcome_label):
    """Deterministic path for meetings that already carry a real Avoma
    Outcome tag — copied verbatim from the production scripts."""
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


# ===== AI-inferred path, for meetings with no real Outcome tag =====
def haiku_classify_qualified(call_summary):
    """
    Applies Stephen's exact Qualified/Disqualified criteria (same text as
    the live Avoma Outcome descriptions) to a call's parsed notes.
    Returns 'Qualified', 'Disqualified', or 'Unclear' — 'Unclear' is
    returned whenever the summary doesn't give enough to confidently
    apply the criteria, rather than guessing.
    """
    if not call_summary or len(call_summary.strip()) < 40:
        return "Unclear"

    prompt = f"""You are auditing a past sales call to decide whether the prospect should be marked Qualified or Disqualified in the CRM, using these exact criteria:

QUALIFIED:
{QUALIFIED_CRITERIA}

DISQUALIFIED:
{DISQUALIFIED_CRITERIA}

Call notes/summary:
{call_summary[:4000]}

Based ONLY on the criteria above and the call notes, respond with EXACTLY ONE WORD: Qualified, Disqualified, or Unclear. Use Unclear if the notes don't give enough information to confidently apply the criteria — do not guess."""

    payload = {"model": HAIKU_MODEL, "max_tokens": 10, "messages": [{"role": "user", "content": prompt}]}
    resp = requests.post(ANTHROPIC_API_URL, headers=ANTHROPIC_HEADERS, json=payload)
    if not resp.ok:
        log(f"Haiku classify failed: {resp.status_code}: {resp.text[:300]}", indent=2)
        return "Unclear"
    text = resp.json()["content"][0]["text"].strip()
    lower = text.lower()
    # Check "disqualified" before "qualified" — the latter is a substring
    # of the former, same gotcha as derive_qualified_value() above.
    if "disqualified" in lower:
        return "Disqualified"
    if "qualified" in lower:
        return "Qualified"
    return "Unclear"


# ===== Close lead matching (copied verbatim from avoma_to_close_meeting_sync.py) =====
def get_prospect_email(meeting):
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
    if not title:
        return None
    m = re.search(r"\bwith\s+((?:[A-Z][a-zA-Z\.]*\s*)+)", title)
    if m:
        return m.group(1).strip()
    return None


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


# ===== Qualified read/write (backfill-specific: skip on ANY existing value) =====
def get_lead_qualified_state(lead_id):
    fields = f"id,display_name,custom.{QUALIFIED_OVERRIDE_FIELD},custom.{QUALIFIED_FIELD}"
    resp = close_get(f"/lead/{lead_id}/", params={"_fields": fields})
    if not resp.ok:
        return {"qualified_override": None, "qualified_current": None}
    data = resp.json()
    return {
        "qualified_override": data.get(f"custom.{QUALIFIED_OVERRIDE_FIELD}"),
        "qualified_current": data.get(f"custom.{QUALIFIED_FIELD}"),
    }


def write_lead_qualified(lead_id, value):
    payload = {f"custom.{QUALIFIED_FIELD}": value}
    if DRY_RUN:
        log(f"DRY_RUN — would PUT lead {lead_id} with: {payload}", indent=1)
        return True
    resp = close_put(f"/lead/{lead_id}/", payload)
    if not resp.ok:
        log(f"⚠️  Failed to update Qualified on lead {lead_id}: {resp.status_code}: {resp.text[:300]}", indent=1)
        return False
    return True


# ===== Per-meeting processing =====
def process_meeting(meeting):
    """
    Returns an audit row (dict) for every meeting considered, whether or
    not anything got written — so the CSV is a complete record of the
    run, not just the successful writes.
    """
    uuid = meeting.get("uuid", "")
    title = meeting.get("subject", "")
    start_at = meeting.get("start_at", "")

    row = {
        "meeting_uuid": uuid,
        "meeting_title": title,
        "meeting_start_at": start_at,
        "lead_id": "",
        "lead_name": "",
        "match_method": "",
        "source": "",
        "ai_verdict": "",
        "derived_value": "",
        "previous_qualified_value": "",
        "action": "",
        "reason": "",
    }

    log(f"\n[{uuid}] '{title}' ({start_at})")

    if meeting.get("is_call"):
        log("→ Dialer-originated call — out of scope for this backfill, skip", indent=1)
        row["action"] = "skipped"
        row["reason"] = "dialer-call"
        return row

    # 1. Resolve Close lead — email primary, title-name fallback
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
        row["action"] = "skipped"
        row["reason"] = "no-lead-match"
        return row

    lead_id = matched_lead["id"]
    lead_name = matched_lead.get("display_name", "Unknown")
    row["lead_id"] = lead_id
    row["lead_name"] = lead_name
    row["match_method"] = match_method
    log(f"Matched lead: {lead_name} ({lead_id}) via {match_method}", indent=1)

    # 2. Current Qualified state — skip if override-locked or already set
    state = get_lead_qualified_state(lead_id)
    row["previous_qualified_value"] = state["qualified_current"] or ""
    if (state["qualified_override"] or "").lower() == "yes":
        log("→ Qualified Override is 'Yes' — leaving field untouched, skip", indent=1)
        row["action"] = "skipped"
        row["reason"] = "override-locked"
        return row
    if state["qualified_current"]:
        log(f"→ Qualified already set to {state['qualified_current']!r} — leaving field untouched, skip", indent=1)
        row["action"] = "skipped"
        row["reason"] = "already-set"
        return row

    # 3. Determine desired value — real Outcome tag first, else AI-inferred
    outcome_label = extract_outcome_label(meeting)
    derived_value = None
    if outcome_label:
        derived_value = derive_qualified_value(outcome_label)
        row["source"] = "avoma-outcome"
        log(f"Real Avoma Outcome: {outcome_label!r} → derived {derived_value!r}", indent=1)
    else:
        notes_raw = avoma_get_notes(uuid)
        notes = parse_avoma_notes(notes_raw)
        call_summary = build_full_call_summary(notes)
        if not call_summary:
            log("→ No Avoma notes available for this meeting (analysis not complete / no data), skip", indent=1)
            row["action"] = "skipped"
            row["reason"] = "no-analysis-data"
            return row
        row["source"] = "ai-inferred"
        log("No real Outcome tag — classifying from parsed notes (Haiku)...", indent=1)
        verdict = haiku_classify_qualified(call_summary)
        row["ai_verdict"] = verdict
        log(f"→ {verdict}", indent=2)
        if verdict == "Qualified":
            derived_value = "Yes"
        elif verdict == "Disqualified":
            derived_value = "No"
        else:
            log("→ AI verdict Unclear — skipping rather than guessing", indent=1)
            row["action"] = "skipped"
            row["reason"] = "ai-unclear"
            return row

    if derived_value is None:
        log("→ No interpretable signal, skip", indent=1)
        row["action"] = "skipped"
        row["reason"] = "no-signal"
        return row

    row["derived_value"] = derived_value

    # 4. Write
    ok = write_lead_qualified(lead_id, derived_value)
    if ok:
        log(f"{'DRY_RUN — would set' if DRY_RUN else '✅ Set'} Qualified = {derived_value!r} on lead {lead_name}", indent=1)
        row["action"] = "dry-run-would-write" if DRY_RUN else "written"
    else:
        row["action"] = "failed"
        row["reason"] = "close-write-failed"
    return row


# ===== Main =====
def main():
    until_dt = (
        datetime.now(timezone.utc)
        if not BACKFILL_UNTIL
        else datetime.strptime(BACKFILL_UNTIL, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    )
    since_dt = datetime.strptime(BACKFILL_SINCE, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    section(f"Qualified backfill (DRY_RUN={DRY_RUN}) — {since_dt.isoformat()} → {until_dt.isoformat()}")

    meetings = avoma_list_meetings(since_dt, until_dt)
    log(f"Total meetings in window: {len(meetings)}")

    # Oldest first, so if a lead had multiple calls in the window, the
    # earliest usable signal is the one that fills the field — mirroring
    # what would have happened had the pipeline run correctly all month.
    meetings.sort(key=lambda m: m.get("start_at") or "")

    section("Processing meetings")
    rows = []
    stats = {}
    for meeting in meetings:
        try:
            row = process_meeting(meeting)
        except Exception as e:
            uuid = meeting.get("uuid", "?")
            log(f"❌ Error processing {uuid}: {e}", indent=1)
            row = {
                "meeting_uuid": uuid,
                "meeting_title": meeting.get("subject", ""),
                "meeting_start_at": meeting.get("start_at", ""),
                "lead_id": "", "lead_name": "", "match_method": "", "source": "",
                "ai_verdict": "", "derived_value": "", "previous_qualified_value": "",
                "action": "error", "reason": str(e)[:200],
            }
        rows.append(row)
        key = f"{row['action']}" + (f" ({row['reason']})" if row["reason"] else "")
        stats[key] = stats.get(key, 0) + 1

    section("Done")
    for key, count in sorted(stats.items(), key=lambda x: -x[1]):
        log(f"{key}: {count}")

    csv_path = f"qualified_backfill_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    fieldnames = [
        "meeting_uuid", "meeting_title", "meeting_start_at", "lead_id", "lead_name",
        "match_method", "source", "ai_verdict", "derived_value",
        "previous_qualified_value", "action", "reason",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    log(f"\nAudit CSV written: {csv_path}")

    written = sum(1 for r in rows if r["action"] in ("written", "dry-run-would-write"))
    errors = sum(1 for r in rows if r["action"] in ("error", "failed"))
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
