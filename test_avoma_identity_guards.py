import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault("CLOSE_API_KEY", "offline")
os.environ.setdefault("AVOMA_API_KEY", "offline")
import avoma_to_close_first_meeting_sync as sync
from avoma_outcome_rules import select_unique_close_meeting

START = datetime(2026, 10, 21, 15, 30, tzinfo=timezone.utc)
CLOSE = {"id": "meeting", "lead_id": "lead", "user_id": "rep", "title": "Vendingpreneurs Next Steps",
         "starts_at": START.isoformat(), "status": "completed", "outcome_id": None,
         "calendar_event_uids": ["calendar-1"]}
AVOMA = {"uuid": "avoma", "subject": CLOSE["title"], "start_at": START.isoformat(), "calendar_event_id": "calendar-1"}


class IdentityGuardTests(unittest.TestCase):
    def test_calendar_uid_selects_one_neighbor_and_cannot_be_reused(self):
        neighbor = {**CLOSE, "id": "other", "calendar_event_uids": ["other-event"]}
        selected, reason = select_unique_close_meeting([CLOSE, neighbor], START, "vendingpreneurs", sync.offer_family_from_title, 4, calendar_uid="calendar-1")
        self.assertEqual("matched", reason)
        self.assertEqual(CLOSE["id"], selected["id"])

    def test_duplicate_calendar_identity_stays_ambiguous(self):
        selected, reason = select_unique_close_meeting([CLOSE, {**CLOSE, "id": "duplicate"}], START, "vendingpreneurs", sync.offer_family_from_title, 4, calendar_uid="calendar-1")
        self.assertIsNone(selected)
        self.assertEqual("close_match_ambiguous", reason)

    def test_changed_identity_and_unverified_write_are_rejected(self):
        response = lambda value: SimpleNamespace(ok=True, json=lambda: value)
        match = patch.object(sync, "find_close_meeting_activity", return_value=(CLOSE, "matched", [CLOSE]))
        with match, patch.object(sync, "DRY_RUN", False), patch.object(sync, "close_get", return_value=response({**CLOSE, "starts_at": "changed"})), patch.object(sync, "close_put") as put:
            self.assertEqual((False, "close_meeting_identity_changed"), sync.write_meeting_outcome("lead", AVOMA, "Show", "vendingpreneurs"))
            put.assert_not_called()
        with patch.object(sync, "find_close_meeting_activity", return_value=(CLOSE, "matched", [CLOSE])), patch.object(sync, "DRY_RUN", False), patch.object(sync, "close_get", side_effect=[response(CLOSE), response(CLOSE)]), patch.object(sync, "close_put", return_value=response({})):
            self.assertEqual((False, "close_outcome_readback_failed"), sync.write_meeting_outcome("lead", AVOMA, "Show", "vendingpreneurs"))
