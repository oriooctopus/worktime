#!/usr/bin/env python3
"""Tests for the two per-person overrides: long reading, and extra apps.

Both exist because the defaults encode one person's working day. Credit lands
on the ACTIVATION because that person switches windows hundreds of times an
hour, and the allow list omits Obsidian because that person's vault holds a
grocery list. Neither is a fact about time tracking; both are facts about a
setup, and somebody who reads one article for forty minutes in a vault that is
only ever work is erased by them.

What is asserted here, in order of what would be expensive to get wrong:

1. OFF changes nothing. The default path must produce the identical day it
   produced before either setting existed.
2. ON, a stay earns only while the machine says somebody is there. This is the
   span model's old failure -- forty-one minutes billed to an untouched Slack
   window -- and the whole claim of the feature is that gating each event on
   its own row's idle reading is what the span model was missing.

Run: pytest tests/test_long_read.py
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

spec_wc = importlib.util.spec_from_file_location(
    "wc", os.path.join(ROOT, "bin", "worktime_common.py"))
wc = importlib.util.module_from_spec(spec_wc)
spec_wc.loader.exec_module(wc)

DAY = "2026-03-04"
SLACK = "com.tinyspeck.slackmacgap"
OBSIDIAN = "md.obsidian"
CHROME = "com.google.Chrome"

# Four minutes between events, five before a stay stops being read. The
# defaults; named here so a test that depends on one says which.
STRIDE = 240
MAX_IDLE = 300


def hms(sec):
    return "{:02d}:{:02d}:{:02d}".format(sec // 3600, sec % 3600 // 60, sec % 60)


class LongReadCase(unittest.TestCase):
    """Writes a real focus log and reads it back through the probe.

    Same approach as test_focus.py: point FOCUS_DIR at a temp dir rather than
    stub focus_rows(), so the parsing and ordering are exercised too. The
    module-level settings are overridden here rather than read from a profile,
    because the profile on this machine is the developer's own and a test that
    read it would pass or fail according to somebody's personal config.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_dir = wp.FOCUS_DIR
        self.orig_presence = wp.PRESENCE_PATH
        self.orig_long = wp._LONG_READ
        self.orig_include = wp._FOCUS_INCLUDE_RESOLVED
        wp.FOCUS_DIR = self.tmp.name
        wp.PRESENCE_PATH = os.path.join(self.tmp.name, "presence.json")
        wp._LONG_READ = None

    def tearDown(self):
        wp.FOCUS_DIR = self.orig_dir
        wp.PRESENCE_PATH = self.orig_presence
        wp._LONG_READ = self.orig_long
        wp._FOCUS_INCLUDE_RESOLVED = self.orig_include
        self.tmp.cleanup()

    def enable(self, max_idle_sec=MAX_IDLE, stride_sec=STRIDE):
        wp._LONG_READ = {"max_idle_sec": max_idle_sec, "stride_sec": stride_sec}

    def write(self, rows, day=DAY):
        with open(os.path.join(self.tmp.name, day + ".jsonl"), "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def stay(self, start, n, bundle=SLACK, idle=0, step=30, tab=None):
        """`n` heartbeat rows on the SAME thing -- one activation, one stay.

        `idle` may be an int (constant) or a list read per row, which is how a
        reader who wanders off and comes back is expressed.
        """
        rows = []
        for i in range(n):
            r = {"day": DAY, "t": hms(start + i * step),
                 "app": bundle.split(".")[-1], "bundle": bundle,
                 "idle": idle[i] if isinstance(idle, list) else idle}
            if tab is not None:
                r["tab"] = tab
                r["url"] = tab
            rows.append(r)
        return rows

    def secs(self, day=DAY):
        """Focus events as seconds-of-day, which is what the assertions read."""
        return [t.hour * 3600 + t.minute * 60 + t.second
                for t in wp.focus_for(day)]


class TestOffByDefault(LongReadCase):
    """Without long_read, a stay earns on the tight 30s input default."""

    def test_an_actively_used_stay_earns_every_heartbeat(self):
        # Forty minutes in one window with input throughout: one event per
        # 30s heartbeat, so the stay reads as the forty minutes it was.
        self.write(self.stay(9 * 3600, 80))
        self.assertEqual(self.secs(), [9 * 3600 + 30 * i for i in range(80)])

    def test_a_stay_without_recent_input_is_a_single_event(self):
        # Reading without touching anything for 45s at a time is past the
        # default cutoff -- that is what long_read exists to loosen.
        self.write(self.stay(9 * 3600, 80, idle=45))
        self.assertEqual(self.secs(), [9 * 3600])

    def test_obsidian_earns_nothing(self):
        # The built-in allow list omits it, and omitting it is the default for
        # everybody who has not said their vault is only ever work.
        self.write(self.stay(9 * 3600, 20, bundle=OBSIDIAN))
        self.assertEqual(self.secs(), [])


class TestReadingEarns(LongReadCase):
    def test_a_stay_earns_one_event_per_stride(self):
        # Twenty minutes of reading with input throughout. The activation at
        # 09:00 plus an event every four minutes after it -- enough to keep a
        # five-minute cutoff from closing the bout, which is the entire point.
        self.enable()
        self.write(self.stay(9 * 3600, 41))  # 09:00:00 -> 09:20:00
        self.assertEqual(
            self.secs(),
            [9 * 3600, 9 * 3600 + 240, 9 * 3600 + 480,
             9 * 3600 + 720, 9 * 3600 + 960, 9 * 3600 + 1200])

    def test_events_never_fall_further_apart_than_the_probe_can_chain(self):
        # The invariant that makes the feature work at all rather than merely
        # produce more events: consecutive events inside a read must stay under
        # GAP_AFTER, or the bout chainer splits the read into fragments and the
        # day is no longer than it was.
        self.enable()
        self.write(self.stay(9 * 3600, 121))  # a full hour
        got = self.secs()
        gaps = [b - a for a, b in zip(got, got[1:])]
        self.assertTrue(gaps, "an hour of reading produced no events to chain")
        self.assertLess(max(gaps), wp.GAP_AFTER * 60)


class TestParkingEarnsNothing(LongReadCase):
    """The failure this feature would be a bug if it reintroduced."""

    def test_an_untouched_window_earns_only_its_activation(self):
        # The 2026-09-02 evening, in miniature: Slack in front for an hour with
        # nobody in the room. Idle climbs past the limit and every subsequent
        # row declines to vouch, so the hour earns the switch that started it
        # and not one second more.
        self.enable()
        self.write(self.stay(9 * 3600, 120,
                             idle=[30 * i for i in range(120)]))
        self.assertEqual([s for s in self.secs() if s > 9 * 3600 + 600], [])

    def test_a_reader_who_leaves_and_returns_earns_neither_the_absence(self):
        # Ten minutes reading, twenty minutes away, ten minutes reading. The
        # gap must be genuinely empty -- not bridged by the rows that keep
        # arriving throughout it, which is precisely how the span model billed
        # an empty chair.
        self.enable()
        idle = [0] * 20 + [30 * i for i in range(40)] + [0] * 20
        self.write(self.stay(9 * 3600, 80, idle=idle))
        away_start, away_end = 9 * 3600 + 600, 9 * 3600 + 1800
        during = [s for s in self.secs()
                  if away_start + MAX_IDLE < s < away_end]
        self.assertEqual(during, [], "credited an absence: {}".format(during))
        self.assertTrue([s for s in self.secs() if s >= away_end],
                        "never resumed crediting after they came back")

    def test_returning_earns_one_event_not_one_per_missed_stride(self):
        # A subtle way to get this wrong: hold the anchor still through the
        # absence and then, on the first vouching row, pay out every stride
        # that elapsed while nobody was there.
        self.enable()
        idle = [0] * 2 + [30 * i for i in range(60)] + [0] * 2
        self.write(self.stay(9 * 3600, 64, idle=idle))
        returned = [s for s in self.secs() if s > 9 * 3600 + 1800]
        self.assertLessEqual(len(returned), 2)


class TestSwitchingIsUnaffected(LongReadCase):
    def test_a_switch_restarts_the_stay(self):
        # Quick navigation must not inherit the previous thing's anchor and
        # collect a dwell event it never sat still for. Four switches four
        # minutes apart earn four activations and nothing else.
        self.enable()
        rows = []
        for i, bundle in enumerate([SLACK, "dev.zed.Zed", SLACK, "dev.zed.Zed"]):
            rows += self.stay(9 * 3600 + i * 240, 1, bundle=bundle)
        self.write(rows)
        self.assertEqual(self.secs(),
                         [9 * 3600, 9 * 3600 + 240,
                          9 * 3600 + 480, 9 * 3600 + 720])

    def test_a_new_chrome_tab_is_a_new_stay(self):
        # Chrome earns per page, so changing tab is a switch by every measure
        # the rest of the probe uses, and the read clock starts again with it.
        self.enable()
        self.write(self.stay(9 * 3600, 2, bundle=CHROME,
                             tab="github.com/x/y/pull/1")
                   + self.stay(9 * 3600 + 60, 2, bundle=CHROME,
                               tab="github.com/x/y/pull/2"))
        self.assertEqual(self.secs(), [9 * 3600, 9 * 3600 + 60])


class TestExtraApps(LongReadCase):
    def test_a_named_app_earns_its_foreground(self):
        # The override that makes a notes-only vault countable. Same rows as
        # the default-off test above, one line of profile apart.
        wp._FOCUS_INCLUDE_RESOLVED = wp.FOCUS_INCLUDE | {OBSIDIAN}
        self.write(self.stay(9 * 3600, 3, bundle=OBSIDIAN, step=120))
        self.assertEqual(self.secs(), [9 * 3600, 9 * 3600 + 120, 9 * 3600 + 240])

    def test_it_adds_and_never_removes(self):
        # An override that replaced the list would silently drop Slack and
        # Zoom for anybody who used it, and the loss would look like a quiet
        # week rather than like a misconfiguration.
        wp._FOCUS_INCLUDE_RESOLVED = wp.FOCUS_INCLUDE | {OBSIDIAN}
        self.write(self.stay(9 * 3600, 1, bundle=SLACK))
        self.assertEqual(self.secs(), [9 * 3600])


class TestProfileParsing(unittest.TestCase):
    """The accessors, read straight from a written profile file."""

    def profile(self, obj):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(obj, tmp)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return wc.load_profile(tmp.name)

    def test_absent_means_off(self):
        self.assertIsNone(wc.long_read(self.profile({})))
        self.assertEqual(wc.focus_extra_apps(self.profile({})), set())

    def test_present_but_disabled_means_off(self):
        # Somebody who tried it and turned it back off leaves the block behind
        # with its numbers in it; that must read as off, not as off-with-
        # settings-that-do-something.
        self.assertIsNone(wc.long_read(self.profile(
            {"long_read": {"enabled": False, "max_idle_sec": 900}})))

    def test_defaults_fill_in_around_enabled(self):
        cfg = wc.long_read(self.profile({"long_read": {"enabled": True}}))
        self.assertEqual(cfg["max_idle_sec"], wc.DEFAULT_LONG_READ_IDLE_SEC)
        self.assertEqual(cfg["stride_sec"], wc.DEFAULT_LONG_READ_STRIDE_SEC)

    def test_a_nonsense_setting_is_refused_rather_than_ignored(self):
        # Silently falling back to the default would leave somebody who set
        # zero believing they had configured something.
        with self.assertRaises(wc.ProfileError):
            wc.long_read(self.profile(
                {"long_read": {"enabled": True, "stride_sec": 0}}))
        with self.assertRaises(wc.ProfileError):
            wc.focus_extra_apps(self.profile({"focus_extra_apps": "md.obsidian"}))


if __name__ == "__main__":
    unittest.main()
