#!/usr/bin/env python3
"""Tests for `track` -- claiming a known number of minutes that are behind you.

⌥W once says "this minute was worked". ⌥W twice asks how many, because the case
it exists for is the phone call that just ended: the length is known, the
minutes are already past, and the one thing the tracker cannot do is see them.

The whole difficulty is that those minutes are not necessarily empty. Sending a
Slack message in the middle of a ten-minute call leaves a period there, and
what should happen to the claim around it is a real choice with two defensible
answers -- so it is asked rather than assumed, and both answers are pinned here.

`clip` stops at that period: five minutes asked for with the last two
unaccounted for banks two, and does not put the other three on top of time that
is already counted. `split` steps over it instead and carries the remainder to
the free minutes in front of it -- two after, three before, the same total in
the holes that actually exist.

Two things are easy to get wrong and are held down hardest. Neither mode may
ever claim a minute that is already counted, because periods are unioned: the
overlap would not add to the day, it would silently swallow part of the claim,
and the minutes that vanished would be exactly the ones the person was trying
to record. And `split` has to keep walking when there is more than one period
in the way -- stopping at the second one would drop the remainder without
saying so.

Run: pytest tests/test_track_back.py
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

DAY = "2026-03-04"


def period(start_hhmm, end_hhmm):
    """A snapshot work period, as write_vault_snapshot publishes them."""
    s, e = wp.to_min(start_hhmm), wp.to_min(end_hhmm)
    return {"start": s, "end": e, "len": e - s}


def spans(worked, now_hhmm, minutes, split):
    got, unplaced = wp.claim_spans(worked, wp.to_min(now_hhmm), minutes, split)
    return [(wp.hhmm_of(a), wp.hhmm_of(b)) for a, b in got], unplaced


class ClaimSpansCase(unittest.TestCase):
    """Where the minutes land, before anything is written."""

    def test_an_empty_stretch_is_claimed_whole(self):
        # Nothing behind you, so nothing to work around: five minutes asked
        # for, five minutes claimed, in one span.
        got, unplaced = spans([], "12:15", 5, split=False)
        self.assertEqual(got, [("12:10", "12:15")])
        self.assertEqual(unplaced, 0)

    def test_clip_stops_at_the_last_tracked_session(self):
        # The case the default exists for. A Slack message two minutes ago left
        # a period there; asking for five banks the two that are free and
        # refuses to invent the other three on top of counted time.
        worked = [period("12:00", "12:13")]
        got, unplaced = spans(worked, "12:15", 5, split=False)
        self.assertEqual(got, [("12:13", "12:15")])
        self.assertEqual(unplaced, 3)

    def test_split_carries_the_remainder_to_before_that_session(self):
        # The same five minutes, the same period, the other answer: two after
        # it and three in front of it, which is where the hole actually is.
        worked = [period("12:00", "12:13")]
        got, unplaced = spans(worked, "12:15", 5, split=True)
        self.assertEqual(got, [("11:57", "12:00"), ("12:13", "12:15")])
        self.assertEqual(unplaced, 0)

    def test_split_keeps_walking_past_a_second_session(self):
        # One period in the way is the case worth describing; a busy afternoon
        # has several. Stopping at the second would drop the remainder with
        # nothing said about it, which is the failure this mode exists to
        # avoid in the first place.
        worked = [period("11:50", "11:58"), period("12:00", "12:13")]
        got, unplaced = spans(worked, "12:15", 6, split=True)
        self.assertEqual(got, [("11:48", "11:50"), ("11:58", "12:00"),
                               ("12:13", "12:15")])
        self.assertEqual(unplaced, 0)

    def test_neither_mode_ever_claims_a_counted_minute(self):
        # The invariant behind both. Periods are unioned, so an overlapping
        # claim would not lengthen the day -- it would quietly shorten the
        # claim, and the minutes that went missing would be the ones being
        # recorded.
        worked = [period("11:50", "11:58"), period("12:00", "12:13")]
        counted = {m for p in worked for m in range(p["start"], p["end"])}
        for split in (False, True):
            got, _ = wp.claim_spans(worked, wp.to_min("12:15"), 20, split)
            claimed = {m for a, b in got for m in range(a, b)}
            self.assertEqual(claimed & counted, set(),
                             f"split={split} claimed counted minutes")

    def test_a_live_session_leaves_nothing_to_claim_under_clip(self):
        # Still prompting: the newest period runs to now, or past it -- its end
        # carries TAIL_SEC beyond the last event. There is no free minute
        # behind the cursor at all, and clip stops rather than reaching over a
        # session that has not finished.
        worked = [period("12:00", "12:16")]
        got, unplaced = spans(worked, "12:15", 5, split=False)
        self.assertEqual(got, [])
        self.assertEqual(unplaced, 5)

    def test_split_reaches_over_a_live_session(self):
        # The same clock and the same period under the other rule: the minutes
        # go in front of the session that is still running.
        worked = [period("12:00", "12:16")]
        got, unplaced = spans(worked, "12:15", 5, split=True)
        self.assertEqual(got, [("11:55", "12:00")])
        self.assertEqual(unplaced, 0)

    def test_the_claim_stops_at_midnight(self):
        # Rather than running backwards into a day that is already summarised
        # and closed, where nobody would look for the minutes again.
        got, unplaced = spans([], "00:03", 10, split=True)
        self.assertEqual(got, [("00:00", "00:03")])
        self.assertEqual(unplaced, 7)

    def test_periods_after_now_are_not_in_the_way(self):
        # A snapshot can carry a period ahead of the clock. It is not behind
        # the cursor, so it cannot block or be stepped over.
        worked = [period("13:00", "13:30")]
        got, unplaced = spans(worked, "12:15", 5, split=False)
        self.assertEqual(got, [("12:10", "12:15")])
        self.assertEqual(unplaced, 0)


class TrackBackCase(unittest.TestCase):
    """What the command actually writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marks = os.path.join(self.tmp.name, "marks.jsonl")
        self.snap = os.path.join(self.tmp.name, "snap.json")

        for name, value in (("MARKS", self.marks), ("STATE", self.tmp.name)):
            orig = getattr(wp, name)
            setattr(wp, name, value)
            self.addCleanup(lambda n=name, o=orig: setattr(wp, n, o))

        self.now = datetime(2026, 3, 4, 12, 15)
        self.written = []
        self.patch("now_local", lambda: self.now)
        self.patch("events_for", lambda day: [])
        self.patch("snapshot_path", lambda day: self.snap)
        self.patch("activity_fingerprint", lambda day: "fp")
        self.patch("write_vault_snapshot",
                   lambda day, events: self.written.append(day))

    def patch(self, name, fn):
        orig = getattr(wp, name)
        setattr(wp, name, fn)
        self.addCleanup(lambda: setattr(wp, name, orig))

    def snapshot(self, worked):
        with open(self.snap, "w") as fh:
            json.dump({"fp": "fp", "worked": worked}, fh)

    def marks_written(self):
        if not os.path.exists(self.marks):
            return []
        return [json.loads(l) for l in open(self.marks) if l.strip()]

    def test_writes_one_closed_mark_per_span(self):
        # Closed, not open. The stretch being claimed is over -- that is why it
        # needs claiming by hand -- and an open mark would go on crediting
        # minutes forward from a call that has already ended.
        self.snapshot([])
        out = wp.track_back(5)
        self.assertTrue(out["tracked"])
        self.assertEqual(out["claimed"], 5)
        marks = self.marks_written()
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["start"], wp.to_min("12:10"))
        self.assertEqual(marks[0]["end"], wp.to_min("12:15"))
        self.assertEqual(marks[0]["note"], wp.TRACK_NOTE)

    def test_split_writes_both_halves(self):
        self.snapshot([period("12:00", "12:13")])
        out = wp.track_back(5, "split")
        self.assertEqual(out["claimed"], 5)
        self.assertEqual([(m["start"], m["end"]) for m in self.marks_written()],
                         [(wp.to_min("11:57"), wp.to_min("12:00")),
                          (wp.to_min("12:13"), wp.to_min("12:15"))])

    def test_clip_reports_the_shortfall_rather_than_hiding_it(self):
        # Banking two minutes when five were asked for is the ordinary outcome
        # of the default rule, not an error -- but it is invisible in the
        # period list, so the answer has to carry it or the menu bar cannot
        # say it either.
        self.snapshot([period("12:00", "12:13")])
        out = wp.track_back(5)
        self.assertEqual(out["claimed"], 2)
        self.assertEqual(out["unplaced"], 3)

    def test_a_fully_counted_stretch_writes_nothing(self):
        # Nothing to record, so nothing is recorded. A zero-minute mark would
        # be a row in the day saying work happened that this very command just
        # established was already accounted for.
        self.snapshot([period("12:00", "12:16")])
        out = wp.track_back(5)
        self.assertFalse(out["tracked"])
        self.assertEqual(out["claimed"], 0)
        self.assertEqual(self.marks_written(), [])
        self.assertEqual(self.written, [])

    def test_republishes_so_the_minutes_appear_in_the_list_being_looked_at(self):
        # Same reason as every other mutating command: the person is looking at
        # the day right now, and waiting for the next check would show them a
        # total that does not include what they just recorded.
        self.snapshot([])
        wp.track_back(5)
        self.assertIn(DAY, self.written)

    def test_refuses_a_mode_it_does_not_know(self):
        # The mode comes off a picker, and a typo'd name silently falling back
        # to clip would bank a different rule than the one the row named.
        self.snapshot([period("12:00", "12:13")])
        out = wp.track_back(5, "before")
        self.assertFalse(out["tracked"])
        self.assertEqual(self.marks_written(), [])

    def test_refuses_a_count_that_is_not_a_stretch(self):
        self.snapshot([])
        for n in (0, -5):
            self.assertFalse(wp.track_back(n)["tracked"])
        self.assertEqual(self.marks_written(), [])

    def test_the_marks_resolve_to_the_spans_that_were_promised(self):
        # The answer names the spans, and marks_for is what the day is actually
        # built from -- so the two have to agree. A closed mark is honoured
        # as written, which is the property this depends on.
        self.snapshot([period("12:00", "12:13")])
        out = wp.track_back(5, "split")
        resolved = [(wp.hhmm_of(m["start"]), wp.hhmm_of(m["end"]))
                    for m in wp.marks_for(DAY, stamps=[])]
        self.assertEqual(resolved,
                         [(s["start"], s["end"]) for s in out["spans"]])


if __name__ == "__main__":
    unittest.main()
