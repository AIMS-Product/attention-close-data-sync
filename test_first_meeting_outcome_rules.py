"""Offline regressions for AA attendance sync and existing VP safeguards."""
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("CLOSE_API_KEY", "offline-test")
os.environ.setdefault("AVOMA_API_KEY", "offline-test")
import avoma_to_close_first_meeting_sync as sync


def response(data, **extra):
    return SimpleNamespace(ok=True, json=lambda: {"data": data, **extra})


class AttendanceRules(unittest.TestCase):
    def setUp(self):
        self.quiet = patch.object(sync, "log")
        self.quiet.start()
        self.addCleanup(self.quiet.stop)
        self.start = datetime(2026, 10, 28, 16, 30, tzinfo=timezone.utc)
        self.meeting = {
            "uuid": "offline-meeting",
            "subject": "Acquisition Ace Launch - Next Steps with Casey",
            "start_at": self.start.isoformat(),
            "attendees": [{"email": "casey@example.com", "is_rep": False}],
            "outcome": {"label": "Follow-up Scheduled"},
        }
        self.close_meeting = {
            "id": "offline-activity", "title": self.meeting["subject"],
            "starts_at": self.start.isoformat(), "status": "completed",
            "outcome_id": None,
        }

    def test_aa_next_steps_allowed_without_vp_analysis(self):
        for title in [self.meeting["subject"], "AA Launch - Next Steps with Casey"]:
            with self.subTest(title=title):
                self.assertTrue(sync.is_supported_outcome_title(title))
                self.assertFalse(sync.is_first_sale_title(title))

    def test_vp_exclusions_and_other_offers_preserved(self):
        self.assertTrue(sync.is_supported_outcome_title("Casey and Vendingpreneurs Consultation"))
        for title in ["Vendingpreneurs Next Steps", "Vendingpreneurs Quick Discovery",
                      "TLG Launch", "PPA Call", "SaaS Next Steps", "Internal Huddle", ""]:
            with self.subTest(title=title):
                self.assertFalse(sync.is_supported_outcome_title(title))

    def test_positive_and_negative_outcome_mapping(self):
        for label, expected in [("Qualified", "completed"), ("Follow-up Scheduled", "completed"),
                                ("No-Show", "no_show"), ("Rescheduled", "rescheduled"),
                                ("Cancelled", "cancelled"), ("", None)]:
            with self.subTest(label=label):
                self.assertEqual(sync.derive_meeting_outcome(label), expected)

    def test_aa_requires_unique_email_match(self):
        for body in [response([]), response([{"id": "a"}, {"id": "b"}]),
                     response([{"id": "a"}], has_more=True)]:
            with patch.object(sync, "close_get", return_value=body):
                self.assertIsNone(sync.find_close_lead_by_email("casey@example.com", require_unique=True))
        with patch.object(sync, "close_get", return_value=response([{"id": "a"}])):
            self.assertEqual(sync.find_close_lead_by_email("casey@example.com", require_unique=True)["id"], "a")

    def test_vp_email_selection_keeps_existing_behavior(self):
        with patch.object(sync, "close_get", return_value=response([{"id": "a"}, {"id": "b"}])):
            self.assertEqual(sync.find_close_lead_by_email("casey@example.com")["id"], "a")

    def test_matching_rejects_wrong_offer_and_canceled_activity(self):
        wrong = {**self.close_meeting, "id": "wrong", "title": "Vendingpreneurs Consultation"}
        canceled = {**self.close_meeting, "id": "canceled", "title": "Canceled: " + self.meeting["subject"]}
        with patch.object(sync, "close_get", return_value=response([wrong, canceled, self.close_meeting])):
            self.assertEqual(sync.find_close_meeting_activity("lead", self.start, "acquisition_ace")["id"], "offline-activity")

    def test_matching_rejects_equal_time_duplicates(self):
        with patch.object(sync, "close_get", return_value=response([self.close_meeting, {**self.close_meeting, "id": "duplicate"}])):
            self.assertIsNone(sync.find_close_meeting_activity("lead", self.start, "acquisition_ace"))

    def test_terminal_outcomes_never_overwritten(self):
        for outcome in ["completed", "no_show", "cancelled", "rescheduled"]:
            with self.subTest(outcome=outcome), patch.object(sync, "find_close_meeting_activity", return_value={**self.close_meeting, "outcome_id": sync.OUTCOMES[outcome]}), patch.object(sync, "close_put") as put:
                result = sync.write_meeting_outcome("lead", self.meeting, "Qualified", "acquisition_ace")
                self.assertEqual(result, outcome == "completed")
                put.assert_not_called()

    def test_dry_run_has_no_write(self):
        with patch.object(sync, "find_close_meeting_activity", return_value=self.close_meeting), patch.object(sync, "DRY_RUN", True), patch.object(sync, "close_put") as put:
            self.assertTrue(sync.write_meeting_outcome("lead", self.meeting, "Qualified", "acquisition_ace"))
            put.assert_not_called()

    def test_live_write_only_updates_attendance_outcome(self):
        with patch.object(sync, "find_close_meeting_activity", return_value=self.close_meeting), patch.object(sync, "DRY_RUN", False), patch.object(sync, "close_put", return_value=SimpleNamespace(ok=True)) as put:
            self.assertTrue(sync.write_meeting_outcome("lead", self.meeting, "Follow-up Scheduled", "acquisition_ace"))
            put.assert_called_once_with("/activity/meeting/offline-activity/", {"outcome_id": sync.OUTCOMES["completed"]})

    def test_missing_tag_never_writes(self):
        with patch.object(sync, "find_close_meeting_activity") as find, patch.object(sync, "close_put") as put:
            self.assertFalse(sync.write_meeting_outcome("lead", self.meeting, "", "acquisition_ace"))
            find.assert_not_called()
            put.assert_not_called()

    def test_aa_process_syncs_outcome_without_analysis_or_qualification(self):
        with patch.object(sync, "find_close_lead_by_email", return_value={"id": "lead"}) as find, patch.object(sync, "write_meeting_outcome", return_value=True) as write, patch.object(sync, "update_lead_qualified") as qualify, patch.object(sync, "avoma_get_notes") as notes:
            self.assertEqual(sync.process_meeting(self.meeting, None), ("outcome-synced", "offer-outcome-only"))
            find.assert_called_once_with("casey@example.com", require_unique=True)
            write.assert_called_once_with("lead", self.meeting, "Follow-up Scheduled", required_offer="acquisition_ace")
            qualify.assert_not_called()
            notes.assert_not_called()

    def test_aa_unmatched_email_does_not_fall_back_to_name(self):
        with patch.object(sync, "find_close_lead_by_email", return_value=None), patch.object(sync, "find_close_lead_by_title") as fallback, patch.object(sync, "write_meeting_outcome") as write:
            self.assertEqual(sync.process_meeting(self.meeting, None), ("skipped", "no-match"))
            fallback.assert_not_called()
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
