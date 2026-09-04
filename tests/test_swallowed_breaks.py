#!/usr/bin/env python3
"""The one failure this tracker keeps having: a break comes back as work.

Three separate bugs now, all the same shape. A stretch of the day holding real
absences -- twenty minutes here, half an hour there -- is published as one
unbroken period, and the day total reads hours longer than it was. The mechanism
differs every time (bouts rejoining across a break the dot had already split;
a mark growing when it was closed), which is why fixing the mechanism keeps not
being enough: the next one arrives through a door nobody had thought to lock.

So the invariants here are stated over the OUTCOME rather than over any one
mechanism:

  * A silence nothing explains is a gap, however the period was assembled.
  * Nothing a person can click makes the day longer. Every menu item that ends
    something can only remove minutes; an End Session that ADDS four hours is
    absurd on its face and no amount of arguing about marks makes it less so.
  * Declared presence covers exactly the minutes it declared. A mark worth four
    minutes cannot hold an afternoon together.

The replay at the bottom is the real 2026-09-04, minute for minute. A synthetic
case proves the rule; the recorded day proves the rule was the one that day
needed.

Run: pytest tests/test_swallowed_breaks.py
"""

import importlib.util
import itertools
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

DAY = "2026-09-04"
FOCUSED = [(0, "focused")]


def periods(stamps_min, marks=()):
    """The published work periods, in minutes, for a day made of these events.

    The real builder, in the order write_vault_snapshot runs it: bouts from the
    events, declared presence unioned in, merge last. Meetings, desktop holes
    and session ends are other tests' business and are left out.
    """
    present = wp.build_bouts(sorted(m * 60 for m in stamps_min), FOCUSED)
    present += [[a * 60, b * 60, True] for a, b in marks]
    merged = wp.merge_spans(sorted(present, key=lambda p: p[0]), FOCUSED)
    return [[s // 60, e // 60] for s, e in merged]


def silences(stamps_min):
    """Every stretch between consecutive events long enough to end a period.

    Strictly longer than GAP_AFTER: a silence of exactly the threshold chains,
    which is the model's own rule and has its own test in test_worktime_model.
    """
    s = sorted(set(stamps_min))
    return [(a, b) for a, b in zip(s, s[1:]) if b - a > wp.GAP_AFTER]


class UnexplainedSilenceIsAlwaysAGap(unittest.TestCase):
    """The outcome invariant, held over the builder itself.

    Deliberately not a test of any particular rule inside merge_spans. It asks
    the only question the person looking at the menu is asking -- "was I really
    at this for four hours?" -- and it stays true no matter which rule is
    rewritten next.
    """

    def assert_no_period_swallows_a_silence(self, stamps, marks=()):
        covered = [range(a, b + 1) for a, b in marks]
        for lo, hi in silences(stamps):
            if any(lo in c and hi in c for c in covered):
                continue  # declared presence explains this one
            for a, b in periods(stamps, marks):
                self.assertFalse(
                    a < lo and hi < b,
                    f"the {hi - lo}-minute silence {lo}-{hi} was swallowed by "
                    f"the period {a}-{b}")

    def test_a_long_absence_between_two_busy_stretches(self):
        stamps = [600, 601, 602, 603] + [660, 661, 662, 663]
        self.assertEqual(len(periods(stamps)), 2)
        self.assert_no_period_swallows_a_silence(stamps)

    def test_absences_of_every_length_over_the_threshold(self):
        # The threshold itself is tested elsewhere; what matters here is that
        # nothing above it is ever absorbed, at any size.
        for hole in range(wp.GAP_AFTER + 1, 240, 7):
            stamps = [600, 601, 602, 602 + hole, 603 + hole]
            with self.subTest(hole=hole):
                self.assert_no_period_swallows_a_silence(stamps)

    def test_a_mark_covers_its_own_minutes_and_no_others(self):
        # Declared presence is allowed to bridge -- that is what a mark is for
        # -- but only across what it declared. The 2026-09-04 failure was a
        # four-minute mark holding four hours together.
        stamps = [600, 601, 640, 641, 700, 701]
        marks = [(601, 605)]
        self.assert_no_period_swallows_a_silence(stamps, marks)
        self.assertGreater(len(periods(stamps, marks)), 1)

    def test_a_mark_that_does_cover_the_silence_still_bridges_it(self):
        # The other direction, so the invariant above cannot be satisfied by
        # simply never letting a mark join anything.
        stamps = [600, 601, 700, 701]
        self.assertEqual(len(periods(stamps, [(601, 700)])), 1)


class ClosingAMarkNeverLengthensIt(unittest.TestCase):
    """Every way of ending the day, against every shape of open mark.

    An open mark is READ as running to the first event after it and no further
    than MARK_MAX_OPEN_MIN from the click. Closing it must write down that same
    minute. Anything else means the mark meant one thing while it was open and
    another once it was closed -- which is exactly how a link worth four
    minutes turned into 4h25m when End Session was clicked at 14:28.
    """

    NAMES = ("STATE", "MARKS", "MEETING_CUT", "SESSION_END",
             "now_local", "events_for", "write_vault_snapshot",
             "prompts_for", "focus_for")

    NOW = 16 * 60 + 40

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {n: getattr(wp, n) for n in self.NAMES}
        wp.STATE = self.tmp
        wp.MARKS = os.path.join(self.tmp, "marks.jsonl")
        wp.MEETING_CUT = os.path.join(self.tmp, "meeting-cut.json")
        wp.SESSION_END = os.path.join(self.tmp, "session-end.json")
        wp.now_local = lambda: datetime(2026, 9, 4, self.NOW // 60, self.NOW % 60)
        wp.events_for = lambda day: []
        wp.write_vault_snapshot = lambda day, events: None
        # Stubbed at the sources rather than at mark_stamps, so this class
        # runs against any version of the probe that has ever had this bug --
        # including the ones from before the resolution was given a name.
        self.stamps = []
        wp.prompts_for = lambda day: [
            datetime(2026, 9, 4, m // 60, m % 60) for m in self.stamps]
        wp.focus_for = lambda day: []

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(wp, name, value)

    def write_open_mark(self, start, made):
        with open(wp.MARKS, "w") as fh:
            fh.write(json.dumps({
                "day": DAY, "start": start, "end": None, "note": "linked",
                "created": datetime(2026, 9, 4, made // 60,
                                    made % 60).isoformat()}) + "\n")

    def span(self):
        """The one mark's span as the rest of the tracker sees it."""
        marks = wp.marks_for(DAY, self.stamps)
        return (marks[0]["start"], marks[0]["end"]) if marks else None

    # start, made, the events after it, and the minute the day is ended at.
    STARTS = (10 * 60, 10 * 60 + 3, 16 * 60 + 20)
    MADE_AFTER = (0, 5, 40)
    STAMPS = ((), (10 * 60 + 4,), (10 * 60 + 4, 11 * 60), (16 * 60 + 30,))
    ENDINGS = (None, 10 * 60 + 2, 15 * 60, 16 * 60 + 40)

    def test_closing_writes_the_end_the_mark_already_had(self):
        for start, after, stamps, when in itertools.product(
                self.STARTS, self.MADE_AFTER, self.STAMPS, self.ENDINGS):
            with self.subTest(start=start, made=start + after,
                              stamps=stamps, when=when):
                self.stamps = list(stamps)
                self.write_open_mark(start, start + after)
                before = self.span()
                wp.close_open_marks(when)
                after_close = self.span()
                # Closing at a minute of its own can only cut the mark short.
                # It can never hand back minutes the open reading withheld.
                want_end = max(min(self.NOW if when is None else when,
                                   before[1] if before else start), start)
                if before is None or want_end == start:
                    # Closed and zero-length: marks_for drops it, which is how
                    # a mark undone in the minute it was made leaves no time.
                    self.assertIsNone(after_close)
                else:
                    self.assertEqual(after_close, (start, want_end))
                self.assertLessEqual(
                    (after_close[1] - after_close[0]) if after_close else 0,
                    before[1] - before[0] if before else 0,
                    "closing the mark made it longer")

    def test_end_session_never_adds_minutes_to_the_day(self):
        # Said at the level the person clicks at, because that is the level the
        # absurdity is visible at: ending the day cannot lengthen it.
        for at_last in (False, True):
            for stamps in self.STAMPS:
                with self.subTest(at_last=at_last, stamps=stamps):
                    self.stamps = list(stamps)
                    self.write_open_mark(10 * 60, 10 * 60)
                    before = self.span()
                    wp.end_session(at_last=at_last)
                    now = self.span()
                    self.assertLessEqual((now[1] - now[0]) if now else 0,
                                         before[1] - before[0])

    def test_a_mark_still_survives_being_closed_while_it_is_running(self):
        # The invariant must not be satisfiable by closing every mark to
        # nothing: a mark made at 16:20 with nothing since is genuinely still
        # running at 16:40, and End Session banks those twenty minutes.
        self.write_open_mark(16 * 60 + 20, 16 * 60 + 20)
        wp.end_session()
        self.assertEqual(self.span(), (16 * 60 + 20, self.NOW))


# 2026-09-04, from the recorded day: every minute holding a prompt, a focus
# sample or a Slack send between 10:00 and 14:30. Kept verbatim rather than
# reduced to a few representative gaps, because what made the bug survive
# review was how ordinary the day looked -- 78 minutes of real evidence spread
# across four and a half hours, most silences short, three of them long.
SEPT_4 = [601, 602, 607, 608, 610, 611, 614, 615, 616, 617, 618, 620, 622,
          623, 624, 625, 631, 632, 633, 634, 636, 637, 638, 639, 641, 642,
          643, 650, 652, 669, 671, 696, 697, 698, 699, 700, 701, 703, 724,
          725, 732, 737, 740, 745, 751, 752, 768, 770, 790, 791, 814, 815,
          816, 818, 819, 823, 825, 826, 827, 830, 831, 832, 833, 836, 837,
          838, 839, 840, 841, 842, 851, 852, 853, 856, 857, 865, 866, 867]

# The link that was clicked at 10:08, anchored at 10:03, and closed by End
# Session at 14:28. Its resolved end is 10:07 -- the first event after it --
# and 14:28 is what the old close wrote instead.
LINK_START, LINK_MADE, LINK_RESOLVED, LINK_AS_WRITTEN = 603, 608, 607, 868


class TheDayItHappened(unittest.TestCase):
    """The recorded morning, replayed through the real builder."""

    def test_the_day_reads_as_many_periods_not_one(self):
        out = periods(SEPT_4, [(LINK_START, LINK_RESOLVED)])
        self.assertGreater(len(out), 10)
        self.assertLess(max(b - a for a, b in out), 60)

    def test_the_long_absences_are_still_absences(self):
        out = periods(SEPT_4, [(LINK_START, LINK_RESOLVED)])
        for lo, hi in ((671, 696), (703, 724), (752, 768), (770, 790)):
            with self.subTest(silence=(lo, hi)):
                self.assertFalse(any(a < lo and hi < b for a, b in out),
                                 f"{hi - lo} minutes away, counted as work")

    def test_the_day_total_is_hours_shorter_than_the_bug_made_it(self):
        good = sum(b - a for a, b in periods(SEPT_4,
                                             [(LINK_START, LINK_RESOLVED)]))
        bug = sum(b - a for a, b in periods(SEPT_4,
                                            [(LINK_START, LINK_AS_WRITTEN)]))
        self.assertEqual(len(periods(SEPT_4, [(LINK_START, LINK_AS_WRITTEN)])),
                         1, "the bug published the whole stretch as one period")
        self.assertLess(good, bug - 120,
                        "over two hours of breaks were being credited")


if __name__ == "__main__":
    unittest.main()
