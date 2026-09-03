#!/usr/bin/env python3
"""
Close → Avoma dialer call sync.

Avoma rebuild of close_to_attention_sync.py — the :15 slot. Hourly: pulls
Close native dialer calls from the last HOURS_BACK hours that have a
recording_url and duration >= MIN_DURATION, re-hosts each recording
somewhere Avoma can reach over plain HTTPS, and calls Avoma's
`POST /v1/calls/` to import it (external_id = Close call activity ID, for
idempotency and for the direct `GET /v1/calls/{external_id}/` lookup the
:30 dialer sync uses downstream — see avoma_to_close_dialer_sync.py).

WHY THIS IS DIFFERENT FROM THE ATTENTION VERSION:
The Attention build had to craft an intentionally-filtered title
("{lead} - Close Dialer Call", no "vendingpren") so the reverse sync's
title-keyword filter would skip these conversations and not overwrite the
lead's video-call fields. Avoma's side doesn't need that trick — the read
scripts (avoma_to_close_meeting_sync.py / avoma_to_close_first_meeting_sync.py)
exclude dialer-originated meetings via the native `is_call` boolean, not a
title string. So this script doesn't try to control the title at all —
Avoma auto-generates a subject at import (confirmed 2026-08-28: e.g. "Call
with Darrell (+19162764250) on August 28, 2026").

Also unlike Attention's 3-step signed-upload dance (get URL → PUT bytes →
POST import), Avoma's `POST /v1/calls/` is a single call — but it requires
`recording_url` to be a real public URL it can fetch with no auth (it
could not authenticate to Close's own Basic-auth-gated recording URL —
confirmed 2026-08-28). This script downloads the MP3 from Close with our
own credentials and re-hosts it briefly via `upload_recording_and_get_public_url()`
before handing that URL to Avoma.

============================================================================
READ THIS FIRST — status and assumptions (full write-up in
avoma-migration-rebuild-plan.md in the project docs):

1. **RECORDING STORAGE BACKEND — NOT YET IMPLEMENTED, THIS BLOCKS REAL
   RUNS.** `upload_recording_and_get_public_url()` below is a deliberate
   stub (raises NotImplementedError) because the storage backend (S3 / R2
   / GCS / other) hasn't been decided yet. Everything else in this script
   — the Avoma-user gate, idempotency check, participant/payload
   construction — is complete and runs today under DRY_RUN=1 (DRY_RUN
   short-circuits BEFORE the download/upload/POST, same as the Attention
   version did). Once a backend is picked, fill in that one function —
   ready-to-adapt reference implementations for all three options are in
   its docstring — and this script is done.

2. **Avoma-user gate** (`avoma_build_active_user_emails` /
   `avoma_active_user_emails`) — CONFIRMED NECESSARY: `POST /v1/calls/`
   rejects any `user_email` that isn't an existing, active Avoma user
   (tested 2026-08-28 against Kelly Schrader, not yet onboarded — got
   `{"user_email":["User (kelly@modern-amenities.com) with this email not
   found."]}`). This script checks the Close rep's email against Avoma's
   user list first and skips-and-logs if they're not seated yet, rather
   than letting the POST fail. UNVERIFIED DETAIL: whether `GET /v1/users/`
   distinguishes "invited, not yet accepted" from "active" — the handoff
   doc only confirms the endpoint is read-only, not its exact fields. If
   invited-but-not-accepted users show up in this list, the gate will
   under-filter (a rep who looks seated but hasn't actually accepted their
   invite) and you'll see the same 400 from Avoma; tighten the filter in
   `avoma_build_active_user_emails` once you've seen a real response.

3. **`participants` payload shape** — UNVERIFIED. The handoff doc confirms
   `POST /v1/calls/` accepts a `participants` field but not its exact
   shape. This assumes `[{"email":..., "name":..., "is_rep": bool}, ...]`,
   mirroring the CONFIRMED `/v1/transcriptions/` `speakers[]` shape
   (email, name, is_rep, id). Correct if wrong — isolated to
   `build_participants()`.

4. **`direction` and `source` values** — UNVERIFIED enums. `direction`
   passes through Close's own `direction` field (inbound/outbound,
   standard Close API) with a fallback of `"outbound"` since these are
   dialer calls made by reps. `source` is hardcoded to `"close"` as a
   readable label — confirm both against Avoma's actual accepted values on
   a real 400 response, or with Aditya/Avoma support.

5. **`crm_association` — OFF BY DEFAULT** (`ATTACH_CRM_ASSOCIATION` env
   var). The rebuild plan flags this as "not yet tested," and Avoma's org
   CRM provider currently reads as Pipedrive (not Close) per Adam's
   handoff — sending a Close lead ID through an association field that
   might expect a Pipedrive object shape risks either a silent no-op or an
   outright error. Once Stephen/Aditya confirm the Pipedrive setting is
   stale (see rebuild plan's "Pipedrive/Close question" section) and the
   `crm_obj_type` value Avoma expects, flip `ATTACH_CRM_ASSOCIATION=1` and
   verify by reading a real import back.

6. **Idempotency** uses `GET /v1/calls/{external_id}/` (existence check
   only — 200 vs. 404). This is the same lookup the :30 dialer sync uses;
   see that script's docstring for the caveat that its full response shape
   is still unconfirmed. A bare existence check is lower-risk than parsing
   deeper into an unconfirmed shape.
============================================================================

Required GitHub secrets:
  CLOSE_API_KEY         Close API key (Basic auth)
  AVOMA_API_KEY         Avoma org API key (Bearer auth)
  (plus whatever the storage backend needs once #1 above is filled in —
  e.g. AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY + a bucket name for S3/R2,
  or a GCS service-account key)

Optional env vars:
  HOURS_BACK                 Window of recent Close calls to consider (default: 8)
  MIN_DURATION                Skip calls shorter than this in seconds (default: 180)
  DRY_RUN                     If "1", log what would happen but don't import
  RECORDING_URL_TTL_SECONDS   TTL for the re-hosted recording URL (default: 3600)
  ATTACH_CRM_ASSOCIATION      If "1", attach crm_association (see assumption #5)
"""

import os
import sys
import base64
import time
import json
import requests
from datetime import datetime, timezone, timedelta

# ===== Config =====
CLOSE_API_KEY = os.environ["CLOSE_API_KEY"]
AVOMA_API_KEY = os.environ["AVOMA_API_KEY"]
HOURS_BACK = int(os.environ.get("HOURS_BACK", "8"))
MIN_DURATION = int(os.environ.get("MIN_DURATION", "180"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
RECORDING_URL_TTL_SECONDS = int(os.environ.get("RECORDING_URL_TTL_SECONDS", "3600"))
ATTACH_CRM_ASSOCIATION = os.environ.get("ATTACH_CRM_ASSOCIATION", "0") == "1"

CLOSE_API_BASE = "https://api.close.com/api/v1"
AVOMA_API_BASE = "https://api.avoma.com/v1"

# See assumption #4 — unverified enum values, isolated here for easy fixing.
DEFAULT_DIRECTION = "outbound"
SOURCE_VALUE = "close"

# See assumption #5 — leave "lead" as a placeholder until Avoma's actual
# expected crm_obj_type is confirmed. Only used when ATTACH_CRM_ASSOCIATION=1.
CRM_OBJ_TYPE = "lead"

CLOSE_REQUEST_DELAY = 0.5
AVOMA_REQUEST_DELAY = 0.2

# Auth setup
_close_auth_b64 = base64.b64encode(f"{CLOSE_API_KEY}:".encode()).decode()
CLOSE_HEADERS = {"Authorization": f"Basic {_close_auth_b64}"}
AVOMA_HEADERS = {
    "Authorization": f"Bearer {AVOMA_API_KEY}",
    "Content-Type": "application/json",
}


# ===== Helpers =====
def log(msg, indent=0):
    print(f"{'  ' * indent}{msg}", flush=True)


def section(label):
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}", flush=True)


# ===== Close API (unchanged from the Attention build) =====
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


def close_iter_recent_calls(max_pages=200):
    """Iterate Close call activities newest-first. Stops at max_pages safety limit."""
    skip = 0
    pages = 0
    while pages < max_pages:
        resp = close_get("/activity/call/", params={"_skip": skip, "_limit": 100})
        if not resp.ok:
            raise Exception(f"Close /activity/call/ returned {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        items = data.get("data", [])
        if not items:
            break
        for item in items:
            yield item
        if not data.get("has_more"):
            break
        skip += 100
        pages += 1


def close_get_lead_name(lead_id):
    if not lead_id:
        return ""
    resp = close_get(f"/lead/{lead_id}/", params={"_fields": "display_name"})
    if resp.ok:
        return resp.json().get("display_name", "") or ""
    return ""


def close_get_lead_primary_contact(lead_id):
    """
    Best-effort prospect name/email for the `participants` payload (see
    assumption #3). Not needed by the Attention build (it only used lead
    display_name for a title), so this is new — pulls the first contact
    with an email off the lead.
    """
    if not lead_id:
        return None, None
    resp = close_get(f"/lead/{lead_id}/", params={"_fields": "display_name,contacts"})
    if not resp.ok:
        return None, None
    data = resp.json()
    for contact in data.get("contacts", []) or []:
        emails = contact.get("emails") or []
        for e in emails:
            addr = e.get("email")
            if addr:
                return contact.get("name") or data.get("display_name"), addr
    return data.get("display_name"), None


def close_get_user_email(user_id):
    if not user_id:
        return None, None
    resp = close_get(f"/user/{user_id}/", params={"_fields": "email,first_name,last_name"})
    if resp.ok:
        data = resp.json()
        email = data.get("email")
        name = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p) or None
        return email, name
    return None, None


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


def avoma_post(path, json_data):
    url = f"{AVOMA_API_BASE}{path}"
    for attempt in range(6):
        resp = requests.post(url, headers=AVOMA_HEADERS, json=json_data, timeout=60)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            time.sleep(wait)
            continue
        if resp.status_code in (502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        time.sleep(AVOMA_REQUEST_DELAY)
        return resp
    raise Exception(f"Avoma POST {path} exhausted retries")


def avoma_build_active_user_emails():
    """
    Return a set of Avoma user emails (lowercased) to gate `POST /v1/calls/`
    against. See assumption #2 — the exact "active vs. invited" distinction
    in the response is unconfirmed; this includes every user the endpoint
    returns. Tighten if invited-but-not-accepted users turn out to be
    included and you start seeing 400s despite passing this gate.
    """
    emails = set()
    url = "/users/"
    params = {"page_size": 100}
    for _ in range(50):
        resp = avoma_get(url, params=params)
        if not resp.ok:
            raise Exception(f"Could not list Avoma users: {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        for u in body.get("results", []):
            email = (u.get("email") or "").lower().strip()
            if email:
                emails.add(email)
        next_url = body.get("next")
        if not next_url:
            break
        url = next_url
        params = None
    return emails


def avoma_call_exists(external_id):
    """
    Idempotency check via GET /v1/calls/{external_id}/ — existence only
    (200 vs. 404). See assumption #6 for why this doesn't try to parse
    further into the response.
    """
    resp = avoma_get(f"/calls/{external_id}/")
    if resp.status_code == 404:
        return False
    if resp.ok:
        return True
    log(f"⚠️  Avoma calls lookup returned {resp.status_code} for {external_id}: {resp.text[:200]} — treating as not-yet-imported", indent=1)
    return False


def avoma_extract_meeting_uuid(call_response):
    """Same defensive extraction as avoma_to_close_dialer_sync.py — see
    that script's docstring (assumption #1) for why this is guessy."""
    if not call_response:
        return None
    for key in ("meeting_uuid", "uuid", "conversation_uuid"):
        if call_response.get(key):
            return call_response[key]
    nested = call_response.get("meeting")
    if isinstance(nested, dict):
        return nested.get("uuid") or nested.get("id")
    return None


def avoma_import_call(payload):
    resp = avoma_post("/calls/", payload)
    if not resp.ok:
        raise Exception(f"Avoma import failed: {resp.status_code}: {resp.text[:500]}")
    return resp.json()


# ===== Recording storage shim =====
def upload_recording_and_get_public_url(audio_bytes, content_type, call_id):
    """
    ========================================================================
    PLUGGABLE SHIM — NOT YET IMPLEMENTED. Stephen hasn't picked a storage
    backend (S3 / R2 / GCS / other) yet — see assumption #1 in the module
    docstring. This is the ONE function standing between this script and a
    real (non-DRY_RUN) run; everything else is complete and reviewable
    today via DRY_RUN=1, which short-circuits before this is ever called.

    Contract: given the raw MP3 bytes downloaded from Close, upload them
    somewhere Avoma's servers can reach over plain HTTPS with NO auth
    (Avoma could not authenticate to Close's own gated recording URL —
    confirmed 2026-08-28), and return that public/presigned URL. A short
    TTL is fine — Avoma fetches promptly after POST /v1/calls/.
    RECORDING_URL_TTL_SECONDS (default 3600s) is a starting point.

    Reference implementations for the three options discussed, ready to
    uncomment and adapt once a backend is picked. All three write under an
    `avoma-dialer-recordings/` prefix keyed by Close call ID.

    --- AWS S3 (boto3; add `boto3` to the workflow's `pip install` step) ---
        import boto3
        s3 = boto3.client("s3")
        bucket = os.environ["RECORDING_PROXY_BUCKET"]
        key = f"avoma-dialer-recordings/{call_id}.mp3"
        s3.put_object(Bucket=bucket, Key=key, Body=audio_bytes, ContentType=content_type)
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=RECORDING_URL_TTL_SECONDS,
        )

    --- Cloudflare R2 (boto3, S3-compatible endpoint) ---
        import boto3
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        )
        bucket = os.environ["RECORDING_PROXY_BUCKET"]
        key = f"avoma-dialer-recordings/{call_id}.mp3"
        s3.put_object(Bucket=bucket, Key=key, Body=audio_bytes, ContentType=content_type)
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=RECORDING_URL_TTL_SECONDS,
        )

    --- Google Cloud Storage (add `google-cloud-storage` to pip install) ---
        from google.cloud import storage
        from datetime import timedelta
        client = storage.Client()
        bucket = client.bucket(os.environ["RECORDING_PROXY_BUCKET"])
        blob = bucket.blob(f"avoma-dialer-recordings/{call_id}.mp3")
        blob.upload_from_string(audio_bytes, content_type=content_type)
        return blob.generate_signed_url(expiration=timedelta(seconds=RECORDING_URL_TTL_SECONDS))
    ========================================================================
    """
    raise NotImplementedError(
        "upload_recording_and_get_public_url() is a stub — pick a storage "
        "backend (S3 / R2 / GCS) and fill this in; see its docstring for "
        "ready-to-adapt reference code for all three. Run with DRY_RUN=1 "
        "in the meantime — DRY_RUN skips this call entirely, so the "
        "Avoma-user gate, idempotency check, and payload construction can "
        "all be reviewed today."
    )


# ===== Payload construction =====
def build_participants(rep_email, rep_name, prospect_name, prospect_email):
    """See assumption #3 — shape is unverified, isolated here for easy fixing."""
    participants = []
    if rep_email:
        participants.append({"email": rep_email, "name": rep_name or rep_email, "is_rep": True})
    if prospect_email:
        participants.append({"email": prospect_email, "name": prospect_name or prospect_email, "is_rep": False})
    return participants


# ===== Main sync logic =====
def find_eligible_calls(since_dt):
    """Return Close calls newer than since_dt with a recording and sufficient duration."""
    eligible = []
    inspected = 0
    skipped_short = 0
    skipped_no_recording = 0

    for call in close_iter_recent_calls():
        inspected += 1

        date_str = call.get("date_created", "")
        try:
            call_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if call_dt < since_dt:
            break

        if not call.get("recording_url"):
            skipped_no_recording += 1
            continue
        if (call.get("duration") or 0) < MIN_DURATION:
            skipped_short += 1
            continue

        eligible.append(call)

    log(f"Inspected {inspected} recent calls in the {HOURS_BACK}h window")
    log(f"  Skipped (no recording):       {skipped_no_recording}")
    log(f"  Skipped (under {MIN_DURATION}s): {skipped_short}")
    log(f"  Eligible:                     {len(eligible)}")
    return eligible


def import_call(call, avoma_user_emails, user_info_cache):
    """
    Import one Close call to Avoma. Returns the Avoma call/meeting
    identifier, or None if skipped (already imported, rep not seated in
    Avoma, no owner mapping, etc.).
    """
    call_id = call["id"]
    duration = call.get("duration", "?")

    log(f"\n[{call_id}] duration={duration}s, lead={call.get('lead_id')}, user={call.get('user_id')}")

    # 1. Idempotency
    if avoma_call_exists(call_id):
        log("→ Already imported in Avoma, skip", indent=1)
        return None

    # 2. Resolve owner: Close user_id → Close email → Avoma-seated gate
    close_user_id = call.get("user_id")
    if not close_user_id:
        log("→ No Close user_id on call, skip", indent=1)
        return None

    if close_user_id not in user_info_cache:
        user_info_cache[close_user_id] = close_get_user_email(close_user_id)
    rep_email, rep_name = user_info_cache[close_user_id]

    if not rep_email:
        log(f"→ Could not resolve email for Close user {close_user_id}, skip", indent=1)
        return None

    if rep_email.lower().strip() not in avoma_user_emails:
        log(f"→ {rep_email} is not an active Avoma user yet (seat-sequencing gate), skip", indent=1)
        return None

    log(f"Owner: {rep_email} — confirmed active Avoma user", indent=1)

    # 3. Prospect info for participants
    prospect_name, prospect_email = close_get_lead_primary_contact(call.get("lead_id"))
    lead_name = close_get_lead_name(call.get("lead_id"))
    participants = build_participants(rep_email, rep_name, prospect_name, prospect_email)
    log(f"Participants: {participants}", indent=1)

    direction = call.get("direction") or DEFAULT_DIRECTION

    if DRY_RUN:
        log("→ DRY_RUN, not downloading/uploading/importing", indent=1)
        preview_payload = {
            "external_id": call_id,
            "user_email": rep_email,
            "recording_url": "<would come from upload_recording_and_get_public_url()>",
            "participants": participants,
            "direction": direction,
            "source": SOURCE_VALUE,
        }
        if ATTACH_CRM_ASSOCIATION and call.get("lead_id"):
            preview_payload["crm_association"] = [
                {"crm_obj_id": call["lead_id"], "crm_obj_type": CRM_OBJ_TYPE}
            ]
        log(f"Would POST: {json.dumps(preview_payload, indent=2)[:1200]}", indent=1)
        return None

    # 4. Download MP3 from Close
    resp = requests.get(call["recording_url"], headers=CLOSE_HEADERS)
    if not resp.ok:
        raise Exception(f"Recording download failed: {resp.status_code}")
    audio_bytes = resp.content
    content_type = resp.headers.get("Content-Type", "audio/mpeg")
    log(f"Downloaded MP3: {len(audio_bytes):,} bytes ({content_type})", indent=1)

    # 5. Re-host so Avoma can fetch it with no auth (see shim docstring)
    public_url = upload_recording_and_get_public_url(audio_bytes, content_type, call_id)
    log(f"Re-hosted recording at: {public_url}", indent=1)

    # 6. POST /v1/calls/
    payload = {
        "external_id": call_id,
        "user_email": rep_email,
        "recording_url": public_url,
        "participants": participants,
        "direction": direction,
        "source": SOURCE_VALUE,
    }
    if ATTACH_CRM_ASSOCIATION and call.get("lead_id"):
        payload["crm_association"] = [
            {"crm_obj_id": call["lead_id"], "crm_obj_type": CRM_OBJ_TYPE}
        ]

    result = avoma_import_call(payload)
    meeting_uuid = avoma_extract_meeting_uuid(result)
    log(f"✅ Imported (state={result.get('state', '?')}) for lead '{lead_name or call.get('lead_id')}'", indent=1)
    if meeting_uuid:
        log(f"   https://app.avoma.com/meetings/{meeting_uuid}", indent=1)  # ASSUMPTION — unconfirmed URL format
    else:
        log("   (meeting record materializes asynchronously — no UUID in the immediate response; the :30 dialer sync will pick it up via external_id once ready)", indent=1)
    return meeting_uuid or call_id


def main():
    section(
        f"Close → Avoma sync "
        f"(HOURS_BACK={HOURS_BACK}, MIN_DURATION={MIN_DURATION}s, DRY_RUN={DRY_RUN}, "
        f"ATTACH_CRM_ASSOCIATION={ATTACH_CRM_ASSOCIATION})"
    )

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=HOURS_BACK)
    log(f"Looking for Close calls created since {since.isoformat()}")

    section("Building Avoma active-user set")
    avoma_user_emails = avoma_build_active_user_emails()
    log(f"Loaded {len(avoma_user_emails)} Avoma users")
    for email in sorted(avoma_user_emails):
        log(f"  {email}", indent=1)

    section("Finding eligible Close calls")
    calls = find_eligible_calls(since)

    section("Importing calls")
    user_info_cache = {}  # Close user_id → (email, name)
    stats = {"imported": 0, "skipped": 0, "failed": 0}

    for call in calls:
        try:
            result = import_call(call, avoma_user_emails, user_info_cache)
            if result:
                stats["imported"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            stats["failed"] += 1
            log(f"❌ Error importing {call.get('id')}: {e}", indent=1)

    section("Done")
    log(f"Imported: {stats['imported']}")
    log(f"Skipped:  {stats['skipped']}")
    log(f"Failed:   {stats['failed']}")

    sys.exit(0 if stats["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
