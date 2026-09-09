#!/usr/bin/env python3
"""Tests for "Link with Last Session" -- claiming the gap behind you as work.

The menu item's counterpart to End Session: that one declares a stretch over,
this declares it never stopped. A step away the tracker saw nothing in comes
back as two sessions with a hole between them, and `mark` could only ever claim
from the click forward -- never the stretch already behind it, which is the
only part that needs claiming.

Two things are pinned here, and they are the two ways this goes quietly wrong.

The first is which period counts as "the last session". When something is live,
the newest period IS the current one and linking has to reach past it to the
period before, so the claim covers the gap and the two merge. When nothing is
live the newest period is itself the last one. Get that backwards while a
session is running and the item claims zero minutes and appears to do nothing.

The second is that the menu and the action must not disagree about the minute.
The title names how far back the claim reaches, and it is a claim on time that
cannot be watched being made -- so if status() and link_last_session() derived
the anchor separately, the row could advertise one minute and bank another.
They share link_anchor, and the test holds them to it.

Run: pytest tests/test_link_last_session.py
"""

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

DAY = "2026-03-04"
CUTOFF = 5 * 60  # the focused-mode cutoff, in seconds


def period(start_hhmm, end_hhmm):
    """A snapshot work period, as write_vault_snapshot publishes them."""
    s, e = wp.to_min(start_hhmm), wp.to_min(end_hhmm)
    return {"start": s, "end": e, "len": e - s}


class LinkAnchorCase(unittest.TestCase):
    """Which minute the claim reaches back to."""

    def test_empty_day_has_nothing_to_link_to(self):
        # The ordinary state of the first minute of the morning, not an error.
        # The item greys out rather than disappearing.
        self.assertIsNone(wp.link_anchor([], wp.to_min("09:00"), CUTOFF))

    def test_links_from_the_last_period_when_nothing_is_live(self):
        # Back at the desk after a stretch the tracker saw nothing in: the
        # newest period is the last session, and the claim carries it to now.
        worked = [period("09:00", "10:30"), period("11:00", "11:40")]
        self.assertEqual(wp.link_anchor(worked, wp.to_min("12:15"), CUTOFF),
                         wp.to_min("11:40"))

    def test_reaches_past_the_current_period_when_one_is_live(self):
        # Prompting right now, so the newest period IS the current session.
        # Linking has to reach past it to 10:30 -- claiming from 12:13 would
        # cover no gap at all and the two sessions would stay two.
        worked = [period("09:00", "10:30"), period("12:10", "12:13")]
        self.assertEqual(wp.link_anchor(worked, wp.to_min("12:15"), CUTOFF),
                         wp.to_min("10:30"))

    def test_a_live_period_that_is_the_only_one_has_nothing_behind_it(self):
        # The first session of the day, still running. Reaching past it leaves
        # nothing, and there is no earlier work to rejoin.
        worked = [period("09:00", "09:20")]
        self.assertIsNone(wp.link_anchor(worked, wp.to_min("09:22"), CUTOFF))

    def test_liveness_is_decided_by_the_cutoff_not_by_being_last(self):
        # The same two periods and the same clock, read under two cutoffs. At
        # five minutes the newest period is still live and the anchor reaches
        # past it; at one minute it has lapsed and is itself the last session.
        # This is the whole reason the cutoff is passed in rather than assumed:
        # unfocused mode grants a lone prompt a single minute.
        worked = [period("09:00", "10:30"), period("12:10", "12:13")]
        now = wp.to_min("12:15")
        self.assertEqual(wp.link_anchor(worked, now, 5 * 60), wp.to_min("10:30"))
        self.assertEqual(wp.link_anchor(worked, now, 1 * 60), wp.to_min("12:13"))

    def test_a_running_mark_only_hides_the_item_if_it_covers_the_anchor(self):
        # A mark begun at or before the anchor already holds those minutes; one
        # begun after it holds the stretch in front and leaves the gap behind
        # it exactly as unclaimed as if no mark were running at all.
        worked = [period("09:00", "10:30")]
        now = wp.to_min("12:15")
        self.assertIsNone(
            wp.link_anchor(worked, now, CUTOFF, wp.to_min("10:30")))
        self.assertIsNone(
            wp.link_anchor(worked, now, CUTOFF, wp.to_min("09:30")))
        self.assertEqual(
            wp.link_anchor(worked, now, CUTOFF, wp.to_min("12:05")),
            wp.to_min("10:30"))

    def test_never_starts_in_the_future(self):
        # A period's end carries TAIL_SEC past its last event, so it can land a
        # minute ahead of the clock -- and a claim starting after the minute it
        # was written in would be claiming time that has not happened. It never
        # is, and no clamp enforces that: a period ending past now is live by
        # the cutoff test at any cutoff, so it is the one reached past rather
        # than the one anchored on.
        worked = [period("09:00", "10:00"), period("12:00", "12:16")]
        now = wp.to_min("12:15")
        for cutoff in (0, 1 * 60, 5 * 60):
            self.assertEqual(wp.link_anchor(worked, now, cutoff),
                             wp.to_min("10:00"))


class LinkLastSessionCase(unittest.TestCase):
    """What the action actually writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marks = os.path.join(self.tmp.name, "marks.jsonl")
        self.snap = os.path.join(self.tmp.name, "snap.json")

        self.orig = {"MARKS": wp.MARKS, "STATE": wp.STATE}
        wp.MARKS, wp.STATE = self.marks, self.tmp.name
        self.addCleanup(lambda: setattr(wp, "MARKS", self.orig["MARKS"]))
        self.addCleanup(lambda: setattr(wp, "STATE", self.orig["STATE"]))

        self.now = datetime(2026, 3, 4, 12, 15)
        self.written = []
        self.patch("now_local", lambda: self.now)
        self.patch("events_for", lambda day: [])
        self.patch("live_activity", lambda day: (None, [], [], []))
        self.patch("live_cutoff", lambda day, stamps: CUTOFF)
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

    def running_mark(self, start):
        """Pretend a still-open mark began at `start` and reaches past now."""
        self.patch("marks_for", lambda day, stamps=None: [
            {"start": wp.to_min(start), "end": wp.to_min("12:30"),
             "note": "", "open": True}])

    def marks_written(self):
        if not os.path.exists(self.marks):
            return []
        return [json.loads(l) for l in open(self.marks) if l.strip()]

    def test_writes_an_open_mark_spanning_the_gap(self):
        # The point of the whole item: the claim starts in the past, where the
        # last session ended, and has no end -- the stretch it rejoined is
        # still going, and closing it at the click would end the day in the act
        # of extending it.
        self.snapshot([period("09:00", "10:30"), period("11:00", "11:40")])
        out = wp.link_last_session()
        self.assertTrue(out["linked"])
        self.assertEqual(out["from"], "11:40")
        self.assertEqual(out["gap"], 35)
        marks = self.marks_written()
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["start"], wp.to_min("11:40"))
        self.assertIsNone(marks[0]["end"])
        self.assertEqual(marks[0]["note"], wp.LINK_NOTE)

    def test_claims_from_before_the_current_session_when_one_is_live(self):
        # Reaching past the live period is what makes the two merge; a claim
        # starting at 12:13 would cover nothing.
        self.snapshot([period("09:00", "10:30"), period("12:10", "12:13")])
        out = wp.link_last_session()
        self.assertEqual(out["from"], "10:30")
        self.assertEqual(self.marks_written()[0]["start"], wp.to_min("10:30"))

    def test_refuses_when_the_running_mark_already_covers_the_anchor(self):
        # Two open marks was a real bug the day marks shipped, and a mark that
        # began before the last session ended is already holding every minute
        # the link would claim. Nothing to add.
        self.snapshot([period("09:00", "10:30")])
        self.running_mark("10:00")
        out = wp.link_last_session()
        self.assertFalse(out["linked"])
        self.assertEqual(self.marks_written(), [])

    def test_fills_the_gap_behind_a_mark_that_started_after_the_anchor(self):
        # The order the two get clicked in: back at the desk, marked as
        # working, then reach for the link. The mark holds the stretch in front
        # of it; the minutes behind it are still the hole that needed claiming,
        # and refusing here made the item a no-op in its commonest case.
        self.snapshot([period("09:00", "10:30")])
        self.running_mark("12:05")
        out = wp.link_last_session()
        self.assertTrue(out["linked"])
        self.assertEqual((out["from"], out["to"]), ("10:30", "12:05"))
        marks = self.marks_written()
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["start"], wp.to_min("10:30"))
        # Closed, and closed where the running mark begins: the day is already
        # held open by that one, and a second open mark beside it is the bug.
        self.assertEqual(marks[0]["end"], wp.to_min("12:05"))

    def test_the_menu_greys_out_exactly_when_the_action_would_refuse(self):
        # The two disagreeing is the whole failure being fixed: the item was
        # offered, named a minute, and banked nothing.
        worked = [period("09:00", "10:30")]
        for mark, offered in (("10:00", False), ("12:05", True)):
            with self.subTest(mark=mark):
                self.snapshot(worked)
                self.running_mark(mark)
                advertised = wp.link_anchor(worked, wp.to_min("12:15"), CUTOFF,
                                            wp.to_min(mark))
                self.assertEqual(advertised is not None, offered)
                self.assertEqual(wp.link_last_session()["linked"], offered)

    def test_refuses_on_an_empty_day_without_writing_anything(self):
        self.snapshot([])
        out = wp.link_last_session()
        self.assertFalse(out["linked"])
        self.assertEqual(self.marks_written(), [])

    def test_republishes_so_the_gap_closes_in_the_list_being_looked_at(self):
        # The person clicked this because they can see the gap. Waiting for the
        # next check would leave them staring at it.
        self.snapshot([period("09:00", "10:30"), period("11:00", "11:40")])
        wp.link_last_session()
        self.assertIn(DAY, self.written)

    def test_the_menu_and_the_action_name_the_same_minute(self):
        # The title advertises how far back the claim reaches, and the claim
        # cannot be watched being made -- so the row must not be able to say
        # one minute and bank another. Both go through link_anchor.
        worked = [period("09:00", "10:30"), period("11:00", "11:40")]
        self.snapshot(worked)
        advertised = wp.link_anchor(worked, wp.to_min("12:15"), CUTOFF)
        self.assertEqual(wp.link_last_session()["from"], wp.hhmm_of(advertised))


class OpenMarkCeilingCase(unittest.TestCase):
    """Where the 30-minute ceiling on an open mark is measured from.

    From the minute the mark was MADE, not the minute it starts. The two are
    the same for a mark claiming time from the click forward, so this is
    invisible in the ordinary case -- and decisive for a mark that starts in
    the past, which is what a link is and what `mark HH:MM` has always been.
    Measured from the start, the allowance is spent on minutes that had already
    elapsed before the mark existed, and the claim falls short of the moment it
    was asked to reach.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        orig = wp.MARKS
        wp.MARKS = os.path.join(self.tmp.name, "marks.jsonl")
        self.addCleanup(lambda: setattr(wp, "MARKS", orig))
        self.now = datetime(2026, 3, 4, 12, 15)
        n = wp.now_local
        wp.now_local = lambda: self.now
        self.addCleanup(lambda: setattr(wp, "now_local", n))

    def write(self, start_hhmm, created_hhmm):
        made = datetime(2026, 3, 4, *[int(x) for x in created_hhmm.split(":")])
        with open(wp.MARKS, "w") as fh:
            fh.write(json.dumps({"day": DAY, "start": wp.to_min(start_hhmm),
                                 "end": None, "note": "",
                                 "created": made.isoformat()}) + "\n")

    def test_a_mark_made_where_it_starts_still_expires_after_thirty(self):
        # Unchanged behaviour, and the reason the ceiling exists: one clicked
        # and forgotten must not credit the whole absence.
        self.write("11:00", "11:00")
        self.assertEqual(wp.marks_for(DAY, stamps=[])[0]["end"],
                         wp.to_min("11:30"))

    def test_a_backdated_mark_gets_its_thirty_from_when_it_was_made(self):
        # Made at 12:15 claiming back to 11:40. Measured from the start it
        # would stop at 12:10 -- five minutes short of the click that made it,
        # so the gap it was written to close would not quite close.
        self.write("11:40", "12:15")
        self.assertEqual(wp.marks_for(DAY, stamps=[])[0]["end"],
                         wp.to_min("12:15"))

    def test_a_mark_backdated_past_the_ceiling_is_not_born_expired(self):
        # An hour and a half back. From the start the ceiling lands at 12:00,
        # before the mark was even written, and it would contribute a stretch
        # that stopped before the moment it was made.
        self.write("11:30", "12:15")
        self.assertGreaterEqual(wp.marks_for(DAY, stamps=[])[0]["end"],
                                wp.to_min("12:15"))

    def test_real_activity_still_closes_it_before_the_ceiling(self):
        # The ceiling is a backstop, not the usual way an open mark ends: the
        # first event after it still wins, and moving the ceiling must not have
        # quietly made marks outlive the evidence that supersedes them.
        self.write("11:40", "12:15")
        self.assertEqual(
            wp.marks_for(DAY, stamps=[wp.to_min("11:50")])[0]["end"],
            wp.to_min("11:50"))


class MergesTheGapCase(unittest.TestCase):
    """The goal, through the real snapshot builder: the gap actually closes.

    Everything above tests the decision; this tests the consequence. The person
    clicked the row because the menu showed them two sessions with a hole
    between, and what they are owed is one session. That happens in
    write_vault_snapshot, where the mark joins the spans -- so a test that
    stubs the snapshot out cannot see whether it worked, and the first version
    of this feature passed every such test while landing five minutes short.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, leaf in [("MARKS", "marks.jsonl"), ("NOTES", "notes.jsonl"),
                           ("MEETING_CUT", "meeting-cut.json"),
                           ("LABELS", "labels.jsonl"),
                           ("CAL_FILE", "calendar-today.md"),
                           ("STATE", ""),
                           ("VAULT_SNAPSHOT_DIR", "worktime")]:
            self.patch(name, os.path.join(self.tmp.name, leaf))
        self.now = datetime(2026, 3, 4, 12, 15)
        self.patch("now_local", lambda: self.now)
        # The day's other signals stubbed to empty: this is about prompts and
        # marks, and a calendar or a focus log would only add spans that make a
        # merge easier to achieve than it really is.
        self.patch("calendar_events", lambda day: [])
        self.patch("slack_for", lambda day: [])
        self.patch("focus_for", lambda day, *a, **k: [])
        self.patch("prompts_for", lambda day: [])
        self.patch("idle_cut", lambda day, protected: [])
        self.patch("summarize_span", lambda day, a, b: "")
        self.patch("focus_apps", lambda day, lo, hi: [])
        self.patch("sessions_in", lambda day, a, b: [])

    def patch(self, name, value):
        orig = getattr(wp, name)
        setattr(wp, name, value)
        self.addCleanup(lambda: setattr(wp, name, orig))

    def prompts(self, *spans):
        out = []
        for a, b in spans:
            t, end = datetime(2026, 3, 4, *a), datetime(2026, 3, 4, *b)
            while t <= end:
                out.append(t)
                t += timedelta(minutes=1)
        self.patch("events_for", lambda day: out)
        return out

    def periods(self):
        snap = json.load(open(wp.snapshot_path(DAY)))
        return [(wp.hhmm_of(w["start"]), wp.hhmm_of(w["end"]))
                for w in snap["worked"]]

    def test_the_last_session_ends_up_running_to_now(self):
        # Nothing live: worked till 11:40, stepped away, back at 12:15. The
        # 35-minute gap is longer than the open-mark ceiling used to allow.
        events = self.prompts(((9, 0), (9, 20)), ((11, 0), (11, 40)))
        wp.write_vault_snapshot(DAY, events)
        self.assertEqual(len(self.periods()), 2)
        wp.link_last_session()
        self.assertEqual(self.periods()[-1][1], "12:15")

    def test_a_live_session_and_the_one_before_it_become_one(self):
        # Prompting right now, so the newest period is the current session and
        # the link reaches past it. The morning and the present are one stretch
        # afterwards, which is the whole point of the row.
        events = self.prompts(((9, 0), (10, 30)), ((12, 10), (12, 13)))
        wp.write_vault_snapshot(DAY, events)
        before = self.periods()
        self.assertEqual(len(before), 2)
        wp.link_last_session()
        after = self.periods()
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0][0], before[0][0])
        self.assertGreaterEqual(after[0][1], "12:13")


if __name__ == "__main__":
    unittest.main()
