#!/usr/bin/env python3
"""Tests for the period model and the approval-matching hook.

The model is arithmetic on timestamps and has been rebuilt several times; each
rebuild broke an invariant the previous one established (zero-length periods,
banker's rounding collapsing a minute, gaps rendering under the gap threshold).
Those are exactly the regressions a test can hold down, so the invariants are
asserted here rather than re-discovered on the dashboard.

Run: pytest tests/test_worktime_model.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "bin", "worktime-approval.py")

def _load(name, path):
    """Import a hyphenated script. These are symlinked onto PATH and into
    ~/.claude/hooks rather than packaged, so their filenames are not importable
    names and there is no package to import them from."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wp = _load("wp", os.path.join(ROOT, "bin", "worktime-probe.py"))


def build(stamps, firsts=(), mode="focused"):
    """Run the real period builder over raw second-of-day stamps.

    Calls into the probe rather than re-implementing it. An earlier version of
    this helper carried its own copy of the arithmetic, on the grounds that
    write_vault_snapshot also reads the calendar, Slack, the vault and the
    summariser -- true, but the copy could only ever be as current as the last
    person to remember it existed, and the whole suite would have gone on
    passing against superseded rules. The pure part is now importable, so this
    tests what actually ships.
    """
    tl = [(0, mode)]
    present = wp.build_bouts(sorted(stamps), set(firsts), tl)
    merged = wp.merge_spans(present, tl)
    mins = [[s // 60, e // 60] for s, e in merged]
    gaps = [b[0] - a[1] for a, b in zip(mins, mins[1:]) if b[0] > a[1]]
    # Seconds too: published minutes are floored, so a 20-second difference is
    # invisible there and an assertion about it would be untestable.
    return mins, gaps, merged


HH = lambda h, m, s=0: h * 3600 + m * 60 + s


class PeriodModel(unittest.TestCase):
    def test_single_prompt_is_at_least_a_minute(self):
        mins, _, _sec = build([HH(10, 0)])
        self.assertEqual(len(mins), 1)
        self.assertGreaterEqual(mins[0][1] - mins[0][0], 1)

    def test_no_zero_length_period(self):
        # Four prompts inside one minute used to collapse to 0m, because the
        # arithmetic was done on floored minutes rather than on seconds.
        mins, _, _sec = build([HH(8, 1, 5), HH(8, 1, 20), HH(8, 1, 41), HH(8, 1, 58)])
        self.assertEqual(len(mins), 1)
        self.assertGreater(mins[0][1] - mins[0][0], 0)

    def test_a_minute_of_work_never_publishes_as_zero(self):
        # round() sends halves to even, so 13:05:30 and 13:06:30 both became
        # 786 and a full minute rendered as 0m. Flooring cannot do that.
        mins, _, _sec = build([HH(13, 5, 30), HH(13, 6, 30)])
        self.assertGreaterEqual(mins[0][1] - mins[0][0], 1)

    def test_six_minute_silence_is_a_gap(self):
        mins, gaps, _sec = build([HH(10, 0), HH(10, 6, 30)])
        self.assertEqual(len(mins), 2, "a >5m silence must split the period")
        self.assertEqual(len(gaps), 1)

    def test_five_minute_silence_chains(self):
        mins, gaps, _sec = build([HH(10, 0), HH(10, 5)])
        self.assertEqual(len(mins), 1, "a <=5m silence must not split")
        self.assertEqual(gaps, [])

    def test_lead_never_produces_a_sub_threshold_gap(self):
        # The regression the old five-minute grace period caused: a dashboard
        # showing gaps shorter than the threshold it claims to use.
        for extra_sec in range(310, 700, 7):
            _, gaps, _sec = build([HH(10, 0), HH(10, 0) + extra_sec])
            for g in gaps:
                self.assertGreaterEqual(
                    g, wp.GAP_AFTER,
                    f"silence of {extra_sec}s produced a {g}m gap")

    def test_lead_is_applied_when_there_is_room(self):
        mins, _, _sec = build([HH(10, 0)])
        self.assertEqual(mins[0][0], (HH(10, 0) - wp.LEAD_SEC) // 60)

    def test_session_first_prompt_gets_extra_lead(self):
        # Asserted in seconds: published minutes are floored, so a 20-second
        # difference is invisible there.
        _, _, plain = build([HH(10, 0)])
        _, _, first = build([HH(10, 0)], firsts=[HH(10, 0)])
        self.assertEqual(plain[0][0] - first[0][0], wp.SESSION_LEAD_SEC,
                         "opening a conversation buys exactly the extra lead")

    def test_continuing_a_conversation_gets_no_extra_lead(self):
        _, _, plain = build([HH(10, 0)])
        self.assertEqual(HH(10, 0) - plain[0][0], wp.LEAD_SEC)

    def test_session_lead_also_respects_the_gap_floor(self):
        for extra_sec in range(310, 700, 7):
            t0 = HH(10, 0)
            t1 = t0 + extra_sec
            _, gaps, _sec = build([t0, t1], firsts=[t1])
            for g in gaps:
                self.assertGreaterEqual(g, wp.GAP_AFTER)

    def test_both_rules_hold_across_every_silence(self):
        # The pair that used to contradict each other: a >5m silence must
        # always split, and no resulting gap may display under 5m -- and no
        # period may collapse to zero doing it.
        for extra_sec in range(301, 900):
            t0 = HH(10, 0)
            mins, gaps, _sec = build([t0, t0 + extra_sec])
            self.assertEqual(len(mins), 2,
                             f"{extra_sec}s silence did not split")
            for g in gaps:
                self.assertGreaterEqual(g, wp.GAP_AFTER,
                                        f"{extra_sec}s -> {g}m gap")
            for a, b in mins:
                self.assertGreaterEqual(b - a, 1,
                                        f"{extra_sec}s produced a 0m period")

    def test_periods_never_overlap(self):
        stamps = [HH(9, 0), HH(9, 2), HH(9, 40), HH(9, 41), HH(11, 0)]
        mins, _, _sec = build(stamps)
        for a, b in zip(mins, mins[1:]):
            self.assertLessEqual(a[1], b[0])

    def test_day_anchor_is_five_am(self):
        self.assertEqual(wp.DAY_ANCHOR, 5 * 60)


class FocusModes(unittest.TestCase):
    """The unfocused ramp: a bout earns its cutoff instead of being handed it."""

    def test_focused_cutoff_is_flat(self):
        for elapsed in (0, 60, 600, 36000):
            self.assertEqual(wp.gap_sec_for("focused", elapsed), wp.GAP_AFTER * 60)

    def test_unfocused_opens_at_one_minute(self):
        self.assertEqual(wp.gap_sec_for("unfocused", 0), wp.UNFOCUSED_GAP_START * 60)

    def test_unfocused_reaches_gap_after_at_the_ramp(self):
        self.assertEqual(wp.gap_sec_for("unfocused", wp.UNFOCUSED_RAMP_MIN * 60),
                         wp.GAP_AFTER * 60)

    def test_unfocused_never_exceeds_focused(self):
        for elapsed in range(0, 3600, 30):
            self.assertLessEqual(wp.gap_sec_for("unfocused", elapsed),
                                 wp.gap_sec_for("focused", elapsed))

    def test_unfocused_ramp_is_monotone(self):
        prev = -1
        for elapsed in range(0, 1200, 10):
            cur = wp.gap_sec_for("unfocused", elapsed)
            self.assertGreaterEqual(cur, prev)
            prev = cur

    def test_prompts_every_four_minutes_chain_when_focused(self):
        # The behaviour unfocused mode exists to change: half an hour of
        # occasional prompting reads as one unbroken half hour of work.
        stamps = [HH(10, 0) + i * 240 for i in range(8)]
        mins, gaps, _ = build(stamps, mode="focused")
        self.assertEqual(len(mins), 1)
        self.assertEqual(gaps, [])

    def test_prompts_every_four_minutes_do_not_chain_when_unfocused(self):
        stamps = [HH(10, 0) + i * 240 for i in range(8)]
        mins, _, _ = build(stamps, mode="unfocused")
        self.assertEqual(len(mins), 8, "each isolated prompt is its own bout")

    def test_unfocused_lone_prompt_costs_the_minimum(self):
        # No ramp yet, so no lead: the whole span is the MIN_PERIOD floor,
        # against 80s for the same prompt in focused mode.
        _, _, sec = build([HH(10, 0)], mode="unfocused")
        self.assertEqual(sec[0][1] - sec[0][0], wp.MIN_PERIOD_SEC)
        _, _, foc = build([HH(10, 0)], mode="focused")
        self.assertEqual(foc[0][1] - foc[0][0], wp.LEAD_SEC + wp.TAIL_SEC)

    def test_a_sustained_unfocused_session_widens_to_the_full_cutoff(self):
        # Ten minutes of prompting every 45s earns the ramp, after which a
        # four-minute silence chains exactly as it would when focused.
        warmup = [HH(10, 0) + i * 45 for i in range(15)]
        mins, _, _ = build(warmup + [HH(10, 0) + 14 * 45 + 240], mode="unfocused")
        self.assertEqual(len(mins), 1)

    def test_unfocused_credits_less_than_focused_for_the_same_day(self):
        stamps = [HH(9, 0) + i * 210 for i in range(20)]
        _, _, foc = build(stamps, mode="focused")
        _, _, unf = build(stamps, mode="unfocused")
        self.assertLess(sum(e - s for s, e in unf),
                        sum(e - s for s, e in foc) / 2)

    def test_unfocused_never_publishes_a_zero_length_period(self):
        for extra in range(5, 600, 7):
            _, _, sec = build([HH(10, 0), HH(10, 0) + extra], mode="unfocused")
            for s, e in sec:
                self.assertGreaterEqual(e - s, wp.MIN_PERIOD_SEC,
                                        f"{extra}s apart produced a short span")

    def test_unfocused_periods_never_overlap(self):
        stamps = [HH(9, 0) + i * 37 for i in range(40)] + [HH(11, 0), HH(11, 4)]
        mins, _, sec = build(stamps, mode="unfocused")
        for a, b in zip(sec, sec[1:]):
            self.assertLessEqual(a[1], b[0])
        for a, b in zip(mins, mins[1:]):
            self.assertLessEqual(a[1], b[0])

    def test_a_gap_is_never_shown_shorter_than_the_cutoff_that_made_it(self):
        # The focused rule's invariant, restated for a moving threshold: the
        # padding may never eat a silence down past the cutoff that split it.
        for extra in range(65, 900, 11):
            stamps = [HH(10, 0), HH(10, 0) + extra]
            _, gaps, _ = build(stamps, mode="unfocused")
            if gaps:
                self.assertGreaterEqual(
                    gaps[0] * 60 + 60, wp.gap_sec_for("unfocused", 0),
                    f"{extra}s silence published a {gaps[0]}m gap")

    def test_mode_timeline_confines_a_change_to_what_follows_it(self):
        tl = [(0, "focused"), (HH(15, 0), "unfocused")]
        self.assertEqual(wp.mode_at(tl, HH(9, 0)), "focused")
        self.assertEqual(wp.mode_at(tl, HH(14, 59)), "focused")
        self.assertEqual(wp.mode_at(tl, HH(15, 0)), "unfocused")
        self.assertEqual(wp.mode_at(tl, HH(20, 0)), "unfocused")

    def test_a_midday_switch_leaves_the_morning_intact(self):
        # The reason the mode is a log and not a setting: flipping at noon must
        # not retroactively shred work that happened under the other rule.
        morning = [HH(9, 0) + i * 240 for i in range(6)]
        afternoon = [HH(15, 0) + i * 240 for i in range(6)]
        tl = [(0, "focused"), (HH(12, 0), "unfocused")]
        merged = wp.merge_spans(wp.build_bouts(morning + afternoon, set(), tl), tl)
        before = [p for p in merged if p[0] < HH(12, 0)]
        after = [p for p in merged if p[0] >= HH(12, 0)]
        self.assertEqual(len(before), 1, "the focused morning stays one period")
        self.assertEqual(len(after), 6, "the unfocused afternoon fragments")

    def test_default_mode_is_focused(self):
        self.assertEqual(wp.mode_timeline("1970-01-01"), [(0, "focused")])


def build_desk(stamps, desktop, firsts=(), mode="focused"):
    """The real pipeline including desktop subtraction, as the snapshot runs it.

    Mirrors write_vault_snapshot's order: bouts, merge, THEN subtract. The
    subtraction has to come last -- merge_spans rejoins anything closer than
    the cutoff, so a hole opened before it gets closed again on the way past.
    Marks and meetings are exempt there and are out of scope here.
    """
    tl = [(0, mode)]
    present = wp.build_bouts(sorted(stamps), set(firsts), tl)
    merged = wp.merge_spans(present, tl)
    merged = wp.subtract_spans(
        merged, wp.desktop_holes(desktop, {s // 60 for s in stamps}, tl))
    return [[s // 60, e // 60] for s, e in merged], merged


class DesktopSubtraction(unittest.TestCase):
    def test_desktop_prompt_in_a_gap_changes_nothing(self):
        # Two thirds of real desktop prompts land outside every Mac period.
        # Switching machines makes the Mac go quiet and the ordinary cutoff
        # already ended the period -- there is nothing left to subtract.
        stamps = [HH(10, 0), HH(10, 2), HH(10, 4)]
        plain, _ = build_desk(stamps, [])
        with_desk, _ = build_desk(stamps, [HH(14, 0), HH(14, 3)])
        self.assertEqual(plain, with_desk)

    def test_lone_desktop_prompt_removes_exactly_one_minute(self):
        # No padding. A Mac prompt gets LEAD_SEC + TAIL_SEC because it
        # under-represents the work around it; a desktop prompt means the
        # opposite, so padding it would erase Rubrik time instead.
        #
        # Mac stamps on even minutes only, desktop on an odd one -- otherwise
        # the tie rule protects the minute and nothing is removed at all.
        stamps = [HH(10, 0) + i * 120 for i in range(11)]
        plain, _ = build_desk(stamps, [])
        cut, _ = build_desk(stamps, [HH(10, 9)])
        self.assertEqual(sum(e - s for s, e in plain)
                         - sum(e - s for s, e in cut), 1)

    def test_hole_mid_period_splits_it(self):
        stamps = [HH(10, 0) + i * 120 for i in range(11)]
        cut, _ = build_desk(stamps, [HH(10, 9)])
        self.assertEqual(len(cut), 2)
        self.assertEqual(cut[0][1], 10 * 60 + 9)
        self.assertEqual(cut[1][0], 10 * 60 + 10)

    def test_work_wins_a_tied_minute(self):
        # 16% of desktop minutes also hold a Mac event. The Mac sources carry
        # seconds and the export only names a minute, so the coarser reading
        # must not delete a minute there is direct evidence of working in.
        stamps = [HH(10, 0) + i * 60 for i in range(21)]
        plain, _ = build_desk(stamps, [])
        tied, _ = build_desk(stamps, [HH(10, 10)])       # 10:10 is a Mac minute
        self.assertEqual(plain, tied)
        self.assertEqual(
            wp.desktop_holes([HH(10, 10)], {s // 60 for s in stamps},
                             [(0, "focused")]), [])

    def test_untied_minute_is_removed(self):
        # Same shape, but the desktop prompt lands on a minute with no Mac
        # event in it -- so there is nothing to protect and it comes out.
        stamps = [HH(10, 0) + i * 120 for i in range(11)]   # every other minute
        holes = wp.desktop_holes([HH(10, 1)], {s // 60 for s in stamps},
                                 [(0, "focused")])
        self.assertEqual(holes, [[HH(10, 1), HH(10, 2)]])

    def test_consecutive_desktop_prompts_chain_into_one_hole(self):
        holes = wp.desktop_holes([HH(9, 0), HH(9, 3), HH(9, 6)], set(),
                                 [(0, "focused")])
        self.assertEqual(holes, [[HH(9, 0), HH(9, 7)]])

    def test_distant_desktop_prompts_stay_separate(self):
        holes = wp.desktop_holes([HH(9, 0), HH(9, 30)], set(), [(0, "focused")])
        self.assertEqual(len(holes), 2)

    def test_subtraction_never_publishes_a_zero_length_period(self):
        # A remnant that floors to the same minute at both ends would render
        # 0m -- the exact thing MIN_PERIOD_SEC keeps out of the snapshot.
        for k in range(1, 40):
            stamps = [HH(10, 0), HH(10, 0) + k * 60]
            mins, _ = build_desk(stamps, [HH(10, 0) + 30])
            for s, e in mins:
                self.assertGreater(e, s, f"zero-length period at k={k}")

    def test_periods_stay_sorted_and_disjoint_after_splitting(self):
        stamps = [HH(9, 0) + i * 60 for i in range(121)]
        mins, _ = build_desk(
            stamps, [HH(9, 20), HH(9, 21), HH(10, 5), HH(10, 40), HH(10, 41)])
        for a, b in zip(mins, mins[1:]):
            self.assertLessEqual(a[1], b[0])
        for s, e in mins:
            self.assertGreater(e, s)

    def test_fully_covered_period_disappears(self):
        stamps = [HH(10, 0)]
        holes = [[HH(9, 0), HH(11, 0)]]
        self.assertEqual(wp.subtract_spans([[HH(10, 0), HH(10, 1)]], holes), [])

    def test_subtraction_only_ever_removes(self):
        stamps = [HH(8, 0) + i * 90 for i in range(60)]
        desk = [HH(8, 30), HH(9, 15), HH(9, 16), HH(10, 2)]
        plain, _ = build_desk(stamps, [])
        cut, _ = build_desk(stamps, desk)
        self.assertLessEqual(sum(e - s for s, e in cut),
                             sum(e - s for s, e in plain))


class IdleExclusion(unittest.TestCase):
    """Silence inside a working period, taken back out of it.

    A period is built from its first and last event, so before this a two
    minute absence between two prompts was counted in full: nothing in between
    was ever asked whether anybody was there for it.
    """

    DAY = "2026-08-26"

    def _focus(self, samples):
        """A focus log. samples: [(hh, mm, ss, idle_sec)], app always Ghostty."""
        d = tempfile.mkdtemp()
        with open(os.path.join(d, f"{self.DAY}.jsonl"), "w") as fh:
            for hh, mm, ss, idle in samples:
                fh.write(json.dumps({
                    "day": self.DAY, "t": f"{hh:02d}:{mm:02d}:{ss:02d}",
                    "app": "Ghostty", "bundle": "com.mitchellh.ghostty",
                    "idle": idle}) + "\n")
        return d

    def _with(self, samples, claims=(), fn=None, subtracts=True):
        """`subtracts` forces IDLE_SUBTRACTS, so the tests of the RULE keep
        testing the rule whichever way the shipped switch is set. What the
        switch itself does is tested separately, against the real constant.
        """
        d = self._focus(samples)
        claim_path = os.path.join(tempfile.mkdtemp(), "idle-claims.jsonl")
        with open(claim_path, "w") as fh:
            for lo, hi in claims:
                fh.write(json.dumps({"day": self.DAY, "from": lo,
                                     "until": hi}) + "\n")
        old = (wp.FOCUS_DIR, wp.IDLE_CLAIMS, wp.IDLE_SUBTRACTS)
        try:
            wp.FOCUS_DIR, wp.IDLE_CLAIMS = d, claim_path
            wp.IDLE_SUBTRACTS = subtracts
            return fn()
        finally:
            wp.FOCUS_DIR, wp.IDLE_CLAIMS, wp.IDLE_SUBTRACTS = old

    # --- recovering the stretch from the samples ---

    def test_the_stretch_runs_back_to_the_last_real_input(self):
        # The sample at 10:05 reporting 300s idle says nobody touched the
        # machine since 10:00. Starting the exclusion where the threshold
        # tripped instead would leave two minutes of phantom work on the end
        # of every absence.
        got = self._with([(10, 5, 0, 300)],
                         fn=lambda: wp.idle_stretches(self.DAY))
        self.assertEqual(got, [[HH(10, 0), HH(10, 5)]])

    def test_a_run_of_samples_is_one_stretch_not_sixty(self):
        # Every 30s sample while away reports a longer idle covering the same
        # silence, so the raw readings are nested spans of one absence.
        samples = [(10, m // 60 % 60, m % 60, 120 + m)
                   for m in range(0, 600, 30)]
        got = self._with(samples, fn=lambda: wp.idle_stretches(self.DAY))
        self.assertEqual(len(got), 1)

    def test_input_inside_the_threshold_is_not_a_stretch(self):
        got = self._with([(10, 5, 0, wp.FOCUS_IDLE_SEC)],
                         fn=lambda: wp.idle_stretches(self.DAY))
        self.assertEqual(got, [])

    # --- what it does to the period ---

    def test_an_absence_between_two_prompts_stops_being_counted(self):
        # The whole point. Prompts at 10:00 and 10:04 form one period; the
        # machine was untouched for the middle of it.
        stamps = [HH(10, 0), HH(10, 4)]
        tl = [(0, "focused")]
        merged = wp.merge_spans(wp.build_bouts(stamps, set(), tl), tl)
        before = sum(e - s for s, e in merged)
        # The absence sits strictly inside, so both remnants survive and the
        # only thing removed is the silence itself -- no interaction with the
        # separate rule that drops a remnant too short to publish a minute.
        holes = self._with([(10, 3, 0, 150)],
                           fn=lambda: wp.idle_stretches(self.DAY))
        after = sum(e - s for s, e in wp.subtract_spans(merged, holes))
        self.assertLess(after, before)
        self.assertEqual(before - after, 150)

    def test_a_claim_protects_the_silence_it_answered(self):
        stamps = [HH(10, 0), HH(10, 4)]
        tl = [(0, "focused")]
        merged = wp.merge_spans(wp.build_bouts(stamps, set(), tl), tl)
        kept = self._with(
            [(10, 4, 0, 210)], claims=[(HH(10, 0), HH(10, 24))],
            fn=lambda: wp.subtract_spans(merged, wp.idle_cut(self.DAY, [])))
        self.assertEqual(kept, merged)

    def test_the_grace_window_covers_a_later_silence_too(self):
        # Answering "I am here" while reading a long diff should not have to
        # be answered again two minutes later.
        claims = [(HH(10, 0), HH(10, 0) + wp.IDLE_GRACE_SEC)]
        left = self._with(
            [(10, 4, 0, 210), (10, 18, 0, 300)], claims=claims,
            fn=lambda: wp.idle_cut(self.DAY, []))
        self.assertEqual(left, [])

    # --- what the switch does ---

    def test_the_cut_is_off_and_costs_the_day_nothing(self):
        # HID idle sees keys and mouse only, so reading, a call and an empty
        # room are one reading to it; the cut removed worked minutes more
        # often than absent ones and is switched off until it can tell them
        # apart. An untouched stretch that would have been cut now isn't.
        self.assertFalse(wp.IDLE_SUBTRACTS,
                         "flipping this back on needs a rule that can tell "
                         "reading from an empty room -- see README")
        cut = self._with([(10, 4, 0, 210)], subtracts=wp.IDLE_SUBTRACTS,
                         fn=lambda: wp.idle_cut(self.DAY, []))
        self.assertEqual(cut, [])

    def test_the_absence_is_still_measured_while_the_cut_is_off(self):
        # The point of leaving the prompt up: the stretches keep being found
        # and reported, so there is evidence to design a better rule from.
        # Same sample, one switch apart.
        seen = self._with([(10, 5, 0, 300)], subtracts=wp.IDLE_SUBTRACTS,
                          fn=lambda: wp.idle_stretches(self.DAY))
        self.assertEqual(seen, [[HH(10, 0), HH(10, 5)]])

    # --- which ones are worth showing ---

    def test_a_bridged_absence_is_an_activity(self):
        # Three minutes away, then input again: the period survived and its
        # arithmetic changed, so there is something to see.
        got = self._with([(10, 4, 0, 190), (10, 5, 0, 10)],
                         fn=lambda: wp.bridged_idle(self.DAY, [(0, "focused")]))
        self.assertEqual(got, [[HH(10, 0) + 50, HH(10, 4)]])

    def test_an_absence_that_ended_the_period_is_not_an_activity(self):
        # Six minutes is past the cutoff, so the period ended on its own. The
        # ended period already says that; a row repeating it is noise.
        got = self._with([(10, 6, 0, 360), (10, 7, 0, 10)],
                         fn=lambda: wp.bridged_idle(self.DAY, [(0, "focused")]))
        self.assertEqual(got, [])

    def test_an_absence_still_running_is_not_yet_an_activity(self):
        # Nothing has closed it, so reporting it as a completed event would
        # announce it while it was still happening.
        got = self._with([(10, 4, 0, 190)],
                         fn=lambda: wp.bridged_idle(self.DAY, [(0, "focused")]))
        self.assertEqual(got, [])


class SnapshotMeetingRecovery(unittest.TestCase):
    """A rebuild must not drop a source it merely failed to read.

    calendar-today.md holds only today, so calendar_events returns None for any
    earlier day -- indistinguishable from "no meetings" once it has returned.
    A backfill therefore erased 36 minutes of meeting-held work from 08-28.
    """

    def _with_snapshot(self, payload):
        import tempfile
        day = "2026-08-28"
        d = tempfile.mkdtemp()
        with open(os.path.join(d, f"{day}.json"), "w") as fh:
            json.dump(payload, fh)
        old = wp.VAULT_SNAPSHOT_DIR
        try:
            wp.VAULT_SNAPSHOT_DIR = d
            return wp.meetings_from_snapshot(day)
        finally:
            wp.VAULT_SNAPSHOT_DIR = old

    def test_meetings_are_read_back_off_the_snapshot(self):
        got = self._with_snapshot({"worked": [
            {"start": 600, "end": 660, "meetings": [
                {"start": 600, "end": 660, "title": "Standup", "counts": True}]}]})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["title"], "Standup")

    def test_a_meeting_spanning_two_periods_is_not_duplicated(self):
        m = {"start": 600, "end": 700, "title": "Review", "counts": True}
        got = self._with_snapshot({"worked": [
            {"start": 600, "end": 640, "meetings": [m]},
            {"start": 660, "end": 700, "meetings": [m]}]})
        self.assertEqual(len(got), 1)

    def test_no_snapshot_yields_none_not_empty(self):
        # None means "unknown", [] would mean "known to have none" and would
        # re-introduce the silent drop this exists to stop.
        import tempfile
        old = wp.VAULT_SNAPSHOT_DIR
        try:
            wp.VAULT_SNAPSHOT_DIR = tempfile.mkdtemp()
            self.assertIsNone(wp.meetings_from_snapshot("2026-08-28"))
        finally:
            wp.VAULT_SNAPSHOT_DIR = old

    def test_a_day_with_no_meetings_yields_none(self):
        self.assertIsNone(self._with_snapshot(
            {"worked": [{"start": 600, "end": 660, "meetings": []}]}))

    def test_corrupt_snapshot_does_not_raise(self):
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "2026-08-28.json"), "w") as fh:
            fh.write("{not json")
        old = wp.VAULT_SNAPSHOT_DIR
        try:
            wp.VAULT_SNAPSHOT_DIR = d
            self.assertIsNone(wp.meetings_from_snapshot("2026-08-28"))
        finally:
            wp.VAULT_SNAPSHOT_DIR = old


class GithubFilter(unittest.TestCase):
    def _visits(self, rows):
        import tempfile
        day = "2026-08-26"
        d = tempfile.mkdtemp()
        body = ("| time | source | direction | who | detail |\n"
                "|---|---|---|---|---|\n") + "".join(rows)
        with open(os.path.join(d, f"{day}.md"), "w") as fh:
            fh.write(body)
        # CHROME_DIR is redirected at an empty directory as well, or every
        # assertion here would also be reading whatever the person running the
        # suite happened to browse on the test day.
        old, old_chrome = wp.ACTIVITY_DIR, wp.wc.CHROME_DIR
        try:
            wp.ACTIVITY_DIR = d
            wp.wc.CHROME_DIR = os.path.join(d, "no-chrome")
            wp._gh_live_cache.clear()
            return wp.github_visits_for(day)
        finally:
            wp.ACTIVITY_DIR, wp.wc.CHROME_DIR = old, old_chrome
            wp._gh_live_cache.clear()

    def test_pr_page_matches_though_truncation_ate_the_url(self):
        # The exporter cuts detail at 80 chars and GitHub puts the title first,
        # so a real PR row never contains "github.com". Of 13 PR pages in six
        # days, a URL-only test caught one.
        row = ("| 08:16 | chrome | visit | synced | Route code-comment-content "
               "reviews to OWNERS team by oriooctopus · Pull Re |\n")
        self.assertEqual(len(self._visits([row])), 1)

    def test_repo_page_matches(self):
        row = ("| 09:01 | chrome | visit | synced | sdmain/foo.py at master · "
               "scaledata/sdmain |\n")
        self.assertEqual(len(self._visits([row])), 1)

    def test_saml_login_is_not_work(self):
        rows = [("| 08:03 | chrome | visit | synced | Rubrik - Sign In — "
                 "https://github.com/orgs/scaledata/saml/initiate?return_to=htt |\n"),
                ("| 08:03 | chrome | visit | synced | Sign in to scaledata · GitHub"
                 " — https://github.com/orgs/scaledata/saml/consume |\n")]
        self.assertEqual(self._visits(rows), [])

    def test_personal_oauth_is_not_rubrik_work(self):
        # Supabase and Microsoft sign-ins for projects on the other machine.
        # Counted as github.com visits they manufactured Rubrik work periods.
        rows = [("| 19:02 | chrome | visit | synced | Supabase — "
                 "https://github.com/login/oauth/authorize?client_id=60b7a7e |\n"),
                ("| 13:10 | chrome | visit | synced | Stay signed in? — "
                 "https://github.com/login/oauth/authorize?response_type=code |\n")]
        self.assertEqual(self._visits(rows), [])

    def test_ordinary_browsing_still_excluded(self):
        rows = [("| 10:00 | chrome | visit | synced | Amazon.com — "
                 "https://www.amazon.com/dp/B08 |\n"),
                ("| 10:01 | chrome | visit | synced | lm-review — "
                 "http://127.0.0.1:8213/app/dashboard |\n")]
        self.assertEqual(self._visits(rows), [])

    def test_docebo_visit_is_work(self):
        row = ("| 11:00 | chrome | visit | synced | Security Awareness Training"
               " — https://rubrik.docebosaas.com/learn/course/123 |\n")
        self.assertEqual(len(self._visits([row])), 1)

    def test_sso_visit_is_work_even_though_it_looks_like_a_login_page(self):
        # sso.rubrik.com's own pages legitimately live under /saml/-shaped
        # paths -- that pattern only excludes GitHub's login noise, and must
        # not also swallow real visits to the SSO portal itself.
        row = ("| 07:58 | chrome | visit | synced | Rubrik SSO — "
               "https://sso.rubrik.com/app/rubrik/exk1a2b3/sso/saml |\n")
        self.assertEqual(len(self._visits([row])), 1)


class ActivityFingerprint(unittest.TestCase):
    """What the five-second poll uses to decide nothing has changed.

    A miss here is invisible in the worst way: the menu keeps showing an
    earlier reading and gives no sign it is stale, because as far as the cache
    can tell nothing happened.
    """

    DAY = "2026-08-26"

    def _prompt_in(self, root_dir, name):
        d = os.path.join(root_dir, "-Users-someone-project")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as fh:
            fh.write('{"type":"user"}\n')

    def test_a_prompt_under_either_profile_moves_it(self):
        # Background jobs run under a second profile. Walking only the first
        # meant a prompt sent to one of them left the fingerprint identical,
        # so the verdict was served from cache for as long as nothing else
        # happened to touch a file.
        old = wp.PROMPT_ROOTS
        try:
            for i in range(len(old)):
                roots = [tempfile.mkdtemp() for _ in old]
                wp.PROMPT_ROOTS = roots
                before = wp.activity_fingerprint(self.DAY)
                self._prompt_in(roots[i], "session.jsonl")
                self.assertNotEqual(before, wp.activity_fingerprint(self.DAY),
                                    f"a prompt under root {i} changed nothing")
        finally:
            wp.PROMPT_ROOTS = old

    def test_it_stays_put_when_nothing_happened(self):
        # The other half: if it moved on its own the memo would never hit and
        # the 5-second poll would re-derive the whole day every time.
        old = wp.PROMPT_ROOTS
        try:
            wp.PROMPT_ROOTS = [tempfile.mkdtemp() for _ in old]
            self.assertEqual(wp.activity_fingerprint(self.DAY),
                             wp.activity_fingerprint(self.DAY))
        finally:
            wp.PROMPT_ROOTS = old

    def test_the_probe_and_the_counter_read_the_same_roots(self):
        # Not "the same values" -- the same object. They used to be two lists
        # in two files, and prompt-count.py grew a second root that the
        # fingerprint never learned about, so a prompt typed at a background
        # job was counted as presence but did not invalidate the memoised
        # status: the menu bar sat on a reading from minutes earlier with
        # nothing to show it was stale. Identity is what makes that
        # unrepeatable; equality could be restored to drifting tomorrow.
        pc = _load("prompt-count", os.path.join(ROOT, "bin", "prompt-count.py"))
        self.assertIs(wp.PROMPT_ROOTS, wp.wc.PROJECT_ROOTS)
        self.assertIs(pc.PROJECT_ROOTS, wp.wc.PROJECT_ROOTS)


class GithubLiveHistory(unittest.TestCase):
    """The live read of this Mac's own Chrome history.

    The export it supplements is written by a nightly job, so before this
    existed today was the one day with no browser evidence at all: a morning
    spent reviewing pull requests surfaced tomorrow, which is exactly when the
    dot no longer needs it.
    """

    DAY = "2026-08-26"

    def _chrome(self, profiles):
        """A fake Chrome directory. profiles: {name: [(hh, mm, url, title)]}."""
        import sqlite3
        from datetime import datetime
        root = tempfile.mkdtemp()
        for name, rows in profiles.items():
            os.makedirs(os.path.join(root, name))
            c = sqlite3.connect(os.path.join(root, name, "History"))
            c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, "
                      "title TEXT)")
            c.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, "
                      "url INTEGER, visit_time INTEGER)")
            for i, (hh, mm, url, title) in enumerate(rows, start=1):
                when = datetime.strptime(self.DAY, "%Y-%m-%d").replace(
                    hour=hh, minute=mm, tzinfo=wp.LOCAL)
                micros = wp.wc.chrome_micros(when)
                c.execute("INSERT INTO urls VALUES (?,?,?)", (i, url, title))
                c.execute("INSERT INTO visits VALUES (?,?,?)", (i, i, micros))
            c.commit()
            c.close()
        return root

    def _rows(self, profiles, export=""):
        root = self._chrome(profiles)
        act = tempfile.mkdtemp()
        with open(os.path.join(act, f"{self.DAY}.md"), "w") as fh:
            fh.write("| time | source | direction | who | detail |\n"
                     "|---|---|---|---|---|\n" + export)
        old, old_act = wp.wc.CHROME_DIR, wp.ACTIVITY_DIR
        try:
            wp.wc.CHROME_DIR, wp.ACTIVITY_DIR = root, act
            wp._gh_live_cache.clear()
            return wp.github_rows_for(self.DAY)
        finally:
            wp.wc.CHROME_DIR, wp.ACTIVITY_DIR = old, old_act
            wp._gh_live_cache.clear()

    def test_a_pr_read_this_morning_is_visible_now(self):
        rows = self._rows({"Profile 2": [
            (9, 5, "https://github.com/scaledata/sdmain/pull/1", "Fix the thing")]})
        self.assertEqual([(w.strftime("%H:%M"), d) for w, d in rows],
                         [("09:05", "Fix the thing")])

    def test_the_profile_is_found_rather_than_assumed(self):
        # This machine has no "Default" at all -- its only profile is
        # "Profile 2". A hardcoded path reads an empty history forever while
        # looking exactly like somebody who did not browse.
        rows = self._rows({"Profile 2": [
            (9, 5, "https://github.com/scaledata/sdmain/pull/1", "A PR")]})
        self.assertEqual(len(rows), 1)

    def test_the_profile_in_use_wins_when_several_exist(self):
        root = self._chrome({
            "Default": [(9, 0, "https://github.com/a/b/pull/1", "Stale")],
            "Profile 2": [(9, 5, "https://github.com/a/b/pull/2", "Live")]})
        os.utime(os.path.join(root, "Default", "History"), (1, 1))
        old = wp.wc.CHROME_DIR
        try:
            wp.wc.CHROME_DIR = root
            wp._gh_live_cache.clear()
            self.assertEqual([d for _w, d in wp.github_live_rows(self.DAY)],
                             ["Live"])
        finally:
            wp.wc.CHROME_DIR = old
            wp._gh_live_cache.clear()

    def test_no_chrome_at_all_is_not_an_error(self):
        old = wp.wc.CHROME_DIR
        try:
            wp.wc.CHROME_DIR = os.path.join(tempfile.mkdtemp(), "absent")
            wp._gh_live_cache.clear()
            self.assertEqual(wp.github_live_rows(self.DAY), [])
        finally:
            wp.wc.CHROME_DIR = old
            wp._gh_live_cache.clear()

    def test_another_days_visits_are_not_todays_evidence(self):
        root = self._chrome({"Profile 2": [
            (9, 5, "https://github.com/a/b/pull/1", "Yesterday")]})
        old = wp.wc.CHROME_DIR
        try:
            wp.wc.CHROME_DIR = root
            wp._gh_live_cache.clear()
            self.assertEqual(wp.github_live_rows("2026-08-27"), [])
        finally:
            wp.wc.CHROME_DIR = old
            wp._gh_live_cache.clear()

    def test_signing_in_is_not_reviewing_code(self):
        # Same rule the export path applies: the SAML pair fires every weekday
        # morning at the same minute and is not a minute of work.
        rows = self._rows({"Profile 2": [
            (8, 3, "https://github.com/orgs/scaledata/saml/initiate", "Sign In"),
            (19, 2, "https://github.com/login/oauth/authorize?x=1", "Supabase")]})
        self.assertEqual(rows, [])

    def test_docebo_and_sso_visits_are_read_live_too(self):
        rows = self._rows({"Profile 2": [
            (10, 0, "https://rubrik.docebosaas.com/learn/course/9", "Training"),
            (7, 58, "https://sso.rubrik.com/app/rubrik/exk1/sso/saml", "Rubrik SSO")]})
        self.assertEqual([d for _w, d in rows], ["Rubrik SSO", "Training"])

    def test_ordinary_browsing_is_not_picked_up(self):
        rows = self._rows({"Profile 2": [
            (10, 0, "https://www.amazon.com/dp/B08", "Amazon.com")]})
        self.assertEqual(rows, [])

    def test_the_live_row_wins_its_minute_over_the_export(self):
        # Both records describe the same browsing. Counting them both would
        # double the evidence for one visit; the live one is kept because it
        # carries the full URL and second resolution.
        export = ("| 09:05 | chrome | visit | synced | Fix the thing by "
                  "someone · Pull Re |\n")
        rows = self._rows({"Profile 2": [
            (9, 5, "https://github.com/a/b/pull/1", "Fix the thing")]},
            export=export)
        self.assertEqual([d for _w, d in rows], ["Fix the thing"])

    def test_the_export_still_supplies_minutes_the_live_read_missed(self):
        # The export's value is the machine the live read cannot see: GitHub
        # visits synced from the Linux desktop.
        export = ("| 14:20 | chrome | visit | synced | Some other PR by "
                  "someone · Pull Re |\n")
        rows = self._rows({"Profile 2": [
            (9, 5, "https://github.com/a/b/pull/1", "Fix the thing")]},
            export=export)
        self.assertEqual([w.strftime("%H:%M") for w, _d in rows],
                         ["09:05", "14:20"])

    def test_a_titleless_page_falls_back_to_its_url(self):
        rows = self._rows({"Profile 2": [
            (9, 5, "https://github.com/a/b/pull/1", "")]})
        self.assertEqual([d for _w, d in rows],
                         ["https://github.com/a/b/pull/1"])

    def test_the_read_is_memoised_until_chrome_writes_again(self):
        # It sits behind a five-second poll, so re-copying a 58MB database on
        # every call is the difference between cheap and not worth having.
        root = self._chrome({"Profile 2": [
            (9, 5, "https://github.com/a/b/pull/1", "A PR")]})
        old, real_copy = wp.wc.CHROME_DIR, wp.shutil.copy2
        copies = []
        try:
            wp.wc.CHROME_DIR = root
            wp._gh_live_cache.clear()
            wp.shutil.copy2 = lambda *a, **k: (copies.append(a),
                                               real_copy(*a, **k))[1]
            wp.github_live_rows(self.DAY)
            wp.github_live_rows(self.DAY)
            self.assertEqual(len(copies), 1)
        finally:
            wp.shutil.copy2 = real_copy
            wp.wc.CHROME_DIR = old
            wp._gh_live_cache.clear()


class CalendarParsing(unittest.TestCase):
    def test_missing_gcloud_reports_no_source_instead_of_raising(self):
        # 2026-09-01: the vault dump was still the previous day's, so the API
        # path ran -- and this machine has no gcloud at all. The uncaught
        # FileNotFoundError took down the whole `status` call, which the menu
        # bar can only read as "probe did not answer": a red dot on a morning
        # that was working normally. A machine without the SDK is the same
        # answer as an unusable grant, which is None.
        import tempfile, os
        day = wp.now_local().strftime("%Y-%m-%d")
        missing = os.path.join(tempfile.mkdtemp(), "no-calendar-here.md")
        old_file, old_run = wp.CAL_FILE, wp.subprocess.run

        def no_such_binary(args, *a, **kw):
            if args and args[0] == "gcloud":
                raise FileNotFoundError(2, "No such file or directory", "gcloud")
            return old_run(args, *a, **kw)

        try:
            wp.CAL_FILE = missing          # force the vault path to miss
            wp.subprocess.run = no_such_binary
            self.assertIsNone(wp.calendar_events(day))
        finally:
            wp.CAL_FILE, wp.subprocess.run = old_file, old_run

    def test_identical_rows_collapse(self):
        # The free/busy exporter emitted the same 08:15-09:00 block twice, and
        # the tooltip duly rendered "meeting 08:15-09:00 - meeting 08:15-09:00".
        import tempfile, os
        from datetime import datetime
        now = wp.now_local().strftime("%Y-%m-%dT%H:%M")
        day = wp.now_local().strftime("%Y-%m-%d")
        text = f"""---
created: {now}
updated: {now}
---
# Calendar - {day}

| Start | End | Event |
|-------|-----|-------|
| 08:15 | 09:00 | Busy |
| 08:15 | 09:00 | Busy |
| 14:30 | 16:30 | Busy |
"""
        d = tempfile.mkdtemp()
        path = os.path.join(d, "calendar-today.md")
        open(path, "w").write(text)
        old = wp.CAL_FILE
        try:
            wp.CAL_FILE = path
            events = wp.calendar_from_vault(day)
        finally:
            wp.CAL_FILE = old
        self.assertIsNotNone(events)
        spans = [(e["start"], e["end"]) for e in events]
        self.assertEqual(spans, [(495, 540), (870, 990)],
                         "duplicate calendar rows were not collapsed")

    def test_distinct_overlapping_rows_are_kept(self):
        # A real double-booking has different titles and must survive.
        import tempfile, os
        now = wp.now_local().strftime("%Y-%m-%dT%H:%M")
        day = wp.now_local().strftime("%Y-%m-%d")
        text = f"""---
created: {now}
updated: {now}
---
# Calendar - {day}

| Start | End | Event |
|-------|-----|-------|
| 08:15 | 09:00 | Standup |
| 08:15 | 09:00 | Design review |
"""
        d = tempfile.mkdtemp()
        path = os.path.join(d, "calendar-today.md")
        open(path, "w").write(text)
        old = wp.CAL_FILE
        try:
            wp.CAL_FILE = path
            events = wp.calendar_from_vault(day)
        finally:
            wp.CAL_FILE = old
        self.assertEqual(len(events), 2, "a real double-booking was collapsed")

    def _parse(self, table: str, front: str = "") -> list[dict]:
        import tempfile, os
        now = wp.now_local().strftime("%Y-%m-%dT%H:%M")
        day = wp.now_local().strftime("%Y-%m-%d")
        text = (f"---\ncreated: {now}\nupdated: {now}\n{front}---\n"
                f"# Calendar - {day}\n\n{table}")
        path = os.path.join(tempfile.mkdtemp(), "calendar-today.md")
        open(path, "w").write(text)
        old = wp.CAL_FILE
        try:
            wp.CAL_FILE = path
            return wp.calendar_from_vault(day)
        finally:
            wp.CAL_FILE = old

    def test_personal_rows_do_not_count_as_work(self):
        # The whole point of the Calendar column. A therapy session and a
        # football fixture are real appointments but not time on the job; on
        # 2026-08-27 counting them added 110 minutes to the day.
        rows = self._parse(
            "| Start | End | Event | Calendar |\n"
            "|-------|-----|-------|----------|\n"
            "| 08:15 | 09:00 | Talkspace therapy session | personal |\n"
            "| 09:30 | 10:00 | (busy) | work |\n")
        self.assertEqual([r["counts"] for r in rows], [False, True])
        self.assertEqual([r["calendar"] for r in rows], ["personal", "work"])

    def test_untagged_rows_still_count(self):
        # An exporter predating the column emitted the work free/busy feed and
        # nothing else. Defaulting those to "personal" would silently erase
        # every real meeting rather than fix anything.
        rows = self._parse(
            "| Start | End | Event |\n"
            "|-------|-----|-------|\n"
            "| 09:30 | 10:00 | Busy |\n")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["counts"], "an untagged row stopped counting")
        self.assertIsNone(rows[0]["calendar"])

    def test_generated_wins_over_rewritten_updated(self):
        # Obsidian's update-time-on-edit plugin rewrites `updated:` moments
        # after any write and drops the offset while doing it, so freshness has
        # to key off `generated:`, which nothing touches. A stale `updated:`
        # must not be able to reject a file the exporter just wrote.
        import datetime
        stale = (wp.now_local()
                 - datetime.timedelta(hours=wp.CAL_STALE_HOURS + 2))
        day = wp.now_local().strftime("%Y-%m-%d")
        fresh = wp.now_local().isoformat()
        import tempfile, os
        text = (f"---\nupdated: {stale.strftime('%Y-%m-%dT%H:%M')}\n"
                f"generated: {fresh}\n---\n# Calendar - {day}\n\n"
                "| Start | End | Event | Calendar |\n"
                "|-------|-----|-------|----------|\n"
                "| 09:30 | 10:00 | (busy) | work |\n")
        path = os.path.join(tempfile.mkdtemp(), "calendar-today.md")
        open(path, "w").write(text)
        old = wp.CAL_FILE
        try:
            wp.CAL_FILE = path
            rows = wp.calendar_from_vault(day)
        finally:
            wp.CAL_FILE = old
        self.assertIsNotNone(rows, "a fresh generated: was rejected as stale")
        self.assertEqual(len(rows), 1)

    def test_personal_meeting_is_not_presence(self):
        # End to end through the period builder: a two-hour personal block with
        # no prompts in it must not hold a work period open.
        at = wp.now_local().replace(hour=15, minute=30, second=0, microsecond=0)
        personal = [{"start": 14 * 60 + 30, "end": 16 * 60 + 30,
                     "title": "Brighton Tromso", "calendar": "personal",
                     "counts": False}]
        work = [dict(personal[0], calendar="work", counts=True)]
        quiet = wp.covered_by_meeting(
            at, [m for m in personal if m.get("counts", True)])
        self.assertIsNone(quiet, "a personal block still explained a gap")
        self.assertIsNotNone(
            wp.covered_by_meeting(at,
                                  [m for m in work if m.get("counts", True)]),
            "a work block stopped explaining a gap")


class MeetingCuts(unittest.TestCase):
    """Ending a meeting early must end that meeting and no other.

    These matter more than they used to. A cut used to be written only when a
    human clicked "Meeting ended early", a few times a week; the audio watcher
    writes one at the end of every call, so a cut that reached beyond its own
    meeting would now blind the tracker to the rest of the day's calendar
    within one morning.
    """

    def setUp(self):
        self.old = wp.MEETING_CUT
        wp.MEETING_CUT = os.path.join(tempfile.mkdtemp(), "meeting-cut.json")
        self.morning = {"start": 10 * 60, "end": 11 * 60, "title": "Standup",
                        "calendar": "work", "counts": True}
        self.afternoon = {"start": 14 * 60, "end": 15 * 60, "title": "Review",
                          "calendar": "work", "counts": True}

    def tearDown(self):
        wp.MEETING_CUT = self.old

    def at(self, hh, mm):
        return wp.now_local().replace(hour=hh, minute=mm, second=0, microsecond=0)

    def covered(self, when):
        m = wp.covered_by_meeting(when, [self.morning, self.afternoon])
        return m["title"] if m else None

    def test_cut_ends_the_meeting_it_landed_in(self):
        wp.append_meeting_cut(10 * 60 + 30)
        self.assertIsNone(self.covered(self.at(10, 30)),
                          "the cut meeting still covered its own scheduled tail")
        self.assertEqual(self.covered(self.at(10, 15)), "Standup",
                         "the cut retroactively erased time before it")

    def test_cut_does_not_touch_a_later_meeting(self):
        # The regression: one `cut_min` compared against every row gave the
        # afternoon meeting an effective end of 10:30, before its own start,
        # so it could never cover a minute again.
        wp.append_meeting_cut(10 * 60 + 30)
        self.assertEqual(self.covered(self.at(14, 30)), "Review",
                         "ending the standup early also erased the afternoon")

    def test_each_meeting_can_be_cut_independently(self):
        wp.append_meeting_cut(10 * 60 + 30)
        wp.append_meeting_cut(14 * 60 + 20)
        self.assertIsNone(self.covered(self.at(10, 45)))
        self.assertEqual(self.covered(self.at(14, 10)), "Review")
        self.assertIsNone(self.covered(self.at(14, 30)))

    def test_cut_between_meetings_truncates_neither(self):
        # 12:00 is inside nothing, so it is not an early end for anything.
        wp.append_meeting_cut(12 * 60)
        self.assertEqual(self.covered(self.at(10, 30)), "Standup")
        self.assertEqual(self.covered(self.at(14, 30)), "Review")

    def test_earliest_cut_inside_a_meeting_wins(self):
        wp.append_meeting_cut(10 * 60 + 50)
        wp.append_meeting_cut(10 * 60 + 20)
        self.assertIsNone(self.covered(self.at(10, 30)),
                          "a later cut in the same meeting undid an earlier one")

    def test_cuts_expire_with_the_day(self):
        wp.append_meeting_cut(10 * 60 + 30)
        rec = json.load(open(wp.MEETING_CUT))
        rec["day"] = "2001-01-01"
        json.dump(rec, open(wp.MEETING_CUT, "w"))
        self.assertEqual(wp.read_meeting_cuts(), [],
                         "yesterday's cut still applied to today")

    def test_cut_shortens_the_day_total_too(self):
        # The dot and the day total read the same cut, so a meeting that ended
        # early cannot be amber on the menu bar and a full hour in the total.
        cuts = [10 * 60 + 30]
        self.assertEqual(wp.effective_meeting_end(self.morning, cuts), 10 * 60 + 30)
        self.assertEqual(wp.effective_meeting_end(self.afternoon, cuts), 15 * 60)


class ApprovalMatching(unittest.TestCase):
    """The hook must record an approval only when a human answered a prompt."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "approvals.jsonl")
        self.env = dict(os.environ, HOME=self.tmp)
        os.makedirs(os.path.join(self.tmp, ".claude/stats/worktime"),
                    exist_ok=True)

    def run_hook(self, mode, payload):
        subprocess.run([sys.executable, HOOK, mode], input=json.dumps(payload),
                       text=True, capture_output=True, env=self.env)

    def events(self):
        p = os.path.join(self.tmp, ".claude/stats/worktime/approvals.jsonl")
        if not os.path.exists(p):
            return []
        return [json.loads(l) for l in open(p) if l.strip()]

    def test_unattended_tool_records_nothing(self):
        self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                               "duration_ms": 10})
        self.assertEqual(self.events(), [])

    def test_ask_alone_records_nothing(self):
        # The prompt being SHOWN is not evidence anyone was there.
        self.run_hook("ask", {"session_id": "s1",
                              "message": "Claude needs your permission to use Bash"})
        self.assertEqual(self.events(), [])

    def test_matching_pair_records_one(self):
        self.run_hook("ask", {"session_id": "s1",
                              "message": "Claude needs your permission to use Bash"})
        self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                               "duration_ms": 10})
        self.assertEqual(len(self.events()), 1)

    def test_other_session_cannot_consume_the_marker(self):
        # The failure that motivated keying: these hooks are global, so an
        # unrelated auto-approved tool in another window was eating the marker.
        self.run_hook("ask", {"session_id": "s1",
                              "message": "Claude needs your permission to use Bash"})
        self.run_hook("done", {"session_id": "OTHER", "tool_name": "Bash",
                               "duration_ms": 10})
        self.assertEqual(self.events(), [])
        self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                               "duration_ms": 10})
        self.assertEqual(len(self.events()), 1)

    def test_wrong_tool_leaves_the_marker_pending(self):
        self.run_hook("ask", {"session_id": "s1",
                              "message": "Claude needs your permission to use Bash"})
        self.run_hook("done", {"session_id": "s1", "tool_name": "Grep",
                               "duration_ms": 10})
        self.assertEqual(self.events(), [])
        self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                               "duration_ms": 10})
        self.assertEqual(len(self.events()), 1)

    def test_marker_is_consumed_once(self):
        self.run_hook("ask", {"session_id": "s1",
                              "message": "Claude needs your permission to use Bash"})
        for _ in range(3):
            self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                                   "duration_ms": 10})
        self.assertEqual(len(self.events()), 1)

    def test_click_time_backs_out_the_tool_runtime(self):
        # PostToolUse fires when the tool FINISHES; the click was when it began.
        # Measured against the moment `done` was invoked, NOT against a second
        # run: two runs happen at different wall-clock times, so comparing
        # their absolute timestamps tests the clock, not the subtraction.
        import time
        from datetime import datetime
        ask = {"session_id": "s1",
               "message": "Claude needs your permission to use Bash"}
        self.run_hook("ask", ask)
        time.sleep(4)
        at_done = datetime.now(wp.LOCAL)
        self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                               "duration_ms": 3000})
        rec = self.events()[0]
        h, m, s = (int(x) for x in rec["t"].split(":"))
        clicked = at_done.replace(hour=h, minute=m, second=s, microsecond=0)
        back = (at_done - clicked).total_seconds()
        self.assertGreaterEqual(back, 2, f"expected ~3s backed out, got {back}s")
        self.assertLessEqual(back, 5, f"backed out too far: {back}s")

    def test_click_time_can_never_precede_the_ask(self):
        # A 30s tool answered instantly would otherwise be backdated to before
        # the prompt even appeared, which is not a time anyone could have
        # clicked. The runtime subtracted is capped by how long the wait was.
        self.run_hook("ask", {"session_id": "s1",
                              "message": "Claude needs your permission to use Bash"})
        self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                               "duration_ms": 30000})
        e = self.events()[0]
        self.assertEqual(e["waited_sec"], 0)

    def test_subagent_tool_cannot_consume_the_main_threads_marker(self):
        # Verified against real payloads: a subagent's PostToolUse carries the
        # PARENT's session_id, differing only in agent_type. Session keying
        # alone therefore does not isolate it, and a subagent finishing a tool
        # while a prompt was open in the main thread would bank a free
        # approval.
        self.run_hook("ask", {"session_id": "s1", "agent_type": "claude",
                              "message": "Claude needs your permission to use Bash"})
        self.run_hook("done", {"session_id": "s1", "agent_type": "general-purpose",
                               "tool_name": "Bash", "duration_ms": 10})
        self.assertEqual(self.events(), [], "subagent consumed the main marker")
        self.run_hook("done", {"session_id": "s1", "agent_type": "claude",
                               "tool_name": "Bash", "duration_ms": 10})
        self.assertEqual(len(self.events()), 1)

    def test_a_subagents_own_approval_still_records(self):
        # The isolation must not make subagent approvals impossible -- a prompt
        # raised by a subagent is answered by the same human.
        self.run_hook("ask", {"session_id": "s1", "agent_type": "general-purpose",
                              "message": "Claude needs your permission to use Bash"})
        self.run_hook("done", {"session_id": "s1", "agent_type": "general-purpose",
                               "tool_name": "Bash", "duration_ms": 10})
        self.assertEqual(len(self.events()), 1)

    def test_idle_notification_never_plants_a_marker(self):
        # The highest-severity failure available to this hook. Claude Code
        # sends its idle nudge through Notification too ("Claude is waiting
        # for your input", notificationType "idle_prompt"), and that nudge
        # fires BECAUSE nobody has touched the keyboard for a while. Treating
        # it as a pending decision manufactures presence out of an absence.
        self.run_hook("ask", {"session_id": "s1",
                              "message": "Claude is waiting for your input",
                              "notificationType": "idle_prompt"})
        self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                               "duration_ms": 10})
        self.assertEqual(self.events(), [])

    def test_idle_notification_rejected_on_wording_alone(self):
        # notificationType may not always be present, so the wording has to be
        # enough to reject it by itself.
        self.run_hook("ask", {"session_id": "s1",
                              "message": "Claude is waiting for your input"})
        self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                               "duration_ms": 10})
        self.assertEqual(self.events(), [])

    def test_bare_permission_wording_still_matches(self):
        # The bundle also carries a permission message with no tool name; with
        # no tool to match on, session matching has to carry it.
        self.run_hook("ask", {"session_id": "s1",
                              "message": "Claude needs your permission"})
        self.run_hook("done", {"session_id": "s1", "tool_name": "Bash",
                               "duration_ms": 10})
        self.assertEqual(len(self.events()), 1)


DAY = "2026-08-26"


class RecentActivities(unittest.TestCase):
    """The menu bar's list of what the dot's verdict was actually derived from.

    Every source is stubbed rather than read off disk, because the point of
    each assertion is the merging and collapsing -- the parsers that feed it
    have their own tests above.
    """

    def acts(self, prompts=(), slack=(), approvals=(), github=(), focus=(),
             limit=10):
        def sess(ps):
            return {"sessions": [{"label": "s",
                                  "prompts": [{"ts": t, "text": x} for t, x in ps]}]}

        saved = {n: getattr(wp, n) for n in
                 ("full_day", "slack_for", "approval_rows_for", "github_rows_for",
                  "focus_for", "focus_app_by_minute")}
        wp.full_day = lambda _d: sess(prompts)
        wp.slack_for = lambda _d: list(slack)
        wp.approval_rows_for = lambda _d: list(approvals)
        wp.github_rows_for = lambda _d: [
            (wp.datetime.strptime(f"{DAY} {t}", "%Y-%m-%d %H:%M")
               .replace(tzinfo=wp.LOCAL), detail)
            for t, detail in github]
        # `focus` is (HH:MM, app) pairs: the minutes the machine was attended
        # and what held each one. Both stubs are driven from it because the
        # two must agree -- a minute in one and not the other is a state the
        # real pair cannot produce.
        wp.focus_for = lambda _d: [
            wp.datetime.strptime(f"{DAY} {t}", "%Y-%m-%d %H:%M")
              .replace(tzinfo=wp.LOCAL)
            for t, _app in focus]
        wp.focus_app_by_minute = lambda _d: {
            int(t[:2]) * 60 + int(t[3:5]): app for t, app in focus}
        try:
            return wp.recent_activities(DAY, limit=limit)
        finally:
            for n, f in saved.items():
                setattr(wp, n, f)

    def test_all_four_streams_merge_newest_first(self):
        got = self.acts(
            prompts=[("09:00", "fix the dot")],
            slack=[{"t": "09:01:30", "ch": "ruby-dev", "im": False, "text": "on it"}],
            approvals=[{"t": "09:02:00", "tool": "Bash"}],
            github=[("09:03", "Some PR by someone · Pull Re")],
            focus=[("09:01", "Slack")])
        self.assertEqual([a["kind"] for a in got],
                         ["browsing", "approval", "slack", "prompt"])
        self.assertEqual([a["t"] for a in got],
                         ["09:03", "09:02", "09:01", "09:00"])

    def test_each_row_carries_the_absolute_instant_it_happened(self):
        # The widget renders these as ages ("4m", "2h"), which it can only do
        # against the current time -- and it cannot recover one from "09:00"
        # without knowing which day that was. The age is deliberately NOT
        # computed here: this result is memoised behind a file fingerprint, so
        # a "4m" written at this point would still read "4m" an hour later.
        got = self.acts(prompts=[("09:00", "fix the dot")])
        want = wp.datetime.strptime(f"{DAY} 09:00", "%Y-%m-%d %H:%M").timestamp()
        self.assertEqual(got[0]["at"], want)

    def test_repeated_page_collapses_into_one_counted_row(self):
        # The redirect-chain trap: one PR reloaded eight times is one thing
        # that happened. Uncollapsed it filled the entire list on a real day,
        # hiding every other piece of evidence behind a single page.
        got = self.acts(
            prompts=[("09:00", "hello")],
            github=[(f"09:1{i}", "Add Copy Link action by jackie") for i in range(8)])
        self.assertEqual([a["kind"] for a in got], ["browsing", "prompt"])
        self.assertEqual(got[0]["n"], 8)
        self.assertEqual(got[1]["n"], 1)

    def test_same_page_revisited_later_is_its_own_row(self):
        # Only an ADJACENT run merges. Coming back to a PR after doing
        # something else is a second visit, not more of the first one.
        got = self.acts(
            prompts=[("09:05", "meanwhile")],
            github=[("09:00", "the same PR"), ("09:10", "the same PR")])
        self.assertEqual([(a["kind"], a["n"]) for a in got],
                         [("browsing", 1), ("prompt", 1), ("browsing", 1)])

    def test_limit_counts_distinct_rows_not_raw_events(self):
        got = self.acts(
            prompts=[(f"09:{i:02d}", "repeated") for i in range(30)],
            slack=[{"t": f"08:{i:02d}:00", "ch": "c", "im": False, "text": f"m{i}"}
                   for i in range(20)],
            focus=[(f"08:{i:02d}", "Slack") for i in range(20)],
            limit=3)
        # 30 identical prompts are one row, so the limit still has room for
        # two Slack messages behind them rather than being spent on the run.
        self.assertEqual([a["kind"] for a in got], ["prompt", "slack", "slack"])
        self.assertEqual(got[0]["n"], 30)

    def test_last_row_count_is_not_truncated_by_the_limit(self):
        # Stopping the walk at the limit would leave the final row's own
        # duplicates uncounted, so it alone would under-report.
        got = self.acts(prompts=[("09:00", "a"), ("09:01", "b"), ("09:02", "b")],
                        limit=1)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["n"], 2)

    def test_slack_entities_are_unescaped_for_a_native_menu(self):
        # slack_plain() escapes for the dashboard's markup. An AppKit menu
        # renders "&amp;" as five literal characters, so it has to come back.
        got = self.acts(slack=[{"t": "09:00:00", "ch": "dev", "im": False,
                                "text": "Tom &amp; Jerry &lt;3"}],
                        focus=[("09:00", "Slack")])
        self.assertEqual(got[0]["what"], "#dev · Tom & Jerry <3")

    def test_dm_is_labelled_by_type_not_by_its_empty_channel_name(self):
        # The search response carries the other party's user ID, not a name,
        # so `im` is what decides -- exactly as the period summaries key on it.
        got = self.acts(slack=[{"t": "09:00:00", "ch": "", "im": True, "text": "hi"}],
                        focus=[("09:00", "Slack")])
        self.assertEqual(got[0]["what"], "DM · hi")

    def test_a_send_from_a_minute_focus_did_not_count_is_left_out(self):
        # A send is not evidence in its own right -- it rides on the minute
        # having been attended. Sent from the phone, nothing here was
        # attended, and the row would be a newer top entry than the header's
        # "Nm since last activity" can see: two ages for one event that do
        # not agree, which is exactly what the header/list pair is for.
        got = self.acts(prompts=[("09:00", "at the mac")],
                        slack=[{"t": "12:00:00", "ch": "dev", "im": False,
                                "text": "from the train"}],
                        focus=[("09:00", "Ghostty")])
        self.assertEqual([a["kind"] for a in got], ["prompt"])

    def test_a_send_from_an_attended_minute_says_what_that_minute_was_about(self):
        # The other side of it: focus knows the minute was Slack and nothing
        # more, so the send is the only thing that can say what was going on.
        got = self.acts(slack=[{"t": "09:00:00", "ch": "dev", "im": False,
                                "text": "shipping it"}],
                        focus=[("09:00", "Slack")])
        self.assertEqual([(a["kind"], a["what"]) for a in got],
                         [("slack", "#dev · shipping it")])

    def test_prompt_newlines_are_flattened_to_one_line(self):
        got = self.acts(prompts=[("09:00", "first line\n\n  second   line")])
        self.assertEqual(got[0]["what"], "first line second line")

    def test_approval_names_the_tool_it_approved(self):
        got = self.acts(approvals=[{"t": "09:00:00", "tool": "Write"}])
        self.assertEqual(got[0]["what"], "approved Write")

    def test_text_is_capped_so_the_payload_stays_bounded(self):
        got = self.acts(prompts=[("09:00", "x" * 500)])
        self.assertEqual(len(got[0]["what"]), wp.ACTIVITY_TEXT_CHARS)

    def test_desktop_prompts_are_not_listed_as_work(self):
        # They reach the model with the OPPOSITE polarity: a prompt on the
        # other machine is evidence of being away from this job. Listing one
        # under "work" would have the tracker asserting the reverse of what it
        # concluded. Read through the real parsers off a real activity file.
        d = tempfile.mkdtemp()
        with open(os.path.join(d, f"{DAY}.md"), "w") as fh:
            fh.write("| time | source | direction | who | detail |\n|---|---|---|---|---|\n"
                     "| 09:00 | claude | prompt | me | build the iOS app |\n"
                     "| 09:05 | chrome | visit | synced | a PR · scaledata/sdmain |\n")
        saved = (wp.ACTIVITY_DIR, wp.wc.CHROME_DIR, wp.full_day, wp.slack_for,
                 wp.approval_rows_for)
        try:
            wp.ACTIVITY_DIR = d
            # Without this the live reader adds whatever the person running
            # the suite really browsed on DAY, which is neither fixed nor known.
            wp.wc.CHROME_DIR = os.path.join(d, "no-chrome")
            wp._gh_live_cache.clear()
            wp.full_day = lambda _d: {"sessions": []}
            wp.slack_for = lambda _d: []
            wp.approval_rows_for = lambda _d: []
            got = wp.recent_activities(DAY)
        finally:
            (wp.ACTIVITY_DIR, wp.wc.CHROME_DIR, wp.full_day, wp.slack_for,
             wp.approval_rows_for) = saved
            wp._gh_live_cache.clear()
        self.assertEqual([(a["kind"], a["t"]) for a in got], [("browsing", "09:05")])

    def test_desktop_prompts_still_reach_the_model_they_belong_to(self):
        # The exclusion above is about this list, not about the signal: the
        # hole-punching path must still see it, or dropping it from the menu
        # would have quietly changed what the dot reports.
        d = tempfile.mkdtemp()
        with open(os.path.join(d, f"{DAY}.md"), "w") as fh:
            fh.write("| time | source | direction | who | detail |\n|---|---|---|---|---|\n"
                     "| 09:00 | claude | prompt | me | build the iOS app |\n")
        old = wp.ACTIVITY_DIR
        try:
            wp.ACTIVITY_DIR = d
            self.assertEqual(len(wp.desktop_prompts_for(DAY)), 1)
        finally:
            wp.ACTIVITY_DIR = old


class StatusCacheVersion(unittest.TestCase):
    def test_cache_from_an_older_build_is_a_miss_not_a_crash(self):
        # The v1 payload has no "acts" key. Indexing it would raise on the
        # first poll after an upgrade, which the menu bar can only read as
        # "probe did not answer" -- a red dot until the cache happened to be
        # rewritten. A version mismatch takes the path a stale day already had.
        import time
        d = tempfile.mkdtemp()
        path = os.path.join(d, "status-cache.json")
        with open(path, "w") as fh:
            json.dump({"fp": "whatever", "day": DAY, "stamps": [540],
                       "at": time.time(), "last": None}, fh)
        saved = {n: getattr(wp, n) for n in
                 ("STATUS_CACHE", "events_for", "prompts_for", "slack_for",
                  "full_day", "approval_rows_for", "github_rows_for")}
        try:
            wp.STATUS_CACHE = path
            wp.events_for = lambda _d: []
            wp.prompts_for = lambda _d: []
            wp.slack_for = lambda _d: []
            wp.full_day = lambda _d: {"sessions": []}
            wp.approval_rows_for = lambda _d: []
            wp.github_rows_for = lambda _d: []
            last, stamps, acts = wp.live_activity(DAY)
        finally:
            for n, f in saved.items():
                setattr(wp, n, f)
        # Recomputed from the stubs rather than served from the old file.
        self.assertIsNone(last)
        self.assertEqual(stamps, [])
        self.assertEqual(acts, [])
        self.assertEqual(json.load(open(path))["v"], wp.STATUS_CACHE_V)


if __name__ == "__main__":
    unittest.main(verbosity=2)
