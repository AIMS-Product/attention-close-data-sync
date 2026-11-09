#!/usr/bin/env python3
"""Close is the durable backlog: revisit every unresolved supported past meeting."""
import json
from datetime import datetime, timedelta, timezone
import avoma_to_close_first_meeting_sync as sync
from avoma_outcome_rules import select_unique_close_meeting


def external_emails(meeting):
    return {(a.get("email") or "").casefold() for a in meeting.get("attendees", [])
            if a.get("is_rep") is not True and not a.get("user_id") and a.get("email")}


def evidence_windows(meetings):
    """Merge nearby appointments, cap each query at one day to bound source pages."""
    windows = []
    for meeting in meetings:
        start = sync.avoma_meeting_start({"start_at": meeting.get("starts_at")})
        if start:
            windows.append((start - timedelta(hours=4), start + timedelta(hours=4)))
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] and end - merged[-1][0] <= timedelta(days=1):
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    # New meetings get evidence first; old meetings remain eligible on every pass.
    return list(reversed(merged))


def matching_avoma(close_meeting, lead_meetings, avoma_meetings, emails):
    family = sync.offer_family_from_title(close_meeting.get("title") or "")
    start = sync.avoma_meeting_start({"start_at": close_meeting.get("starts_at")})
    matches = []
    for avoma in avoma_meetings:
        avoma_start = sync.avoma_meeting_start(avoma)
        if avoma_start is None or start is None or abs((avoma_start-start).total_seconds()) > 14400:
            continue
        if sync.offer_family_from_title(avoma.get("subject") or "") != family:
            continue
        uid = avoma.get("calendar_event_id")
        uid_match = uid and uid in (close_meeting.get("calendar_event_uids") or [])
        if not uid_match and not emails.intersection(external_emails(avoma)):
            continue
        selected, reason = select_unique_close_meeting(
            lead_meetings, avoma_start, family, sync.offer_family_from_title, 4, calendar_uid=uid)
        if reason == "matched" and selected.get("id") == close_meeting["id"]:
            matches.append(avoma)
    stable = [a for a in matches if a.get("calendar_event_id") in (close_meeting.get("calendar_event_uids") or [])]
    return stable or matches


def reconcile():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=4)
    candidates = sync.fetch_close_blank_eligible_meetings(datetime(1970, 1, 1, tzinfo=timezone.utc), cutoff)
    report = {"generated_at": now.isoformat(), "dry_run": sync.DRY_RUN,
              "backlog_count": len(candidates), "resolved": [], "review_items": [], "errors": []}
    def checkpoint():
        with open("avoma_outcome_review.json", "w") as stream:
            json.dump(report, stream, indent=2)
    checkpoint()
    valid = [m for m, reason in candidates if reason is None]
    meetings = {}
    for start, end in evidence_windows(valid):
        try:
            for avoma in sync.avoma_list_meetings(start, end):
                meetings[avoma["uuid"]] = avoma
            report["last_source_window"] = {"start": start.isoformat(), "end": end.isoformat()}
            checkpoint()
        except Exception as error:
            # Never turn an incomplete source capture into an absence inference.
            report["errors"].append({"window_start": start.isoformat(), "error": str(error)[:300]})
    histories, contacts = {}, {}
    for close_meeting, start_reason in candidates:
        lead_id = close_meeting.get("lead_id")
        reason = start_reason
        source_status = {}
        try:
            if not reason:
                if lead_id not in histories:
                    histories[lead_id], error = sync.fetch_close_meetings_for_lead(lead_id)
                    if error:
                        raise RuntimeError(error)
                    response = sync.close_get(f"/lead/{lead_id}/", params={"_fields": "id,contacts"})
                    if not response.ok:
                        raise RuntimeError("Close lead contacts unavailable")
                    contacts[lead_id] = {e["email"].casefold() for c in response.json().get("contacts", [])
                                         for e in c.get("emails", []) if e.get("email")}
                emails = contacts[lead_id] | external_emails(close_meeting)
                matches = matching_avoma(close_meeting, histories[lead_id], meetings.values(), emails)
                if len(matches) == 1:
                    avoma = matches[0]
                    source_status = {"avoma_meeting_id": avoma.get("uuid"),
                                     "recording_state": avoma.get("state"),
                                     "recording_failure_reason": avoma.get("disable_reason"),
                                     "processing_status": avoma.get("processing_status")}
                    success, reason = sync.write_meeting_outcome(
                        lead_id, avoma, sync.extract_outcome_label(avoma),
                        sync.offer_family_from_title(close_meeting.get("title") or ""),
                        matched_activity=close_meeting, known_lead_meetings=histories[lead_id])
                    if not success and reason in {"close_outcome_reread_failed", "close_outcome_write_failed", "close_outcome_readback_failed"}:
                        report["errors"].append({"meeting": close_meeting["id"], "error": reason})
                    if success:
                        report["resolved"].append({"meeting": close_meeting["id"], "avoma": avoma["uuid"], "result": reason})
                        checkpoint()
                        continue
                else:
                    reason = "avoma_match_missing" if not matches else "avoma_match_ambiguous"
            start = sync.avoma_meeting_start({"start_at": close_meeting.get("ends_at") or close_meeting.get("starts_at")})
            due = start + timedelta(hours=48) if start else None
            report["review_items"].append({"close_activity_id": close_meeting["id"], "close_lead_id": lead_id,
                "owner_id": close_meeting.get("user_id"), "reason": reason, **source_status,
                "review_due_at": due.isoformat() if due else None,
                "overdue": due is None or now >= due})
        except Exception as error:
            report["errors"].append({"meeting": close_meeting["id"], "error": str(error)[:300]})
        checkpoint()
    with open("avoma_outcome_review.json", "w") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({key: len(value) if isinstance(value, list) else value for key, value in report.items()}))
    return 1 if report["errors"] or any(r["overdue"] for r in report["review_items"]) else 0


if __name__ == "__main__":
    raise SystemExit(reconcile())
