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
        # Five minutes of presence earn five minutes, not six: the run ends on
        # the 09:05 boundary and covers no part of that minute.
        self.write(self.samples(9 * 3600, 11))
        self.assertEqual(self.minutes(), list(range(540, 545)))

    def test_a_single_sample_credits_nothing(self):
        # One row has no successor, so there is no interval to vouch for. The
        # alternative -- crediting from the sample to now -- is the failure
        # that would let one row before lunch claim the afternoon.
        self.write(self.samples(9 * 3600, 1))
        self.assertEqual(self.minutes(), [])

    def test_every_named_app_earns_its_time(self):
        # The allow list is the whole of what focus can ever credit, so each
        # entry is asserted rather than only the one Slack case above. An app
        # silently dropped from the set fails here instead of showing up as a
        # thin day nobody can explain.
        for bundle in sorted(wp.FOCUS_INCLUDE):
            with self.subTest(bundle=bundle):
                self.setUp()
                self.write(self.samples(9 * 3600, 11, bundle=bundle))
                self.assertEqual(self.minutes(), list(range(540, 545)))
                self.tearDown()

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
    """The gate ships OFF (FOCUS_IDLE_GATES). These force it on, because what
    they cover is the RULE -- what the gate does when something asks for it --
    and that has to keep working whichever way the switch is set. What the
    switch itself does is TestIdleGateOff, below, against the real constant.
    """

    def setUp(self):
        super().setUp()
        self.orig_gate = wp.FOCUS_IDLE_GATES
        wp.FOCUS_IDLE_GATES = True

    def tearDown(self):
        wp.FOCUS_IDLE_GATES = self.orig_gate
        super().tearDown()

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
        self.assertEqual(self.minutes(), [540, 541])

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


class TestIdleGateOff(FocusCase):
    """What ships: idle no longer withholds credit, only the one binary
    question remains.

    Being untouched used to have two separate consequences -- time cut from
    the day, and time that quietly never arrived. The second had no row
    anywhere to explain it, so a minute that was simply never credited was
    indistinguishable from one that was never worked.
    """

    def test_the_gate_ships_off(self):
        self.assertFalse(wp.FOCUS_IDLE_GATES)

    def test_an_untouched_window_still_earns_its_minutes(self):
        # The same log as test_idle_beyond_the_threshold_stops_credit, run
        # against the shipped switch instead of a forced one.
        self.write(self.samples(9 * 3600, 11, idle=wp.FOCUS_IDLE_SEC + 30))
        self.assertEqual(self.minutes(), [540, 541, 542, 543, 544])

    def test_the_gate_is_off_everywhere_or_nowhere(self):
        # A minute credited by focus_for() that focus_app_by_minute() then
        # refuses to label is a counted minute with no app against it, which
        # is what a half-applied switch produces. Same log, both answers.
        self.write(self.samples(9 * 3600, 11, idle=wp.FOCUS_IDLE_SEC + 30))
        labelled = wp.focus_app_by_minute(DAY)
        self.assertEqual(sorted(labelled), self.minutes())
        self.assertEqual(set(labelled.values()), {"Slack"})

    def test_the_apps_that_held_an_untouched_span_are_still_named(self):
        self.write(self.samples(9 * 3600, 11, idle=wp.FOCUS_IDLE_SEC + 30))
        self.assertEqual(wp.focus_apps(DAY, 9 * 3600, 9 * 3600 + 600), ["Slack"])

    def test_the_apps_that_do_not_count_are_still_refused(self):
        # Removing the gate must not have removed the allow list with it: an
        # untouched hour of Chrome earns nothing for a reason of its own.
        self.write(self.samples(9 * 3600, 11, bundle="com.google.Chrome",
                                app="Google Chrome",
                                idle=wp.FOCUS_IDLE_SEC + 30))
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
        self.assertEqual(self.minutes(), list(range(540, 545)))

    def test_missing_log_is_not_an_error(self):
        # A machine that has never run the bar has no log. That is a day with
        # no focus evidence, not a crash.
        self.assertEqual(self.minutes("2026-01-01"), [])

    def test_out_of_order_rows_are_sorted_before_pairing(self):
        # Five heartbeats 30s apart run 09:00:00 to 09:02:00. Written
        # backwards, they must still pair up as neighbours -- unsorted, every
        # pair would have a negative span and earn nothing.
        rows = self.samples(9 * 3600, 5)
        self.write(list(reversed(rows)))
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
    def test_covers_exactly_the_credited_minutes(self):
        self.write(self.samples(9 * 3600, 11))
        self.assertEqual(sorted(wp.focus_app_by_minute(DAY)), self.minutes())

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
        self.assertEqual(self.minutes(), [540])


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
        # The case focus_for() cannot serve: one row, no successor. It still
        # carries a complete claim -- somebody touched this machine at 09:00:00
        # with Slack in front -- and the live dot has nothing else to go on
        # until the next heartbeat, thirty seconds away.
        self.write(self.samples(9 * 3600, 1))
        self.assertEqual(self.minutes(), [])
        self.assertEqual(self.at().strftime("%H:%M:%S"), "09:00:00")

    def test_keeps_seconds_where_focus_for_floors_to_the_minute(self):
        # The screenshot case, reproduced from the log that produced it: the
        # newest sample was 09:01:47 and focus_for()'s newest minute was
        # 09:01:00. Measured at 09:02:05 that is 18s of quiet against 65s --
        # either side of the one-minute unfocused cutoff.
        self.write([
            {"day": DAY, "t": "09:01:12", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "09:01:47", "app": "Slack", "bundle": SLACK,
             "idle": 0},
        ])
        self.assertEqual(wp.focus_for(DAY)[-1].strftime("%H:%M:%S"), "09:01:00")
        self.assertEqual(self.at().strftime("%H:%M:%S"), "09:01:47")

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

    def test_the_three_readers_agree_on_what_counts(self):
        # A run that counts, then a hole longer than one sample may vouch for,
        # then a run that counts. Every reader must see the same two windows.
        self.write(self.samples(9 * 3600, 3)
                   + self.samples(9 * 3600 + 600, 3))
        windows = list(wp.focus_windows(DAY))
        self.assertEqual([(lo, hi) for lo, hi, _ in windows],
                         [(32400, 32430), (32430, 32460),
                          (33000, 33030), (33030, 33060)])
        # focus_apps() spans the same seconds, so the gap is absent from it too.
        self.assertEqual(wp.focus_apps(DAY, 0, 86400), ["Slack"])

    def test_the_window_is_named_by_the_app_that_held_it(self):
        # The EARLIER row names the window; the later one only closes it.
        # Reading the app off the closing row would label every stretch with
        # whatever you switched to next.
        self.write([
            {"day": DAY, "t": "09:00:00", "app": "Slack", "bundle": SLACK,
             "idle": 0},
            {"day": DAY, "t": "09:00:30", "app": "Obsidian",
             "bundle": "md.obsidian", "idle": 0},
        ])
        self.assertEqual([a["app"] for _, _, a in wp.focus_windows(DAY)],
                         ["Slack"])


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
                               "https://docs.google.com/document/d/1BgD"))
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

        Four windows, not three: the window a sample opens is named by that
        sample, so the doc's last row still holds the thirty seconds up to the
        switch. That is the same rule every other app is credited under.
        """
        self.write(self.chrome(9 * 3600, 4, "Rubrik AI / RAC Policy",
                               "https://docs.google.com/document/d/1BgD"))
        self.write(self.chrome(9 * 3600 + 120, 4, "Hacker News",
                               "https://news.ycombinator.com/"))
        self.assertEqual([wp.focus_name(a) for _, _, a in wp.focus_windows(DAY)],
                         ["Rubrik AI / RAC Policy"] * 4)

    def test_a_named_app_still_counts_without_any_tab(self):
        """The Chrome branch must not have made the allow list conditional."""
        self.write(self.samples(9 * 3600, 3))
        self.assertEqual(len(wp.focus_for(DAY)), 1)


if __name__ == "__main__":
    unittest.main()
