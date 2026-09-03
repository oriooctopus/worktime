#!/usr/bin/env python3
"""The one place a displayed clock time is turned back into an instant.

`at` is what the widget subtracts from the current time to draw an age, so a
wrong `at` shows "4h" beside a row whose clock time reads correctly -- the two
halves of one line disagreeing, with nothing to say which is lying. It used to
be built from a naive datetime, whose .timestamp() resolves through the ambient
C-library zone; the sandbox some callers run under denies the zone database and
that fallback answers UTC, so the whole list slid by the UTC offset with no
error anywhere. What is asserted here is that the ambient zone cannot reach it.

Run: pytest tests/test_stamp_epoch.py
"""

import importlib.util
import os
import time
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)


class StampEpoch(unittest.TestCase):
    def test_resolves_through_the_configured_zone(self):
        got = wp.stamp_epoch("2026-09-03", "13:38")
        want = datetime(2026, 9, 3, 13, 38, tzinfo=wp.LOCAL).timestamp()
        self.assertEqual(got, want)

    def test_seconds_on_the_stamp_are_ignored(self):
        self.assertEqual(wp.stamp_epoch("2026-09-03", "13:38:41"),
                         wp.stamp_epoch("2026-09-03", "13:38"))

    def test_ambient_zone_does_not_move_it(self):
        # The sandbox failure mode, reproduced: TZ resolution answering UTC
        # shifted every `at` by the whole offset. It must not any more.
        before = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"
            time.tzset()
            under_utc = wp.stamp_epoch("2026-09-03", "13:38")
        finally:
            if before is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = before
            time.tzset()
        self.assertEqual(under_utc, wp.stamp_epoch("2026-09-03", "13:38"))


if __name__ == "__main__":
    unittest.main()
