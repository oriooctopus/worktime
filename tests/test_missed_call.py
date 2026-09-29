#!/usr/bin/env python3
"""Calls the microphone never saw: busy block + meeting app in front, no meeting.

Run: pytest tests/test_missed_call.py
"""

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

DAY = "2026-09-29"

CALENDAR = """---
generated: 2026-09-29T13:15:35-04:00
---
# Calendar — Tuesday 2026-09-29

| Start | End | Event | Calendar |
|-------|-----|-------|----------|
| 09:14 | 09:24 | Working (browsing) | work |
| 11:00 | 11:30 | (busy) | work |
| 12:00 | 13:15 | (busy) | work |
| 13:30 | 14:30 | MRI | personal |
| 14:00 | 14:30 | (busy) | work |
"""


class MissedCallCase(unittest.TestCase):
    NAMES = ("STATE", "MEETINGS", "MISSED_CALLS", "CAL_FILE", "FOCUS_DIR",
             "now_local")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {n: getattr(wp, n) for n in self.NAMES}
        wp.STATE = self.tmp
        wp.MEETINGS = os.path.join(self.tmp, "meetings.jsonl")
        wp.MISSED_CALLS = os.path.join(self.tmp, "missed-calls.jsonl")
        wp.CAL_FILE = os.path.join(self.tmp, "calendar-today.md")
        wp.FOCUS_DIR = os.path.join(self.tmp, "focus")
        os.makedirs(wp.FOCUS_DIR)
        wp.now_local = lambda: datetime(2026, 9, 29, 13, 20)
        with open(wp.CAL_FILE, "w") as fh:
            fh.write(CALENDAR)

    def tearDown(self):
        for n, v in self.saved.items():
            setattr(wp, n, v)

    def focus(self, *rows):
        with open(os.path.join(wp.FOCUS_DIR, f"{DAY}.jsonl"), "a") as fh:
            for t, bundle in rows:
                fh.write(json.dumps({"day": DAY, "t": t, "bundle": bundle}) + "\n")

    def meeting(self, start, end):
        with open(wp.MEETINGS, "a") as fh:
            fh.write(json.dumps({"day": DAY, "start": start, "end": end,
                                 "title": "meeting"}) + "\n")

    def test_zoom_in_a_finished_busy_block_is_asked_about(self):
        self.focus(("12:03:58", "us.zoom.xos"), ("12:40:18", "us.zoom.xos"))
        self.assertEqual(wp.missed_call(DAY, 13 * 60 + 20),
                         {"block_start": "12:00", "block_end": "13:15",
                          "start": "12:03", "end": "13:15",
                          "app": "Zoom"})

    def test_block_still_running_is_not_asked_yet(self):
        self.focus(("12:03:58", "us.zoom.xos"))
        self.assertIsNone(wp.missed_call(DAY, 13 * 60 + 10))

    def test_block_without_a_meeting_app_is_not_asked(self):
        self.focus(("12:03:58", "com.google.Chrome"))
        self.assertIsNone(wp.missed_call(DAY, 13 * 60 + 20))

    def test_block_with_a_recorded_meeting_is_not_asked(self):
        self.focus(("12:03:58", "us.zoom.xos"))
        self.meeting(12 * 60 + 59, 13 * 60 + 11)
        self.assertIsNone(wp.missed_call(DAY, 13 * 60 + 20))

    def test_joining_early_counts_from_the_join(self):
        self.focus(("11:55:10", "us.zoom.xos"))
        self.assertEqual(wp.missed_call(DAY, 13 * 60 + 20)["start"], "11:55")

    def test_personal_and_tracker_rows_are_never_calls(self):
        self.focus(("09:15:00", "us.zoom.xos"), ("13:45:00", "us.zoom.xos"))
        self.assertIsNone(wp.missed_call(DAY, 13 * 60 + 50))

    def test_a_calendar_for_another_day_is_ignored(self):
        with open(wp.CAL_FILE, "w") as fh:
            fh.write(CALENDAR.replace("2026-09-29", "2026-09-28"))
        self.focus(("12:03:58", "us.zoom.xos"))
        self.assertIsNone(wp.missed_call(DAY, 13 * 60 + 20))

    def test_not_asked_while_a_meeting_is_running(self):
        self.focus(("12:03:58", "us.zoom.xos"))
        with open(wp.MEETINGS, "a") as fh:
            fh.write(json.dumps({"day": DAY, "start": 13 * 60 + 18, "end": None,
                                 "title": "meeting",
                                 "created": "2026-09-29T13:18:00"}) + "\n")
        self.assertIsNone(wp.missed_call(DAY, 13 * 60 + 20))

    def test_previous_call_tail_is_not_this_blocks_start(self):
        # Zoom still up at 11:52 from the 11:00-11:30 call's overrun would sit
        # inside the early-join allowance of 12:00 -- but a block ended at 11:30
        # and that is before this one began, so only 11:30 onward can count.
        # Here the recorded meeting of the earlier block ends 11:55, so 11:52
        # belongs to it and the call starts at 12:10.
        self.meeting(11 * 60, 11 * 60 + 55)
        self.focus(("11:52:00", "us.zoom.xos"), ("12:10:00", "us.zoom.xos"))
        self.assertEqual(wp.missed_call(DAY, 13 * 60 + 20)["start"], "12:10")

    def test_teams_is_named_as_teams(self):
        self.focus(("12:05:00", "com.microsoft.teams2"))
        self.assertEqual(wp.missed_call(DAY, 13 * 60 + 20)["app"], "Teams")

    def test_yes_records_the_confirmed_times_and_stops_asking(self):
        self.focus(("12:03:58", "us.zoom.xos"))
        wp.answer_missed_call("12:00", "13:15", True, "12:04", "13:08")
        self.assertEqual([(m["start"], m["end"]) for m in wp.meetings_for(DAY)],
                         [(12 * 60 + 4, 13 * 60 + 8)])
        self.assertIsNone(wp.missed_call(DAY, 13 * 60 + 20))

    def test_no_records_nothing_and_stops_asking(self):
        self.focus(("12:03:58", "us.zoom.xos"))
        wp.answer_missed_call("12:00", "13:15", False)
        self.assertEqual(wp.meetings_for(DAY), [])
        self.assertIsNone(wp.missed_call(DAY, 13 * 60 + 20))

    def test_yes_does_not_touch_a_meeting_running_now(self):
        # A live call must not be closed, or refuse the record, because an
        # earlier one is being confirmed.
        with open(wp.MEETINGS, "a") as fh:
            fh.write(json.dumps({"day": DAY, "start": 13 * 60 + 18, "end": None,
                                 "title": "meeting",
                                 "created": "2026-09-29T13:18:00"}) + "\n")
        wp.answer_missed_call("12:00", "13:15", True, "12:04", "13:08")
        spans = [(m["start"], m["open"]) for m in wp.meetings_for(DAY)]
        self.assertEqual(spans, [(12 * 60 + 4, False), (13 * 60 + 18, True)])

    def test_yes_with_backwards_or_future_times_is_refused(self):
        with self.assertRaises(ValueError):
            wp.answer_missed_call("12:00", "13:15", True, "13:08", "12:04")
        with self.assertRaises(ValueError):
            wp.answer_missed_call("12:00", "13:15", True, "12:04", "13:30")
        self.assertFalse(os.path.exists(wp.MISSED_CALLS))


if __name__ == "__main__":
    unittest.main()
