#!/usr/bin/env python3
"""Tests for ending the day -- the ⌘⌥S stop and the End Session menu.

Two things are pinned here, and they are the two ways this can quietly go
wrong.

The first is that "end after the last entry" means one minute, not two. The
shift toggle and End Session's second row both offer it, and they are the same
decision made in two places; the day they resolve differently, one of them
starts silently crediting a minute the other does not, and nothing on the
dashboard would say which was right. So both go through last_entry_end and the
test asserts they agree by construction.

The second is that end_session closes more than a mark. A mark is only
sometimes what is holding the day open -- a meeting runs on the calendar's
schedule whether or not anything was marked -- and the whole reason the menu
item exists is to be the honest answer on an afternoon where nothing was
marked at all. A version of it that only ever closed marks would look correct
in every test that had one.

Run: pytest tests/test_end_session.py
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


def at(h, m, s=0):
    return datetime(2026, 3, 4, h, m, s)


class LastEntryEndCase(unittest.TestCase):
    """The minute "end after the last entry" resolves to."""

    def test_empty_day_ends_at_zero(self):
        # Clamped up to each mark's own start by close_open_marks, which closes
        # it zero-length -- crediting nothing for a day the tracker saw nothing
        # in is the honest reading, and marks_for drops the span on end > start.
        self.assertEqual(wp.last_entry_end([]), 0)

    def test_ends_at_the_last_event_not_the_first(self):
        self.assertEqual(wp.last_entry_end([at(9, 5), at(14, 30), at(11, 0)]),
                         14 * 60 + 30)

    def test_carries_the_tail_the_period_model_already_gives(self):
        # A period gets TAIL_SEC on top of its last prompt because sending one
        # is not instantaneous. A stop that ignored it would credit this last
        # minute less than simply walking away does, so the person who pressed
        # the shortcut would come out behind the one who did not.
        self.assertEqual(wp.TAIL_SEC, 20)
        self.assertEqual(wp.last_entry_end([at(14, 29, 50)]), 14 * 60 + 30)

    def test_tail_does_not_move_a_minute_it_cannot_reach(self):
        self.assertEqual(wp.last_entry_end([at(14, 29, 10)]), 14 * 60 + 29)


class EndSessionCase(unittest.TestCase):
    """end_session against a real marks file and a real cut file.

    Points the module's paths at a temp dir rather than stubbing the writers,
    so the JSONL round trip and the cut file's day filter are exercised by the
    same tests. The two halves most likely to drift apart are the writer and
    the reader, and nothing in the menu would show it if they did.
    """

    NAMES = ("STATE", "MARKS", "MEETING_CUT",
             "now_local", "events_for", "write_vault_snapshot")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {name: getattr(wp, name) for name in self.NAMES}
        wp.STATE = self.tmp
        wp.MARKS = os.path.join(self.tmp, "marks.jsonl")
        wp.MEETING_CUT = os.path.join(self.tmp, "meeting-cut.json")
        wp.now_local = lambda: at(16, 40)
        # The day's events and the snapshot write are not what these tests are
        # about, and the real ones read the vault and the transcripts.
        wp.events_for = lambda day: [at(9, 0), at(15, 12, 55)]
        self.snapshots = []
        wp.write_vault_snapshot = lambda day, events: self.snapshots.append(day)

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(wp, name, value)

    def open_mark(self, start_min):
        with open(wp.MARKS, "a") as fh:
            fh.write(json.dumps({"day": DAY, "start": start_min, "end": None,
                                 "note": "pairing", "created": "x"}) + "\n")

    def marks(self):
        return [json.loads(l) for l in open(wp.MARKS) if l.strip()]

    def test_end_now_closes_the_open_mark_at_this_minute(self):
        self.open_mark(15 * 60)
        out = wp.end_session()
        self.assertEqual(out["at"], "16:40")
        self.assertFalse(out["at_last_entry"])
        self.assertEqual(self.marks()[0]["end"], 16 * 60 + 40)

    def test_end_after_last_entry_closes_it_at_the_last_entry(self):
        self.open_mark(15 * 60)
        out = wp.end_session(at_last=True)
        # 15:12:55 + TAIL_SEC lands in 15:13.
        self.assertEqual(out["at"], "15:13")
        self.assertTrue(out["at_last_entry"])
        self.assertEqual(self.marks()[0]["end"], 15 * 60 + 13)

    def test_writes_a_meeting_cut_even_with_nothing_marked(self):
        # The case the menu item exists for: no mark running, the dot green off
        # prompts or a meeting. A version that only closed marks would do
        # nothing at all here and still pass every test that had one.
        out = wp.end_session()
        self.assertEqual(out["closed"], [])
        self.assertEqual(out["cuts"], [16 * 60 + 40])
        self.assertEqual(wp.read_meeting_cuts(), [16 * 60 + 40])

    def test_the_cut_only_truncates_the_meeting_it_lands_inside(self):
        # Ending the day at 16:40 must not reach back and erase the 10:00
        # standup, which is what one cut compared against every meeting did.
        wp.end_session()
        cuts = wp.read_meeting_cuts()
        standup = {"start": 10 * 60, "end": 10 * 60 + 30}
        running = {"start": 16 * 60, "end": 17 * 60}
        self.assertEqual(wp.effective_meeting_end(standup, cuts), 10 * 60 + 30)
        self.assertEqual(wp.effective_meeting_end(running, cuts), 16 * 60 + 40)

    def test_rebuilds_the_snapshot(self):
        # Ending the day changes the total, not just the dot. Leaving it to the
        # next 20-minute check shows a dashboard still counting a day the
        # person just declared over.
        wp.end_session()
        self.assertEqual(self.snapshots, [DAY])


if __name__ == "__main__":
    unittest.main()
