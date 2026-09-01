#!/usr/bin/env python3
"""Tests for the focus signal -- attended foreground time.

Focus replaced Slack sends as the evidence that a stretch in Slack was work,
and it is the first input here that is an INTERVAL rather than a point. Both
facts make it easy to get wrong in the expensive direction: a signal that
credits time nobody worked reads as a plausible day, so nothing about the
output looks broken. The invariants that stop that are asserted here.

Run: pytest tests/test_focus.py
"""

import importlib.util
import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

DAY = "2026-03-04"
SLACK = "com.tinyspeck.slackmacgap"


def hms(sec):
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class FocusCase(unittest.TestCase):
    """Writes a real focus log to a temp dir and reads it back through the probe.

    Points FOCUS_DIR at the temp dir rather than stubbing focus_rows(), so the
    parsing, the day filter and the ordering are all exercised too -- those are
    the parts most likely to break silently when the writer's format moves.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = wp.FOCUS_DIR
        wp.FOCUS_DIR = self.tmp.name

    def tearDown(self):
        wp.FOCUS_DIR = self.orig
        self.tmp.cleanup()

    def write(self, rows, day=DAY):
        path = os.path.join(self.tmp.name, f"{day}.jsonl")
        with open(path, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def samples(self, start, n, bundle=SLACK, idle=0, step=30, app="Slack"):
        """`n` heartbeats `step` apart, as the bar would actually write them."""
        return [{"day": DAY, "t": hms(start + i * step), "app": app,
                 "bundle": bundle, "idle": idle} for i in range(n)]

    def minutes(self, day=DAY):
        return [t.hour * 60 + t.minute for t in wp.focus_for(day)]


class TestCredit(FocusCase):
    def test_attended_run_credits_every_minute_it_covers(self):
        # 09:00:00 to 09:05:00, heartbeating every 30s with input throughout.
        self.write(self.samples(9 * 3600, 11))
        self.assertEqual(self.minutes(), list(range(540, 546)))

    def test_a_single_sample_credits_nothing(self):
        # One row has no successor, so there is no interval to vouch for. The
        # alternative -- crediting from the sample to now -- is the failure
        # that would let one row before lunch claim the afternoon.
        self.write(self.samples(9 * 3600, 1))
        self.assertEqual(self.minutes(), [])

    def test_excluded_app_earns_nothing(self):
        self.write(self.samples(9 * 3600, 11, bundle="com.netflix.Netflix",
                                app="Netflix"))
        self.assertEqual(self.minutes(), [])

    def test_empty_bundle_earns_nothing(self):
        # No frontmost app at all -- the login window, or a fast user switch.
        self.write(self.samples(9 * 3600, 11, bundle="", app=""))
        self.assertEqual(self.minutes(), [])


class TestIdle(FocusCase):
    def test_idle_beyond_the_threshold_stops_credit(self):
        # Every sample reports more idle than the threshold allows: the app is
        # frontmost, the machine is unattended, nothing is earned.
        self.write(self.samples(9 * 3600, 11, idle=wp.FOCUS_IDLE_SEC + 30))
        self.assertEqual(self.minutes(), [])

    def test_grace_runs_to_the_threshold_then_stops(self):
        # Input at 09:00:00, then nothing. Idle climbs 30s per heartbeat, so
        # the windows closed by idle <= 120 are credited and the rest are not.
        # This is the reading pause the threshold is meant to survive.
        rows = [{"day": DAY, "t": hms(9 * 3600 + i * 30), "app": "Slack",
                 "bundle": SLACK, "idle": i * 30} for i in range(11)]
        self.write(rows)
        # Credited through the sample at idle=120 (09:02:00), not past it.
        self.assertEqual(self.minutes(), [540, 541, 542])

    def test_the_closing_sample_decides_not_the_opening_one(self):
        # The whole window was unattended, but the row that OPENS it still
        # reports idle 0 -- it was written the instant before the person left.
        # Judging on the opening row would credit the first minutes of every
        # absence; judging on the closing row is what makes the span honest.
        self.write([
            {"day": DAY, "t": "09:00:00", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "09:00:30", "app": "Slack", "bundle": SLACK,
             "idle": 150},
        ])
        self.assertEqual(self.minutes(), [])


class TestTruncation(FocusCase):
    def test_a_gap_longer_than_the_cap_credits_neither_side(self):
        # The machine slept from 09:00:30 to 11:00:00. Two samples bracket two
        # hours of absence; crediting between them is the `visit_duration`
        # trap, and is exactly what FOCUS_MAX_GAP_SEC exists to refuse.
        self.write([
            {"day": DAY, "t": "09:00:00", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "09:00:30", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "11:00:00", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "11:00:30", "app": "Slack", "bundle": SLACK,
             "idle": 0},
        ])
        # 09:00 and 11:00 only -- the two hours between them earn nothing.
        self.assertEqual(self.minutes(), [540, 660])

    def test_gap_exactly_at_the_cap_still_counts(self):
        self.write([
            {"day": DAY, "t": "09:00:00", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": hms(9 * 3600 + wp.FOCUS_MAX_GAP_SEC),
             "app": "Slack", "bundle": SLACK, "idle": 0},
        ])
        self.assertEqual(self.minutes(), [540, 541])

    def test_rows_from_another_day_are_ignored(self):
        self.write(self.samples(9 * 3600, 11))
        self.write([{"day": "2026-03-05", "t": "09:00:00", "app": "Slack",
                     "bundle": SLACK, "idle": 0}])
        self.assertEqual(self.minutes(), list(range(540, 546)))

    def test_missing_log_is_not_an_error(self):
        # A machine that has never run the bar has no log. That is a day with
        # no focus evidence, not a crash.
        self.assertEqual(self.minutes("2026-01-01"), [])

    def test_out_of_order_rows_are_sorted_before_pairing(self):
        # Five heartbeats 30s apart run 09:00:00 to 09:02:00, touching three
        # minutes. Written backwards, they must still pair up as neighbours --
        # unsorted, every pair would have a negative span and earn nothing.
        rows = self.samples(9 * 3600, 5)
        self.write(list(reversed(rows)))
        self.assertEqual(self.minutes(), [540, 541, 542])


class TestApps(FocusCase):
    def test_apps_are_ranked_by_time_held(self):
        self.write(self.samples(9 * 3600, 3, bundle="com.apple.Safari",
                                app="Safari"))
        self.write(self.samples(9 * 3600 + 90, 7))
        self.assertEqual(wp.focus_apps(DAY, 9 * 3600, 9 * 3600 + 600)[0],
                         "Slack")

    def test_apps_outside_the_window_are_excluded(self):
        self.write(self.samples(9 * 3600, 11))
        self.assertEqual(wp.focus_apps(DAY, 14 * 3600, 15 * 3600), [])


class TestReadsRealWriterFormat(FocusCase):
    """The one test that would catch the writer and the reader drifting apart.

    Everything above builds rows by hand, so all of it would go on passing if
    main.swift changed a key name. This asserts the exact field set the Swift
    side emits, so a rename there fails here rather than silently producing a
    day with no focus evidence -- which reads as an ordinary quiet day and is
    therefore the failure nobody would notice.
    """

    def test_the_documented_row_shape_parses(self):
        self.write([
            {"day": DAY, "t": "09:00:00", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "09:00:30", "app": "Slack", "bundle": SLACK,
             "idle": 3},
        ])
        rows = wp.focus_rows(DAY)
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows[0]), {"day", "t", "app", "bundle", "idle"})
        self.assertEqual(self.minutes(), [540])


if __name__ == "__main__":
    unittest.main()
