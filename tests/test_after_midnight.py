#!/usr/bin/env python3
"""Work belongs to the calendar day it happened on, including 00:00-05:00.

A 5 AM day anchor used to drop everything before it from the day, and the
previous day never read past its own midnight, so after-midnight work was
credited to no day at all.

Run: pytest tests/test_after_midnight.py
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

DAY = "2026-09-03"


class AfterMidnightCounts(unittest.TestCase):
    NAMES = ("snapshot_path", "markdown_snapshot_path", "marks_for",
             "prompts_for", "desktop_prompts_for", "slack_for",
             "meetings_for", "summarize_span")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {n: getattr(wp, n) for n in self.NAMES}
        wp.snapshot_path = lambda day: os.path.join(self.tmp, f"{day}.json")
        wp.markdown_snapshot_path = (
            lambda day: os.path.join(self.tmp, f"{day}.md"))
        wp.marks_for = lambda day, stamps=None: []
        wp.desktop_prompts_for = lambda day: []
        wp.slack_for = lambda day: []
        wp.meetings_for = lambda day: []
        wp.summarize_span = lambda day, lo, hi: ""

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(wp, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prompts_just_after_midnight_form_a_period(self):
        base = datetime(2026, 9, 3, tzinfo=wp.LOCAL)
        events = [base.replace(minute=m) for m in (4, 5, 7, 8, 10)]
        wp.prompts_for = lambda day: list(events)
        wp.write_vault_snapshot(DAY, events, "fp")
        snap = json.load(open(wp.snapshot_path(DAY)))
        self.assertTrue(snap["worked"], "no period for 00:04-00:10 prompts")
        self.assertLess(snap["worked"][0]["start"], 5)
        self.assertEqual(snap["prompts"], 5)


if __name__ == "__main__":
    unittest.main()
