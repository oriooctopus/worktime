#!/usr/bin/env python3
"""The desktop activity export going quiet shows up in the menu, not the dot.

Run: pytest tests/test_activity_feed_notice.py
"""

import importlib.util
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


class FeedNotice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = wp.ACTIVITY_DIR
        wp.ACTIVITY_DIR = self.tmp
        self.now = datetime(2026, 9, 9, 15, 5, tzinfo=wp.LOCAL)

    def tearDown(self):
        wp.ACTIVITY_DIR = self.saved
        shutil.rmtree(self.tmp)

    def export(self, day, generated_at):
        with open(os.path.join(self.tmp, f"{day}.md"), "w") as fh:
            fh.write(f"---\ndate: {day}\ngenerated_at: {generated_at}\n---\n")

    def test_fresh_export_says_nothing(self):
        self.export("2026-09-09", "2026-09-09T15:01:00-04:00")
        self.assertIsNone(wp.activity_feed_notice(self.now))

    def test_days_old_export_names_the_date(self):
        self.export("2026-09-05", "2026-09-05T23:56:00-04:00")
        self.export("2026-09-06", "2026-09-06T13:13:53-04:00")
        self.assertEqual(wp.activity_feed_notice(self.now),
                         "Desktop activity feed silent since Sep 6 13:13")

    def test_same_day_lapse_names_only_the_time(self):
        self.export("2026-09-09", "2026-09-09T12:40:00-04:00")
        self.assertEqual(wp.activity_feed_notice(self.now),
                         "Desktop activity feed silent since 12:40")

    def test_no_exports_at_all(self):
        self.assertEqual(wp.activity_feed_notice(self.now),
                         "Desktop activity feed: no exports yet")

    def test_export_without_generated_at_is_an_error(self):
        with open(os.path.join(self.tmp, "2026-09-09.md"), "w") as fh:
            fh.write("---\ndate: 2026-09-09\n---\n")
        with self.assertRaises(StopIteration):
            wp.activity_feed_notice(self.now)


if __name__ == "__main__":
    unittest.main()
