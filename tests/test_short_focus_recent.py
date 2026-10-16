#!/usr/bin/env python3
"""The ignore-glances threshold is customizable and remembers recent picks.

Run: pytest tests/test_short_focus_recent.py
"""

import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)


class ShortFocusRecent(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self._saved = (wp.STATE, wp.SHORT_FOCUS_FILE, wp.SHORT_FOCUS_RECENT_FILE,
                       wp.MIN_PERIOD_FILE, wp.MIN_PERIOD_RECENT_FILE)
        wp.STATE = d
        wp.SHORT_FOCUS_FILE = os.path.join(d, "short-focus.json")
        wp.SHORT_FOCUS_RECENT_FILE = os.path.join(d, "short-focus-recent.json")
        wp.MIN_PERIOD_FILE = os.path.join(d, "min-period.json")
        wp.MIN_PERIOD_RECENT_FILE = os.path.join(d, "min-period-recent.json")

    def tearDown(self):
        (wp.STATE, wp.SHORT_FOCUS_FILE, wp.SHORT_FOCUS_RECENT_FILE,
         wp.MIN_PERIOD_FILE, wp.MIN_PERIOD_RECENT_FILE) = self._saved

    def test_custom_value_is_active_and_remembered(self):
        self.assertEqual(wp.set_short_focus(True, 60), 60)
        self.assertEqual(wp.short_focus_state(), 60)
        self.assertEqual(wp.short_focus_recent(), [60])

    def test_recent_is_most_recent_first_deduped_and_capped(self):
        for v in (10, 20, 30, 40, 50, 60, 20):
            wp.set_short_focus(True, v)
        self.assertEqual(wp.short_focus_recent(), [20, 60, 50, 40, 30])

    def test_off_clears_state_but_keeps_recent(self):
        wp.set_short_focus(True, 45)
        self.assertIsNone(wp.set_short_focus(False))
        self.assertIsNone(wp.short_focus_state())
        self.assertEqual(wp.short_focus_recent(), [45])

    def test_non_positive_threshold_rejected(self):
        with self.assertRaises(SystemExit):
            wp.set_short_focus(True, 0)

    def test_min_period_defaults_then_follows_pick(self):
        self.assertEqual(wp.min_period_state(), 30)
        wp.set_min_period(60)
        self.assertEqual(wp.min_period_state(), 60)
        self.assertEqual(wp._recent(wp.MIN_PERIOD_RECENT_FILE), [60])

    def test_min_period_independent_of_glance_threshold(self):
        wp.set_short_focus(True, 7)
        wp.set_min_period(45)
        self.assertEqual(wp.short_focus_state(), 7)
        self.assertEqual(wp.min_period_state(), 45)
        self.assertEqual(wp.short_focus_recent(), [7])

    def test_non_positive_min_period_rejected(self):
        with self.assertRaises(SystemExit):
            wp.set_min_period(0)


if __name__ == "__main__":
    unittest.main()
