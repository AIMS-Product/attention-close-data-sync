import json
import unittest
from datetime import datetime, timedelta, timezone
from close_meeting_window import read_meeting_window

START = datetime(2026, 11, 8, tzinfo=timezone.utc)
END = START + timedelta(days=1)
MEETING = {"id": "within-window", "starts_at": (START+timedelta(hours=1)).isoformat()}


class RollingWindowTests(unittest.TestCase):
    def test_exact_requested_time_bounds_and_complete_cursor_pages(self):
        calls = []
        def search(payload):
            calls.append(payload)
            return {"data": [MEETING], "cursor": "next" if len(calls) == 1 else None, "count": {"total": 1}}
        self.assertEqual([MEETING], read_meeting_window(search, "id,starts_at", START, END))
        condition = calls[0]["query"]["queries"][1]["condition"]
        self.assertEqual(START.isoformat(), condition["on_or_after"]["value"])
        self.assertEqual(END.isoformat(), condition["before"]["value"])
        self.assertEqual("next", calls[1]["cursor"])
        self.assertNotIn("2026-11-01", json.dumps(calls))

    def test_outside_window_meeting_is_rejected(self):
        for date in (START-timedelta(days=200), END):
            with self.assertRaisesRegex(RuntimeError, "out-of-window"):
                read_meeting_window(lambda p: {"data": [{"id": "outside", "starts_at": date.isoformat()}]}, "id,starts_at", START, END)

    def test_repeated_cursor_and_incomplete_response_fail_closed(self):
        for body in ({"data": [], "cursor": "repeat"}, {}, {"data": [], "count": {"total": 10001}}):
            with self.assertRaises(RuntimeError):
                read_meeting_window(lambda p: body, "id,starts_at", START, END)

    def test_invalid_window_cannot_enable_all_time_scan(self):
        with self.assertRaises(ValueError):
            read_meeting_window(lambda p: {"data": []}, "id,starts_at", END, START)
