#!/usr/bin/env python3
"""The red dot's debug rows: the wording, and the wiring that reaches it.

Two halves. The Swift suite drives ProbeFailure's strings, which are the whole
feature -- a stall and a traceback have to read differently, and reproducing
either by hand needs a wedged filesystem or a broken probe. The Python half
pins the wiring: a failure that is described perfectly and never reaches the
menu is the state this replaced.

Run: pytest tests/test_probe_failure.py
"""

import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAR = os.path.join(ROOT, "bin", "worktime-bar")
SOURCE = os.path.join(BAR, "ProbeRun.swift")
SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "probe_failure_tests.swift")
PLIST = os.path.join(ROOT, "deploy", "launchd", "com.oliver.worktime-bar.plist")


class ProbeFailureSuite(unittest.TestCase):
    def test_swift_probe_failure_tests_pass(self):
        swiftc = shutil.which("swiftc")
        # The menu bar app only exists on the Mac, so a missing swiftc on the
        # Linux box is not evidence of anything.
        if swiftc is None:
            self.skipTest("no swiftc on this machine")
        out = tempfile.mkdtemp()
        binary = os.path.join(out, "probe_failure_tests")
        build = subprocess.run([swiftc, "-O", SOURCE, SUITE, "-o", binary],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0,
                         f"probe failure tests did not compile:\n{build.stderr}")
        run = subprocess.run([binary], capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0,
                         f"probe failure tests failed:\n{run.stdout}{run.stderr}")


class TheMenuShowsIt(unittest.TestCase):
    """The failure has to survive the trip from runProbe to the menu.

    Every assertion here is a step that has been the whole bug on its own: a
    probe whose failure is discarded at the call site, a summary that never
    reaches the status row, rows built off a cache key that cannot see them.
    """

    def setUp(self):
        self.main = open(os.path.join(BAR, "main.swift")).read()

    def test_the_status_poll_keeps_the_failure_it_got(self):
        self.assertIn("self.probeFail = fail", self.main,
                      "the failed poll's detail is dropped instead of kept")

    def test_the_status_row_says_which_failure_it_was(self):
        self.assertIn("s.why = fail.summary", self.main,
                      "the status row no longer shows the failure's summary")
        self.assertNotIn("probe did not answer", self.main,
                         "the one sentence that fits every failure is back")

    def test_a_good_poll_clears_the_failure(self):
        # Otherwise the detail rows outlive the red dot: the day comes back on
        # screen with a stale traceback pinned under it.
        self.assertIn("self.probeFail = nil", self.main,
                      "a successful poll leaves the last failure showing")

    def test_the_streak_is_counted_and_reset(self):
        self.assertIn("self.probeFailures += 1", self.main,
                      "consecutive failures are not counted")
        self.assertIn("self.probeFailures = 0", self.main,
                      "the failure streak is never reset")

    def test_the_debug_rows_are_in_the_menu_cache_key(self):
        # The menu is rebuilt only when this key changes. Rows left out of it
        # are rows that freeze at whatever the first failing poll said.
        key = re.search(r"let key = \(\[.*?\]( \+ \w+)?", self.main, re.S)
        self.assertIsNotNone(key, "the menu cache key moved")
        self.assertIn("debug", key.group(0),
                      "the debug rows are not in the key, so they cannot refresh")

    def test_the_dot_gets_a_label_only_when_broken(self):
        # Red is the state noticed with the menu shut, and it is the only one
        # that earns text in the menu bar; every other state is a bare dot.
        self.assertIn('if s.state == "broken", let f = probeFail {', self.main,
                      "the menu bar label is not gated on the broken state")
        self.assertIn("f.tag", self.main, "the menu bar label does not say what broke")

    def test_the_diagnostics_are_reachable_from_the_menu(self):
        for sel in ("copyProbeDiagnostics", "openBarLog"):
            with self.subTest(action=sel):
                self.assertEqual(self.main.count(sel), 2,
                                 f"{sel} is not both offered and implemented")


class TheLogPathMatchesLaunchd(unittest.TestCase):
    """"Open Bar Log" has to open the file launchd is actually writing.

    Two copies of one path in two file formats, which is a drift waiting to
    happen: a plist edited to move the log leaves a menu row opening a file
    that stopped being written months ago, and nothing about that looks wrong.
    """

    def test_bar_log_is_the_plists_standard_error_path(self):
        found = re.search(r'let BAR_LOG = "([^"]+)"',
                          open(os.path.join(BAR, "main.swift")).read())
        self.assertIsNotNone(found, "the app no longer names the bar log")
        with open(PLIST, "rb") as fh:
            want = plistlib.load(fh)["StandardErrorPath"]
        self.assertEqual(found.group(1), want,
                         "the menu opens a different file than launchd writes")


class TheBuildIncludesIt(unittest.TestCase):
    def test_probe_run_is_compiled_into_the_app(self):
        # A file the build script does not name is a file whose absence is a
        # compile error the moment anything references it -- but the suite
        # above compiles it directly and would pass regardless.
        self.assertIn("ProbeRun.swift", open(os.path.join(BAR, "build.sh")).read(),
                      "ProbeRun.swift is not in the build")


if __name__ == "__main__":
    unittest.main()
