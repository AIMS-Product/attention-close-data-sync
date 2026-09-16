#!/usr/bin/env python3
"""
Read-only diagnostic: fetch ONE Avoma meeting by UUID and show exactly what
the sync scripts would see and derive from it — no Close writes, no
idempotency check, no lead matching. Built to answer one question directly:
"is Avoma actually returning the tagged Outcome, and are we parsing it
right?" — bypassing everything else in the pipeline that could otherwise
mask the answer (most importantly: if a Custom Activity already exists for
this meeting, the real hourly sync scripts skip it via the idempotency
check BEFORE they ever look at outcome — so testing against the live
pipeline on an already-processed meeting proves nothing either way).

Usage:
  MEETING_UUID=<uuid> python3 test_avoma_outcome.py
  (defaults to the meeting below if MEETING_UUID isn't set)

Required env:
  AVOMA_API_KEY

Safe to run any time — read-only, makes no writes to Avoma or Close.
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta, timezone

AVOMA_API_KEY = os.environ["AVOMA_API_KEY"]
AVOMA_API_BASE = "https://api.avoma.com/v1"
AVOMA_HEADERS = {
    "Authorization": f"Bearer {AVOMA_API_KEY}",
    "Content-Type": "application/json",
}

MEETING_UUID = os.environ.get("MEETING_UUID", "b8780aef-0a08-4bea-b006-0e0670a030c0")


def log(msg, indent=0):
    print(f"{'  ' * indent}{msg}", flush=True)


def avoma_get(url, params=None):
    """Retry on 429/502/503/504, same pattern as the production sync scripts."""
    full_url = url if url.startswith("http") else f"{AVOMA_API_BASE}{url}"
    for attempt in range(6):
        resp = requests.get(full_url, headers=AVOMA_HEADERS, params=params, timeout=60)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "5"))
            log(f"[Avoma] 429 rate limited, waiting {wait}s...", indent=1)
            time.sleep(wait)
            continue
        if resp.status_code in (502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        time.sleep(0.3)  # small buffer between calls, matches production delay
        return resp
    raise Exception(f"Avoma GET {url} exhausted retries")


def fetch_meeting_by_uuid(uuid):
    """
    Try the direct detail endpoint first (GET /v1/meetings/{uuid}/ — the
    conventional REST shape, not yet confirmed against Avoma's real API).
    Falls back to paging the list endpoint over a wide window and matching
    by uuid, since avoma_list_meetings() elsewhere in this repo is the only
    CONFIRMED-working way to fetch meetings.
    """
    log(f"Trying direct fetch: GET /meetings/{uuid}/")
    resp = avoma_get(f"/meetings/{uuid}/")
    if resp.ok:
        log("→ Direct fetch worked", indent=1)
        return resp.json()
    log(f"→ Direct fetch failed ({resp.status_code}); falling back to list+filter", indent=1)

    # Wide net: 180 days back. Adjust if the meeting is older than that.
    until_dt = datetime.now(timezone.utc)
    since_dt = until_dt - timedelta(days=180)
    params = {
        "from_date": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to_date": until_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_size": 100,
    }
    log(f"Listing meetings {params['from_date']} → {params['to_date']} and searching for {uuid}...")
    url = "/meetings/"
    next_params = params
    pages = 0
    for _ in range(500):
        resp = avoma_get(url, params=next_params)
        if not resp.ok:
            raise Exception(f"Avoma meetings list returned {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        pages += 1
        for m in body.get("results", []):
            if m.get("uuid") == uuid:
                log(f"→ Found on page {pages}", indent=1)
                return m
        next_url = body.get("next")
        if not next_url:
            break
        url = next_url
        next_params = None
    log(f"→ Not found in the last 180 days ({pages} page(s) searched)", indent=1)
    return None


def extract_outcome_label(meeting):
    """
    CONFIRMED 2026-09-16: Avoma's `outcome` field is an OBJECT, not a
    plain string — {"label": "Disqualified", "uuid": "..."}. Untagged
    meetings return outcome: None. Normalizes both to a plain string.
    """
    outcome = meeting.get("outcome")
    if isinstance(outcome, dict):
        return outcome.get("label") or ""
    if isinstance(outcome, str):
        return outcome
    return ""


def derive_qualified_value(outcome_label):
    """Copied verbatim from avoma_to_close_first_meeting_sync.py — keep in sync."""
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


def derive_show_value_from_outcome(outcome_label):
    """Copied verbatim (outcome-based branch only) from the sync scripts."""
    if not outcome_label:
        return None
    lower = outcome_label.lower()
    if "no-show" in lower or "no show" in lower or "ghost" in lower:
        return "No"
    return "Yes"


def main():
    log("=" * 60)
    log(f"Avoma outcome diagnostic — meeting {MEETING_UUID}")
    log("=" * 60)

    meeting = fetch_meeting_by_uuid(MEETING_UUID)
    if not meeting:
        log(f"\n❌ Could not find meeting {MEETING_UUID} — nothing further to check.")
        sys.exit(1)

    log("\nRaw fields of interest:")
    for key in ("uuid", "subject", "outcome", "purpose", "duration", "start_at", "end_at", "is_call"):
        log(f"  {key}: {meeting.get(key)!r}", indent=1)

    raw_outcome = meeting.get("outcome")
    outcome_label = extract_outcome_label(meeting)
    log(f"\nRaw outcome field as returned by Avoma: {raw_outcome!r}")
    log(f"Extracted label: {outcome_label!r}")

    if not outcome_label:
        log("⚠️  outcome is empty/null — nothing was tagged on this meeting, or")
        log("   the field shape is different than assumed. Full meeting")
        log("   JSON dumped below for inspection.")
    else:
        qualified_value = derive_qualified_value(outcome_label)
        show_value = derive_show_value_from_outcome(outcome_label)
        log(f"\nderive_qualified_value({outcome_label!r}) → {qualified_value!r}")
        log(f"derive_show_value(..., outcome_label={outcome_label!r}) → {show_value!r}")
        if qualified_value is None:
            log("⚠️  No substring in the outcome matched our Qualified rules — check", indent=1)
            log("   derive_qualified_value()'s keyword list against this exact label text.", indent=1)

    log("\nFull raw meeting JSON:")
    log(json.dumps(meeting, indent=2, default=str)[:4000])


if __name__ == "__main__":
    main()
