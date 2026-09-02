#!/usr/bin/env python3
"""Compile and run the Swift CountdownPanel tests as part of the pytest suite.

The panel is the only part of the meeting-end feature a person interacts with,
and the button on it is the only way to say "no, I am still working". A button
that silently does nothing looks identical to no button at all, so it gets an
actual press in a test rather than a reading of the code that wires it up.

Run: pytest tests/test_countdown_panel.py
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "bin", "worktime-bar", "CountdownPanel.swift")
SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "countdown_panel_tests.swift")


class CountdownPanelSuite(unittest.TestCase):
    def test_swift_countdown_panel_tests_pass(self):
        swiftc = shutil.which("swiftc")
        # Same reasoning as the call detector suite: the menu bar app only
        # exists on the Mac, so a missing swiftc on the Linux box is not
        # evidence of anything.
        if swiftc is None:
            self.skipTest("no swiftc on this machine")
        out = tempfile.mkdtemp()
        binary = os.path.join(out, "countdown_panel_tests")
        build = subprocess.run([swiftc, "-O", PANEL, SUITE, "-o", binary],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0,
                         f"countdown panel tests did not compile:\n{build.stderr}")
        # The panel opens real windows, so this needs a window server. In a
        # session without one AppKit aborts rather than returning, which would
        # read as a failing button; skip instead of reporting a lie.
        run = subprocess.run([binary], capture_output=True, text=True, timeout=120)
        if run.returncode != 0 and "NSWindow" in run.stderr:
            self.skipTest("no window server in this session")
        self.assertEqual(run.returncode, 0,
                         f"countdown panel tests failed:\n{run.stdout}{run.stderr}")


if __name__ == "__main__":
    unittest.main()


class NoPanelsOnScreen(unittest.TestCase):
    """No Swift test may put a real window on the machine running the suite.

    This is a regression test for an actual interruption, not a style rule.
    Every `pytest` run used to raise four countdown panels over whatever the
    user was doing, and because they are the same widget the app uses for real,
    the only way to tell them from a genuine prompt was to know the suite
    happened to be running. `present: false` keeps every bit of the coverage --
    an off-screen button still clicks -- and costs nothing.
    """

    def test_no_swift_test_builds_a_visible_panel(self):
        here = os.path.dirname(os.path.abspath(__file__))
        offenders = []
        for name in sorted(os.listdir(here)):
            if not name.endswith("_tests.swift"):
                continue
            text = open(os.path.join(here, name)).read()
            # Constructor calls run over several lines, so each one is read
            # from its opening paren to the closing brace of its last closure
            # argument rather than line by line.
            for i, chunk in enumerate(text.split("CountdownPanel(")[1:]):
                head = chunk[:400]
                if "present: false" not in head:
                    offenders.append(f"{name} (call {i + 1})")
        self.assertEqual(offenders, [],
                         "these build a panel that would appear on screen "
                         "during the test run: " + ", ".join(offenders))
