"""Offline checks for Avoma qualification routing."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("CLOSE_API_KEY", "offline-test")
os.environ.setdefault("AVOMA_API_KEY", "offline-test")
os.environ.setdefault("ANTHROPIC_API_KEY", "offline-test")

import avoma_to_close_meeting_sync as sync  # noqa: E402


class QualifiedRoutingTests(unittest.TestCase):
    def test_setter_and_discovery_calls_do_not_read_or_write_lead(self):
        with patch.object(sync, "get_lead_qualified_override", side_effect=AssertionError("Close API should not be called")):
            self.assertEqual(sync.update_lead_qualified("lead_test", "Qualified", "Setter"), {})
            self.assertEqual(sync.update_lead_qualified("lead_test", "Disqualified", "Discovery"), {})

    def test_quick_discovery_and_route_planning_are_screening_calls(self):
        self.assertEqual(sync.classify_meeting_type("Vendingpreneurs Quick Discovery"), "Discovery")
        self.assertEqual(sync.classify_meeting_type("Route Planning Call"), "Setter")

    def test_unqualified_outcome_maps_to_no(self):
        self.assertEqual(sync.derive_qualified_value("Unqualified"), "No")


if __name__ == "__main__":
    unittest.main()
