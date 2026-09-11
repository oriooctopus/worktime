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

    def export(self, exported_at):
        # New layout: freshness is a single vault-wide _status.md (see
        # activity-export.py's maybe_write_status), not a per-day file --
        # a day's own chunks carry no per-run timestamp at all any more.
        with open(os.path.join(self.tmp, "_status.md"), "w") as fh:
            fh.write(f"---\nexported_at: {exported_at}\nerrors: []\n---\n")

    def test_fresh_export_says_nothing(self):
        self.export("2026-09-09T15:01:00-04:00")
        self.assertIsNone(wp.activity_feed_notice(self.now))

    def test_days_old_export_names_the_date(self):
        self.export("2026-09-06T13:13:53-04:00")
        self.assertEqual(wp.activity_feed_notice(self.now),
                         "Desktop activity feed silent since Sep 6 13:13")

    def test_same_day_lapse_names_only_the_time(self):
        self.export("2026-09-09T12:40:00-04:00")
        self.assertEqual(wp.activity_feed_notice(self.now),
                         "Desktop activity feed silent since 12:40")

    def test_no_exports_at_all(self):
        self.assertEqual(wp.activity_feed_notice(self.now),
                         "Desktop activity feed: no exports yet")

    def test_59_minutes_stale_says_nothing(self):
        # ACTIVITY_STALE_SEC is 1 hour; 59 minutes must stay under the "silent
        # since" notice -- boundary case for the "<=" comparison (a mutation
        # to "<" would make this fire a false notice one second early).
        self.export("2026-09-09T14:06:00-04:00")  # 59 min before self.now
        self.assertIsNone(wp.activity_feed_notice(self.now))

    def test_61_minutes_stale_names_the_time(self):
        # One minute past the boundary must produce the notice.
        self.export("2026-09-09T14:04:00-04:00")  # 61 min before self.now
        self.assertEqual(wp.activity_feed_notice(self.now),
                         "Desktop activity feed silent since 14:04")

    def test_exactly_60_minutes_stale_says_nothing(self):
        # Exactly ACTIVITY_STALE_SEC old: the "<=" comparison means this is
        # still "fresh enough" and must NOT produce a notice. A mutation
        # from "<=" to "<" would flip this one test (and only this one) to
        # a false notice, which is why it's asserted at the exact boundary
        # rather than only just inside/outside it.
        self.export("2026-09-09T14:05:00-04:00")  # exactly 60 min before self.now
        self.assertIsNone(wp.activity_feed_notice(self.now))

    def test_status_without_exported_at_is_a_distinct_notice(self):
        # A malformed/missing _status.md is a louder, different problem than
        # "never ran" -- it must not crash the probe (StopIteration, as the
        # old generated_at-scanning code would have) nor silently read fresh.
        with open(os.path.join(self.tmp, "_status.md"), "w") as fh:
            fh.write("---\ndate: 2026-09-09\n---\n")
        self.assertEqual(wp.activity_feed_notice(self.now),
                         "Desktop activity feed: _status.md is malformed")


if __name__ == "__main__":
    unittest.main()
