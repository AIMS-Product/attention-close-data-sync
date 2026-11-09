import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault("CLOSE_API_KEY", "offline")
os.environ.setdefault("AVOMA_API_KEY", "offline")
import avoma_to_close_first_meeting_sync as sync
import reconcile_close_outcome_backlog as backlog

START = datetime(2026, 10, 21, 15, 30, tzinfo=timezone.utc)
CLOSE = {"id": "meeting", "lead_id": "lead", "user_id": "rep", "title": "Vendingpreneurs Next Steps",
         "starts_at": START.isoformat(), "status": "completed", "outcome_id": None,
         "calendar_event_uids": ["calendar-1"], "attendees": [{"email": "jane@example.com", "contact_id": "contact"}]}
AVOMA = {"uuid": "avoma", "subject": CLOSE["title"], "start_at": START.isoformat(),
         "calendar_event_id": "calendar-1", "attendees": [{"email": "jane@example.com", "is_rep": False}], "outcome": {"label": "Show"}}


class BacklogTests(unittest.TestCase):
    def test_calendar_uid_resolves_neighboring_booking(self):
        neighbor = {**CLOSE, "id": "other", "calendar_event_uids": ["other-event"]}
        self.assertEqual([AVOMA], backlog.matching_avoma(CLOSE, [CLOSE, neighbor], [AVOMA], {"jane@example.com"}))

    def test_calendar_uid_does_not_reuse_evidence_for_another_booking(self):
        neighbor = {**CLOSE, "id": "other", "calendar_event_uids": ["other-event"]}
        self.assertEqual([], backlog.matching_avoma(neighbor, [CLOSE, neighbor], [AVOMA], {"jane@example.com"}))

    def test_ambiguous_avoma_records_remain_review(self):
        self.assertEqual(2, len(backlog.matching_avoma(CLOSE, [CLOSE], [AVOMA, {**AVOMA, "uuid": "duplicate"}], set())))

    def test_window_history_has_no_age_expiry_and_is_bounded(self):
        old = {**CLOSE, "starts_at": "2024-01-01T00:00:00Z"}
        windows = backlog.evidence_windows([CLOSE, old])
        self.assertEqual(2, len(windows))
        self.assertEqual(2023, windows[-1][0].year)
        self.assertTrue(all(end-start <= timedelta(days=1) for start,end in windows))

    def test_census_does_not_assume_start_order(self):
        with patch.object(sync, "read_outcome_backlog", return_value=[{**CLOSE, "starts_at": "2024-01-01T00:00:00Z"}, CLOSE]):
            result = sync.fetch_close_blank_eligible_meetings(START-timedelta(days=1), START+timedelta(days=1))
        self.assertEqual([CLOSE], [m for m,r in result])

    def test_changed_identity_and_unverified_write_are_rejected(self):
        resp = lambda value: SimpleNamespace(ok=True, json=lambda: value)
        with patch.object(sync, "DRY_RUN", False), patch.object(sync, "close_get", return_value=resp({**CLOSE, "starts_at": "changed"})), patch.object(sync, "close_put") as put:
            self.assertEqual((False, "close_meeting_identity_changed"), sync.write_meeting_outcome("lead", AVOMA, "Show", "vendingpreneurs", matched_activity=CLOSE))
            put.assert_not_called()
        with patch.object(sync, "DRY_RUN", False), patch.object(sync, "close_get", side_effect=[resp(CLOSE), resp(CLOSE)]), patch.object(sync, "close_put", return_value=resp({})):
            self.assertEqual((False, "close_outcome_readback_failed"), sync.write_meeting_outcome("lead", AVOMA, "Show", "vendingpreneurs", matched_activity=CLOSE))


if __name__ == "__main__":
    unittest.main()
