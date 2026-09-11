#!/usr/bin/env python3
"""Tests for the focus signal -- attended foreground time.

Focus replaced Slack sends as the evidence that a stretch in Slack was work.
It was an INTERVAL and is now a point: credit is the ACTIVATION -- the moment
somebody put an app in front -- and the time the app then spends sitting there
earns nothing. That is the one thing easiest to get wrong in the expensive
direction, because a signal that credits time nobody worked still reads as a
plausible day. The invariants that stop it are asserted here.

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
ZED = "dev.zed.Zed"
# Heartbeats with no input in the last 30s but a person still nearby: the
# activation counts, the rows after it do not. For tests about activations.
QUIET = wp.FOCUS_ACTIVE_IDLE_SEC + 15


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
        self.orig_presence = wp.PRESENCE_PATH
        wp.FOCUS_DIR = self.tmp.name
        # Pointed at the temp dir, not merely ignored: the real file is being
        # written by the bar on this machine while the tests run, so a test
        # that forgot to override it would read live state and pass or fail
        # depending on what was in the foreground.
        wp.PRESENCE_PATH = os.path.join(self.tmp.name, "presence.json")

    def tearDown(self):
        wp.FOCUS_DIR = self.orig
        wp.PRESENCE_PATH = self.orig_presence
        self.tmp.cleanup()

    def write(self, rows, day=DAY):
        path = os.path.join(self.tmp.name, f"{day}.jsonl")
        with open(path, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def samples(self, start, n, bundle=SLACK, idle=0, step=30, app="Slack"):
        """`n` rows for the SAME app, `step` apart -- one activation.

        The shape every log already on disk is made of: the bar used to
        heartbeat every 30s whether or not anything changed. Kept because
        reading those logs back correctly is the whole of what makes today's
        change backdatable, so the tests keep exercising it.
        """
        return [{"day": DAY, "t": hms(start + i * step), "app": app,
                 "bundle": bundle, "idle": idle} for i in range(n)]

    def switches(self, start, bundles, step=30, idle=0):
        """One row per entry, each a different app -- `len(bundles)` activations."""
        return [{"day": DAY, "t": hms(start + i * step), "app": b.split(".")[-1],
                 "bundle": b, "idle": idle} for i, b in enumerate(bundles)]

    def minutes(self, day=DAY):
        return [t.hour * 60 + t.minute for t in wp.focus_for(day)]


class TestCredit(FocusCase):
    def test_a_run_without_recent_input_is_one_event_however_long_it_holds(self):
        # 09:00:00 to 09:05:00 in Slack with nobody touching it: one switch,
        # one point. Being in front is not itself credit.
        self.write(self.samples(9 * 3600, 11, idle=QUIET))
        self.assertEqual(self.minutes(), [540])

    def test_a_run_actually_used_earns_every_heartbeat(self):
        # The same five minutes with input throughout: each 30s heartbeat is
        # evidence somebody is using Slack, so a long read keeps counting.
        self.write(self.samples(9 * 3600, 11))
        self.assertEqual(len(wp.focus_for(DAY)), 11)

    def test_a_single_row_is_a_whole_event(self):
        # The exact inversion of the old rule, which needed a successor to have
        # anything to measure and so credited a lone row nothing. A switch is
        # complete on its own.
        self.write(self.samples(9 * 3600, 1))
        self.assertEqual(self.minutes(), [540])

    def test_switching_back_and_forth_earns_each_switch(self):
        # Why real work is not lost with the duration: nobody sits in one
        # window for an hour. Slack, Zed, Slack is three acts, not one.
        self.write(self.switches(9 * 3600, [SLACK, "dev.zed.Zed", SLACK],
                                 step=120))
        self.assertEqual(self.minutes(), [540, 542, 544])

    def test_every_named_app_earns_its_time(self):
        # The allow list is the whole of what focus can ever credit, so each
        # entry is asserted rather than only the one Slack case above. An app
        # silently dropped from the set fails here instead of showing up as a
        # thin day nobody can explain.
        for bundle in sorted(wp.FOCUS_INCLUDE):
            with self.subTest(bundle=bundle):
                self.setUp()
                self.write(self.samples(9 * 3600, 11, bundle=bundle, idle=QUIET))
                self.assertEqual(self.minutes(), [540])
                self.tearDown()

    def test_a_self_raising_app_earns_nothing_on_an_untouched_machine(self):
        # The 2026-09-02 shape, reproduced: the front app becomes Granola while
        # the idle reading is already deep past the threshold, because Granola
        # raised itself at the end of a meeting nobody was sitting through. The
        # allow list would credit every minute of it and name a session after
        # an app that was never chosen.
        for bundle in sorted(wp.FOCUS_SELF_RAISING):
            with self.subTest(bundle=bundle):
                self.setUp()
                self.write(self.samples(9 * 3600, 11, bundle=bundle,
                                        idle=wp.FOCUS_IDLE_SEC + 600))
                self.assertEqual(self.minutes(), [])
                self.tearDown()

    def test_a_self_raising_app_still_earns_when_somebody_is_typing(self):
        # The gate is on the idle reading, not on the app: Granola opened and
        # actually used is ordinary work and must be counted like Slack.
        for bundle in sorted(wp.FOCUS_SELF_RAISING):
            with self.subTest(bundle=bundle):
                self.setUp()
                self.write(self.samples(9 * 3600, 11, bundle=bundle))
                self.assertEqual(self.minutes()[0], 540)
                self.tearDown()

    def test_a_self_raising_flash_between_other_apps_earns_nothing(self):
        # The half of the bug the input gate cannot see: Granola takes the
        # front for a few seconds while somebody types in another window, so
        # idle reads ~0 and the flash looks exactly like a choice. It has to
        # HOLD the front to count, and this one gives it back inside
        # FOCUS_HOLD_SEC.
        self.write(self.switches(9 * 3600, [SLACK, "com.granola.app", SLACK],
                                 step=15))
        self.assertEqual(self.minutes(), [540, 540])
        self.assertNotIn("Granola", wp.focus_app_by_minute(DAY).values())

    def test_a_self_raising_app_that_holds_the_front_is_a_real_session(self):
        # Dropped for flashing past, not for being Granola: kept the moment it
        # stays put longer than a flash.
        self.write(self.switches(9 * 3600, ["com.granola.app", SLACK],
                                 step=wp.FOCUS_HOLD_SEC))
        self.assertEqual(self.minutes(), [540, 540])
        self.assertIn("com.granola.app",
                      [a["bundle"] for _, _, a in wp.focus_windows(DAY)])

    def test_the_last_activation_of_the_day_is_never_a_flash(self):
        # Nothing follows it, so nothing can prove it was given back. Judging
        # it a flash would silently drop whatever was open at the end of every
        # day that ended in Granola.
        self.write(self.samples(9 * 3600, 1, bundle="com.granola.app",
                                app="Granola"))
        self.assertEqual(self.minutes(), [540])

    def test_every_self_raising_app_is_one_the_allow_list_names(self):
        # The narrow rule only ever tightens the broad one. A bundle here that
        # FOCUS_INCLUDE does not carry would be a rule about nothing.
        self.assertTrue(wp.FOCUS_SELF_RAISING <= wp.FOCUS_INCLUDE)

    def test_leisure_earns_nothing(self):
        self.write(self.samples(9 * 3600, 11, bundle="com.netflix.Netflix",
                                app="Netflix"))
        self.assertEqual(self.minutes(), [])

    def test_an_unnamed_work_app_earns_nothing(self):
        # The accepted cost of the allow list, pinned so it stays a decision
        # rather than a surprise: a plausible work tool that nobody has added
        # earns nothing at all. It errs low, which is the direction chosen --
        # the fix is to name it, and this test is where that is documented.
        self.write(self.samples(9 * 3600, 11, bundle="com.figma.Desktop",
                                app="Figma"))
        self.assertEqual(self.minutes(), [])

    def test_the_terminal_earns_nothing(self):
        # Left out for a different reason than leisure is: ambiguity. The same
        # terminal is frontmost for work on
        # this machine and for work on the Linux desktop, and desktop work is
        # counted as absence elsewhere in the probe -- so crediting it here
        # would re-add exactly the hours that subtraction removes.
        self.write(self.samples(9 * 3600, 11, bundle="com.mitchellh.ghostty",
                                app="Ghostty"))
        self.assertEqual(self.minutes(), [])

    def test_empty_bundle_earns_nothing(self):
        # No frontmost app at all. Rare, but it is what a sample taken during
        # a fast user switch can look like.
        self.write(self.samples(9 * 3600, 11, bundle="", app=""))
        self.assertEqual(self.minutes(), [])

    def test_the_browser_earns_nothing(self):
        # Excluded for the reason github_rows_for() gives for refusing to count
        # every Chrome visit: the frontmost app says a browser is open, not
        # what is in it. Chrome's own history is already classified by domain,
        # so crediting the app would both bypass that classification and count
        # personal browsing as work.
        self.write(self.samples(9 * 3600, 11, bundle="com.google.Chrome",
                                app="Google Chrome"))
        self.assertEqual(self.minutes(), [])

    def test_the_lock_screen_earns_nothing(self):
        # A locked Mac does NOT report an empty frontmost app -- it reports
        # com.apple.loginwindow, and typing the password resets idle to zero.
        # So the lock screen passes both of the other honesty checks, and
        # naming it is the only thing that stops unlocking the machine from
        # reading as attended work.
        self.write(self.samples(9 * 3600, 11, bundle="com.apple.loginwindow",
                                app="loginwindow"))
        self.assertEqual(self.minutes(), [])

    def test_system_settings_earns_nothing(self):
        # Somebody IS at the keyboard, so no idle or gap check will ever rule
        # this out -- changing a display setting simply isn't the job. It has
        # to be named, or a few minutes of housekeeping both earns work time
        # and names the period it lands in.
        self.write(self.samples(9 * 3600, 11,
                                bundle="com.apple.systempreferences",
                                app="System Settings"))
        self.assertEqual(self.minutes(), [])

    def test_a_settings_pane_with_its_own_bundle_earns_nothing(self):
        # Adding a printer is the same housekeeping as any other System
        # Settings pane, but it opens as its own app, so excluding System
        # Settings does not reach it. One sample of it was enough to publish an
        # activity row called "Add Printer".
        self.write(self.samples(9 * 3600, 11, bundle="com.apple.print.add",
                                app="Add Printer"))
        self.assertEqual(self.minutes(), [])


class TestIdle(FocusCase):
    """The input gate: an activation counts only if somebody is at the machine.

    The gate is what separates a person reaching for a window from a window
    arriving on its own, and it is the only thing standing between the allow
    list and an app left in front all night.
    """

    def test_an_activation_on_an_untouched_machine_earns_nothing(self):
        # Something brought Slack to the front while the idle reading was
        # already deep past the threshold. Nobody did that.
        self.write(self.samples(9 * 3600, 1, idle=wp.FOCUS_IDLE_SEC + 30))
        self.assertEqual(self.minutes(), [])

    def test_the_threshold_is_inclusive(self):
        # Exactly at the line still counts: the constant names the longest
        # pause that is still reading, so the pause itself has to fit inside.
        self.write(self.samples(9 * 3600, 1, idle=wp.FOCUS_IDLE_SEC))
        self.assertEqual(self.minutes(), [540])

    def test_a_later_touch_does_not_rescue_an_earlier_activation(self):
        # Coming back at 09:30 says nothing about 09:00. Each activation is
        # judged on the reading it was written with, so evidence cannot travel
        # backwards -- which is how an evening of absence used to be redeemed
        # by the next morning's first keystroke.
        self.write(self.samples(9 * 3600, 1, idle=wp.FOCUS_IDLE_SEC + 600))
        self.write(self.switches(9 * 3600 + 1800, ["dev.zed.Zed"]))
        self.assertEqual(self.minutes(), [570])

    def test_a_heartbeat_counts_only_with_input_in_the_last_30s(self):
        # Inclusive at the line: 30s since the last touch still counts, 31s
        # does not -- that second heartbeat is a window left open.
        for idle, expected in ((wp.FOCUS_ACTIVE_IDLE_SEC, 2),
                               (wp.FOCUS_ACTIVE_IDLE_SEC + 1, 1)):
            with self.subTest(idle=idle):
                self.setUp()
                self.write(self.samples(9 * 3600, 1)
                           + self.samples(9 * 3600 + 30, 1, idle=idle))
                self.assertEqual(len(wp.focus_for(DAY)), expected)
                self.tearDown()

    def test_idle_stretches_inside_a_stay_earn_nothing(self):
        # Two minutes used, three minutes untouched, two minutes used: the
        # middle heartbeats are silent and the gap between is not credited.
        idle = [0] * 4 + [30 * i for i in range(2, 8)] + [0] * 4
        self.write([{"day": DAY, "t": hms(9 * 3600 + i * 30), "app": "Slack",
                     "bundle": SLACK, "idle": v} for i, v in enumerate(idle)])
        secs = [t.hour * 3600 + t.minute * 60 + t.second
                for t in wp.focus_for(DAY)]
        self.assertEqual([s for s in secs if 9 * 3600 + 120 <= s < 9 * 3600 + 300],
                         [])


class TestUntouchedFocus(FocusCase):
    """Being in front is an event, not a subscription.

    On 2026-09-02 the machine was woken at 21:22, Slack came to the front, and
    nothing was touched again until 22:02. The log holds eighty consecutive
    Slack samples with the idle reading climbing 2 -> 2397, and the day billed
    forty-one minutes of it. Every other input here is something a person DID
    and stops arriving when they leave; the foreground kept arriving at the
    same rate all night.
    """

    def test_an_untouched_evening_earns_nothing(self):
        self.write(self.samples(9 * 3600, 11, idle=wp.FOCUS_IDLE_SEC + 30))
        self.assertEqual(self.minutes(), [])

    def test_no_app_is_named_for_a_span_nobody_was_present_for(self):
        # A minute credited by focus_for() that focus_app_by_minute() refuses
        # to label is a counted minute with no app against it, and a span
        # labelled without being counted names a session after nobody. Same
        # log, all three answers.
        self.write(self.samples(9 * 3600, 11, idle=wp.FOCUS_IDLE_SEC + 30))
        self.assertEqual(wp.focus_app_by_minute(DAY), {})
        self.assertEqual(wp.focus_apps(DAY, 9 * 3600, 9 * 3600 + 600), [])

    def test_the_gate_applies_to_every_app_the_list_names(self):
        # Not a rule about Slack. An untouched Zed or Obsidian left in front
        # overnight is the identical absence.
        for bundle in sorted(wp.FOCUS_INCLUDE):
            with self.subTest(bundle=bundle):
                self.setUp()
                self.write(self.samples(9 * 3600, 11, bundle=bundle,
                                        idle=wp.FOCUS_IDLE_SEC + 30))
                self.assertEqual(self.minutes(), [])
                self.tearDown()

    def test_the_touch_that_ends_an_absence_starts_earning_again(self):
        # The gate withholds; it does not blacklist. Reaching for the machine
        # again is an ordinary activation, with no residue from the hours
        # before it and none of them redeemed either.
        self.write(self.samples(9 * 3600, 11, idle=wp.FOCUS_IDLE_SEC + 600))
        self.write(self.switches(9 * 3600 + 300, ["dev.zed.Zed", SLACK],
                                 step=60))
        self.assertEqual(self.minutes(), [545, 546])

    def test_reading_inside_the_grace_still_earns(self):
        # Two minutes of no input is reading, not leaving -- the gate opens at
        # FOCUS_IDLE_SEC precisely so a pause between keystrokes costs nothing.
        self.write(self.samples(9 * 3600, 11, idle=wp.FOCUS_IDLE_SEC - 1))
        self.assertEqual(self.minutes(), [540])

    def test_holding_the_front_all_night_earns_only_the_last_touch(self):
        # The 2026-09-02 shape end to end: one touch, then eighty rows with the
        # idle climbing. The activation and the one heartbeat 30s after the
        # touch count; the seventy-eight behind them are the subscription.
        rows = [{"day": DAY, "t": hms(9 * 3600 + i * 30), "app": "Slack",
                 "bundle": SLACK, "idle": i * 30} for i in range(80)]
        self.write(rows)
        self.assertEqual(self.minutes(), [540, 540])

    def test_the_apps_that_do_not_count_are_still_refused(self):
        # The allow list is a separate reason and outlives the gate: an
        # attended hour of Chrome on a leisure page still earns nothing.
        self.write(self.samples(9 * 3600, 11, bundle="com.google.Chrome",
                                app="Google Chrome"))
        self.assertEqual(self.minutes(), [])


class TestTruncation(FocusCase):
    """What used to need a cap, and now needs none.

    The span model had to refuse a pair of rows too far apart, because the time
    between them was the credit -- two rows bracketing a two-hour sleep would
    have billed the sleep. A point has no reach, so the whole class of
    truncation bug is gone rather than guarded against.
    """

    def test_a_sleep_between_two_activations_earns_nothing_between_them(self):
        # The machine slept from 09:00 to 11:00. Two acts, two points, and
        # nothing in between for either of them to claim.
        self.write([
            {"day": DAY, "t": "09:00:00", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "11:00:00", "app": "Zed",
             "bundle": "dev.zed.Zed", "idle": 0},
        ])
        self.assertEqual(self.minutes(), [540, 660])

    def test_a_name_does_not_carry_across_a_sleep(self):
        # Labelling has the reach credit no longer does, so it keeps a cap of
        # its own: Slack was in front when the machine slept and must not put
        # its name on the two hours before somebody came back.
        self.write([
            {"day": DAY, "t": "09:00:00", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "11:00:00", "app": "Zed",
             "bundle": "dev.zed.Zed", "idle": 0},
        ])
        self.assertEqual(wp.focus_apps(DAY, 10 * 3600, 11 * 3600), [])

    def test_rows_from_another_day_are_ignored(self):
        self.write(self.samples(9 * 3600, 11, idle=QUIET))
        self.write([{"day": "2026-03-05", "t": "09:00:00", "app": "Slack",
                     "bundle": SLACK, "idle": 0}])
        self.assertEqual(self.minutes(), [540])

    def test_missing_log_is_not_an_error(self):
        # A machine that has never run the bar has no log. That is a day with
        # no focus evidence, not a crash.
        self.assertEqual(self.minutes("2026-01-01"), [])

    def test_out_of_order_rows_are_sorted_before_activations_are_read(self):
        # Written backwards, Slack -> Zed must still read as Slack first.
        # Unsorted, the run would be split at the wrong place and both apps
        # would be credited twice.
        rows = self.switches(9 * 3600, [SLACK, "dev.zed.Zed"], step=60)
        self.write(list(reversed(rows)))
        self.assertEqual([a["bundle"] for a in wp.focus_activations(DAY)],
                         [SLACK, "dev.zed.Zed"])
        self.assertEqual(self.minutes(), [540, 541])


class TestApps(FocusCase):
    def test_apps_are_ranked_by_time_held(self):
        self.write(self.samples(9 * 3600, 3, bundle="dev.zed.Zed",
                                app="Zed"))
        self.write(self.samples(9 * 3600 + 90, 7))
        self.assertEqual(wp.focus_apps(DAY, 9 * 3600, 9 * 3600 + 600)[0],
                         "Slack")

    def test_apps_outside_the_window_are_excluded(self):
        self.write(self.samples(9 * 3600, 11))
        self.assertEqual(wp.focus_apps(DAY, 14 * 3600, 15 * 3600), [])


class TestAppByMinute(FocusCase):
    def test_labels_the_minutes_an_activation_reaches_over(self):
        # Labelling and credit answer different questions now: one activation
        # earns one point and still names every minute it held the foreground
        # for. A period built from other evidence needs that name.
        self.write(self.samples(9 * 3600, 11, idle=QUIET))
        self.assertEqual(self.minutes(), [540])
        self.assertEqual(sorted(wp.focus_app_by_minute(DAY)),
                         list(range(540, 550)))

    def test_a_minute_goes_to_whichever_app_held_most_of_it(self):
        # Zed holds 09:00:00-09:00:20, Slack holds 09:00:20-09:01:00.
        self.write([
            {"day": DAY, "t": "09:00:00", "app": "Zed",
             "bundle": "dev.zed.Zed", "idle": 0},
            {"day": DAY, "t": "09:00:20", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "09:01:00", "app": "Slack", "bundle": SLACK,
             "idle": 0},
        ])
        self.assertEqual(wp.focus_app_by_minute(DAY)[540], "Slack")

    def test_a_window_straddling_a_boundary_splits_across_both_minutes(self):
        # 09:00:50 to 09:01:20 is 10s in one minute and 20s in the next.
        # Charging the whole window to the minute it started in would hand
        # 09:00 more seconds than the window spent there, and would let a
        # brief app win a minute it barely touched.
        self.write([
            {"day": DAY, "t": "09:00:00", "app": "Zed",
             "bundle": "dev.zed.Zed", "idle": 0},
            {"day": DAY, "t": "09:00:50", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "09:01:20", "app": "Slack", "bundle": SLACK,
             "idle": 0},
        ])
        by_min = wp.focus_app_by_minute(DAY)
        self.assertEqual(by_min[540], "Zed")   # 50s Zed vs 10s Slack
        self.assertEqual(by_min[541], "Slack")

    def test_agrees_with_focus_apps_over_the_same_minute(self):
        self.write(self.samples(9 * 3600, 3, bundle="dev.zed.Zed",
                                app="Zed"))
        self.write(self.samples(9 * 3600 + 60, 3))
        for minute, app in wp.focus_app_by_minute(DAY).items():
            ranked = wp.focus_apps(DAY, minute * 60, minute * 60 + 60)
            self.assertEqual(app, ranked[0])


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
        # The switch, then a heartbeat with input 3s before it.
        self.assertEqual(self.minutes(), [540, 540])


class TestLastFocusInput(FocusCase):
    """The live anchor: the newest moment somebody touched this Mac with a
    work app in front.

    Separate from focus_for() on purpose, and these assert the difference. The
    day's arithmetic wants whole minutes bounded by a closing sample; the dot
    wants the last instant a person was here, and reading the first as the
    second is what put the dot on "quiet 1m" eighteen seconds after a message
    was typed into Slack.
    """

    def at(self, day=DAY):
        return wp.last_focus_input(day)

    def test_a_single_sample_is_enough(self):
        self.write(self.samples(9 * 3600, 1))
        self.assertEqual(self.at().strftime("%H:%M:%S"), "09:00:00")

    def test_a_row_that_earned_nothing_can_still_anchor_the_dot(self):
        # The two questions pulling apart, in one log: an activation on an
        # untouched machine earns no credit, and the machine WAS touched
        # 09:00:00, five minutes before the row was written. The day should
        # not count it; the dot should still know when it happened.
        self.write([{"day": DAY, "t": "09:05:00", "app": "Slack",
                     "bundle": SLACK, "idle": 300}])
        self.assertEqual(self.minutes(), [])
        self.assertEqual(self.at().strftime("%H:%M:%S"), "09:00:00")

    def test_the_live_reading_outranks_the_log(self):
        # The heartbeat that used to keep the dot fresh now lives in
        # presence.json, so a person typing in an app they opened an hour ago
        # -- no new activation anywhere -- must still read as present.
        self.write(self.samples(9 * 3600, 1))
        with open(wp.PRESENCE_PATH, "w") as fh:
            json.dump({"day": DAY, "t": "10:30:00", "app": "Slack",
                       "bundle": SLACK, "idle": 4}, fh)
        self.assertEqual(self.minutes(), [540])
        self.assertEqual(self.at().strftime("%H:%M:%S"), "10:29:56")

    def test_a_live_reading_from_another_day_is_not_this_day(self):
        # The file is overwritten, never rotated, so at 00:01 it still holds
        # yesterday. Carrying that into today would hand every morning an
        # anchor from the night before.
        self.write(self.samples(9 * 3600, 1))
        with open(wp.PRESENCE_PATH, "w") as fh:
            json.dump({"day": "2026-03-03", "t": "23:50:00", "app": "Slack",
                       "bundle": SLACK, "idle": 0}, fh)
        self.assertEqual(self.at().strftime("%H:%M:%S"), "09:00:00")

    def test_idle_is_subtracted_so_an_absence_cannot_hold_the_dot(self):
        # A row reading 300 at 09:05:00 says the last input was 09:00:00, and
        # that is what it must report. Taking the sample's own timestamp would
        # let every heartbeat during an absence renew the dot indefinitely --
        # the machine is never quiet while the app is running.
        self.write([{"day": DAY, "t": "09:05:00", "app": "Slack",
                     "bundle": SLACK, "idle": 300}])
        self.assertEqual(self.at().strftime("%H:%M:%S"), "09:00:00")

    def test_a_night_of_heartbeats_collapses_to_the_last_real_input(self):
        # Rows keep being written every 30s with nobody there, idle climbing.
        # All of them describe the same instant, so the answer is that instant
        # and not the newest row.
        self.write([{"day": DAY, "t": hms(9 * 3600 + i * 30), "app": "Slack",
                     "bundle": SLACK, "idle": i * 30} for i in range(1, 40)])
        self.assertEqual(self.at().strftime("%H:%M:%S"), "09:00:00")

    def test_an_app_off_the_allow_list_never_anchors_the_dot(self):
        # Ghostty is the front app whether the work is here or on the Linux
        # desktop, so it cannot hold the dot green -- the same reason it earns
        # no credit in focus_for().
        self.write(self.samples(9 * 3600, 4, bundle="com.mitchellh.ghostty",
                                app="Ghostty"))
        self.assertIsNone(self.at())

    def test_no_log_reports_nothing_rather_than_a_time(self):
        self.assertIsNone(self.at())

    def test_never_reaches_back_past_the_start_of_the_day(self):
        # The machine was left on overnight, so the first sample's idle spans
        # midnight. Yesterday's input is not this day's evidence.
        self.write([{"day": DAY, "t": "00:02:00", "app": "Slack",
                     "bundle": SLACK, "idle": 9000}])
        self.assertEqual(self.at().strftime("%H:%M:%S"), "00:00:00")


class TestFocusWindows(FocusCase):
    """The pairing the three duration questions share.

    Extracted because the same five lines were written out three times, and a
    rule added to one copy and not the others produces a minute that is counted
    but has no app against it.
    """

    def test_a_window_runs_from_its_activation_to_the_next(self):
        # Slack at 09:00, Zed at 09:05. One window each, and the first
        # ends exactly where the second begins.
        self.write(self.switches(9 * 3600, [SLACK, ZED], step=300))
        self.assertEqual([(lo, hi) for lo, hi, _ in wp.focus_windows(DAY)],
                         [(32400, 32700), (32700, 32700 + wp.FOCUS_LABEL_MAX_SEC)])

    def test_the_window_is_named_by_the_app_that_opened_it(self):
        # The activation names its window; the next one only ends it. Reading
        # the name off the closing row would label every stretch with whatever
        # you switched to next.
        self.write(self.switches(9 * 3600, [SLACK, ZED]))
        self.assertEqual([a["bundle"] for _, _, a in wp.focus_windows(DAY)],
                         [SLACK, ZED])

    def test_a_window_nothing_replaces_stops_describing_the_day(self):
        # The last activation of the day has no successor. Left unbounded it
        # would put one app's name on every minute until midnight.
        self.write(self.samples(9 * 3600, 1))
        (lo, hi, _), = wp.focus_windows(DAY)
        self.assertEqual(hi - lo, wp.FOCUS_LABEL_MAX_SEC)

    def test_a_window_the_day_never_counted_is_never_labelled(self):
        # Credit and labelling are separate, but not independent: an
        # activation nobody made names nothing either, or a period would be
        # described by an app the day refused to count.
        self.write(self.samples(9 * 3600, 4, idle=wp.FOCUS_IDLE_SEC + 30))
        self.assertEqual(list(wp.focus_windows(DAY)), [])


class TestChromeTab(FocusCase):
    """Chrome earns per PAGE, not per app.

    Every other bundle answers "does this count" once, for good; Chrome answers
    it again on every sample, and the answer is allowed to differ between two
    rows thirty seconds apart. The tests below are the ones that would fail if
    that ever collapsed back to an app-level decision in either direction --
    crediting the browser wholesale, or refusing it wholesale.
    """

    def chrome(self, start, n, tab, url, **kw):
        rows = self.samples(start, n, bundle=wp.CHROME_BUNDLE,
                            app="Google Chrome", **kw)
        for r in rows:
            r["tab"], r["url"] = tab, url
        return rows

    def test_a_work_page_in_the_foreground_counts(self):
        self.write(self.chrome(9 * 3600, 3, "Rubrik AI / RAC Policy",
                               "https://docs.google.com/document/d/1BgD",
                               idle=QUIET))
        self.assertEqual(len(wp.focus_for(DAY)), 1)

    def test_a_personal_page_in_the_foreground_earns_nothing(self):
        self.write(self.chrome(9 * 3600, 20, "Hacker News",
                               "https://news.ycombinator.com/"))
        self.assertEqual(wp.focus_for(DAY), [])

    def test_the_title_alone_can_carry_the_keyword(self):
        """The case the URL cannot answer: a Doc id names nothing at all."""
        self.assertTrue(wp.focus_counts({
            "bundle": wp.CHROME_BUNDLE, "tab": "Rubrik AI / RAC Policy",
            "url": "https://docs.google.com/document/d/1BgD-6LSPyG/edit"}))

    def test_the_url_alone_can_carry_the_keyword(self):
        """And the reverse: a PR page titled with somebody's branch name."""
        self.assertTrue(wp.focus_counts({
            "bundle": wp.CHROME_BUNDLE, "tab": "fix flaky retry by someone",
            "url": "https://github.com/scaledata/sdmain/pull/1"}))

    def test_a_question_mark_in_the_title_does_not_hide_the_url(self):
        """Why the two halves are tested separately rather than concatenated."""
        self.assertTrue(wp.focus_counts({
            "bundle": wp.CHROME_BUNDLE, "tab": "Is this thing on?",
            "url": "https://internal.rubrik.com/wiki"}))

    def test_searching_the_company_name_is_not_work(self):
        self.assertFalse(wp.focus_counts({
            "bundle": wp.CHROME_BUNDLE, "tab": "rubrik stock price - Google Search",
            "url": "https://www.google.com/search?q=rubrik+stock+price"}))

    def test_a_workday_page_counts(self):
        self.assertTrue(wp.focus_counts({
            "bundle": wp.CHROME_BUNDLE, "tab": "Manage Absence - Workday",
            "url": "https://wd5.myworkday.com/rubrik/d/inst/13102/x.htmld"}))

    def test_a_workday_title_counts_when_the_url_is_truncated_away(self):
        """The real export row: 80 characters spent, no address left in it."""
        self.assertTrue(wp._work_site_hit(
            "Self Assessment: FY27 OPE Mid-Year Check-In: Oliver Ullman - "
            "Workday — https://w"))

    def test_a_headline_mentioning_workday_is_not_work(self):
        """Why the title rule is anchored: the word has to end the title."""
        self.assertFalse(wp._work_site_hit(
            "The four-day workday is coming - Hacker News — "
            "https://news.ycombinator.com/item?id=1"))

    def test_a_sample_written_before_the_field_existed_earns_nothing(self):
        """Old rows, and any machine that declined the Automation prompt."""
        self.write(self.samples(9 * 3600, 20, bundle=wp.CHROME_BUNDLE,
                                app="Google Chrome"))
        self.assertEqual(wp.focus_for(DAY), [])

    def test_the_row_is_named_by_the_tab_not_by_the_browser(self):
        self.write(self.chrome(9 * 3600, 3, "Rubrik AI / RAC Policy",
                               "https://docs.google.com/document/d/1BgD"))
        self.assertEqual(wp.focus_apps(DAY, 0, 86400),
                         ["Rubrik AI / RAC Policy"])

    def test_a_tab_switch_mid_morning_splits_the_credit(self):
        """The whole point: two Chrome runs, one counted and one not.

        Two activations, because Chrome is keyed by its tab as well as by
        itself -- leaving the doc for Hacker News is a switch even though the
        bundle never changed. One of them counts.
        """
        self.write(self.chrome(9 * 3600, 4, "Rubrik AI / RAC Policy",
                               "https://docs.google.com/document/d/1BgD"))
        self.write(self.chrome(9 * 3600 + 120, 4, "Hacker News",
                               "https://news.ycombinator.com/"))
        self.assertEqual([a["t"] for a in wp.focus_activations(DAY)],
                         ["09:00:00", "09:02:00"])
        self.assertEqual([wp.focus_name(a) for _, _, a in wp.focus_windows(DAY)],
                         ["Rubrik AI / RAC Policy"])

    def test_a_named_app_still_counts_without_any_tab(self):
        """The Chrome branch must not have made the allow list conditional."""
        self.write(self.samples(9 * 3600, 3, idle=QUIET))
        self.assertEqual(len(wp.focus_for(DAY)), 1)


if __name__ == "__main__":
    unittest.main()
