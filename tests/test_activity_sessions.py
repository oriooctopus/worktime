#!/usr/bin/env python3
"""Tests for the grouped ("sessions") reading of the activity list.

The grouped view and the raw list are two renderings of one set of rows, and
the whole claim the toggle makes is that they are the same day seen twice.
That claim breaks quietly: a row that lands in no group, or a count that adds
up to fewer events than the list holds, still produces a menu that looks
perfectly reasonable. So what is asserted here is conservation -- every row
appears exactly once, and the totals match -- plus the boundary rule, that a
session's span is a period's span rather than a second guess at one.

Run: pytest tests/test_activity_sessions.py
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)


def row(t, kind="prompt", what="x", n=1):
    return {"t": t, "kind": kind, "what": what, "n": n}


def period(start, end, what="", length=None):
    return {"start": start, "end": end,
            "len": end - start if length is None else length, "what": what}


class GroupSessions(unittest.TestCase):
    def test_rows_in_one_period_make_one_session(self):
        rows = [row("09:20"), row("09:15"), row("09:10")]
        out = wp.group_sessions(rows, [period(540, 570, "review")])
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["start"], out[0]["end"]), (540, 570))
        self.assertEqual(out[0]["n"], 3)
        self.assertEqual(out[0]["what"], "review")
        self.assertTrue(out[0]["counted"])

    def test_session_span_is_the_periods_span_not_the_rows(self):
        # The period ran 09:00-09:30; the only evidence in it landed at 09:10.
        # The session has to report the period's half hour, or the grouped view
        # and the period list would give two different answers for one stretch.
        out = wp.group_sessions([row("09:10")], [period(540, 570)])
        self.assertEqual((out[0]["start"], out[0]["end"]), (540, 570))
        self.assertEqual(out[0]["len"], 30)

    def test_separate_periods_stay_separate(self):
        rows = [row("14:05"), row("09:10")]
        out = wp.group_sessions(rows, [period(540, 570), period(840, 850)])
        self.assertEqual(len(out), 2)
        # Newest first, matching the raw list and the period list.
        self.assertEqual(out[0]["start"], 840)
        self.assertEqual(out[1]["start"], 540)

    def test_current_marks_only_the_days_last_period(self):
        rows = [row("14:05"), row("09:10")]
        out = wp.group_sessions(rows, [period(540, 570), period(840, 850)])
        self.assertTrue(out[0]["current"])
        self.assertFalse(out[1]["current"])

    def test_kinds_are_tallied_biggest_first(self):
        rows = [row("09:20", "prompt"), row("09:19", "browsing", n=4),
                row("09:18", "prompt", what="y")]
        out = wp.group_sessions(rows, [period(540, 570)])
        self.assertEqual(out[0]["kinds"], [("browsing", 4), ("prompt", 2)])
        # n counts events, not rows: a row the probe collapsed stands for
        # several, and reporting rows would undercount exactly the stretches
        # where something was done repeatedly.
        self.assertEqual(out[0]["n"], 6)

    def test_rows_outside_every_period_survive_as_uncounted(self):
        rows = [row("09:10"), row("04:00", "approval")]
        out = wp.group_sessions(rows, [period(540, 570)])
        self.assertEqual(len(out), 2)
        stray = out[1]
        self.assertFalse(stray["counted"])
        self.assertEqual((stray["start"], stray["end"]), (240, 240))
        # No length, because none of it was credited.
        self.assertEqual(stray["len"], 0)

    def test_uncounted_rows_far_apart_do_not_merge(self):
        rows = [row("11:00", "approval"), row("04:00", "approval")]
        out = wp.group_sessions(rows, [])
        self.assertEqual(len(out), 2)

    def test_uncounted_rows_close_together_do_merge(self):
        rows = [row("04:02", "approval"), row("04:00", "approval")]
        out = wp.group_sessions(rows, [])
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["start"], out[0]["end"]), (240, 242))

    def test_every_event_is_conserved(self):
        rows = [row("14:05", "prompt", n=2), row("09:20", "slack"),
                row("09:10", "focus", n=3), row("04:00", "approval")]
        out = wp.group_sessions(rows, [period(540, 570), period(840, 850)])
        self.assertEqual(sum(s["n"] for s in out),
                         sum(r["n"] for r in rows))
        for s in out:
            self.assertEqual(sum(n for _, n in s["kinds"]), s["n"])

    def test_limit_keeps_the_newest(self):
        rows = [row(f"{h:02d}:00") for h in range(20, 8, -1)]
        periods = [period(h * 60, h * 60 + 5) for h in range(9, 21)]
        out = wp.group_sessions(rows, periods, limit=3)
        self.assertEqual([s["start"] for s in out], [1200, 1140, 1080])

    def test_no_rows_is_no_sessions(self):
        self.assertEqual(wp.group_sessions([], [period(540, 570)]), [])

    def test_a_session_carries_its_own_rows(self):
        rows = [row("09:20", "prompt", "b"), row("09:10", "slack", "a")]
        out = wp.group_sessions(rows, [period(540, 570)])
        self.assertEqual([r["t"] for r in out[0]["rows"]], ["09:20", "09:10"])
        self.assertEqual(out[0]["rows"][1]["kind"], "slack")

    def test_carried_rows_are_capped_but_the_count_is_not(self):
        rows = [row(f"09:{m:02d}", what=str(m)) for m in range(59, 0, -1)]
        out = wp.group_sessions(rows, [period(540, 600)])
        self.assertEqual(len(out[0]["rows"]), wp.SESSION_TIP_N)
        # The cap is on what travels, not on what is reported: the widget
        # subtracts one from the other to say how many it could not show, so a
        # capped `n` would make it claim the session held only twelve.
        self.assertEqual(out[0]["n"], len(rows))
        # Newest first, so what a hover drops is the oldest end of the session
        # rather than the part being asked about.
        self.assertEqual(out[0]["rows"][0]["t"], "09:59")

    def test_carried_rows_keep_their_collapsed_count(self):
        out = wp.group_sessions([row("09:10", "browsing", "pr", n=4)],
                                [period(540, 570)])
        self.assertEqual(out[0]["rows"][0]["n"], 4)

    def test_no_private_keys_leak_into_the_payload(self):
        out = wp.group_sessions([row("09:10")], [period(540, 570)])
        self.assertNotIn("_idx", out[0])


if __name__ == "__main__":
    unittest.main()
