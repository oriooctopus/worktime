#!/usr/bin/env python3
"""Compile and run the Swift TrackPanel tests as part of the pytest suite.

The panel is the whole of the double-press feature that a person touches, and
the two things it can get wrong are both silent. A picker row that maps to the
wrong rule banks a different claim than the one it named, and there is nothing
in the period list afterwards that says which rule ran. A count that falls back
to a default when the field is empty puts minutes in the day that nobody typed.
Neither shows up in a reading of the wiring, so the field is really typed into
and the button is really pressed.

Run: pytest tests/test_track_panel.py
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "bin", "worktime-bar", "TrackPanel.swift")
MAIN = os.path.join(ROOT, "bin", "worktime-bar", "main.swift")
SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "track_panel_tests.swift")


class TrackPanelSuite(unittest.TestCase):
    def test_swift_track_panel_tests_pass(self):
        swiftc = shutil.which("swiftc")
        # The menu bar app only exists on the Mac, so a missing swiftc on the
        # Linux box is not evidence of anything.
        if swiftc is None:
            self.skipTest("no swiftc on this machine")
        out = tempfile.mkdtemp()
        binary = os.path.join(out, "track_panel_tests")
        build = subprocess.run([swiftc, "-O", PANEL, SUITE, "-o", binary],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0,
                         f"track panel tests did not compile:\n{build.stderr}")
        # The panel builds real AppKit windows, so this needs a window server.
        # Without one AppKit aborts rather than returning, which would read as
        # a failing button; skip instead of reporting a lie.
        run = subprocess.run([binary], capture_output=True, text=True, timeout=120)
        if run.returncode != 0 and "NSWindow" in run.stderr:
            self.skipTest("no window server in this session")
        self.assertEqual(run.returncode, 0,
                         f"track panel tests failed:\n{run.stdout}{run.stderr}")

    def test_reopening_asks_whether_a_panel_is_visible(self):
        """The guard in front of opening a second panel, read from the source.

        A source check rather than a behavioural one because the only way to
        exercise it for real is to build the whole menu bar app and press the
        hot key twice, which needs Accessibility a test runner does not have.
        It is pinned anyway, because this is the bug that killed the shortcut
        on the day it shipped and it is completely silent: `trackPanel != nil`
        is true of a panel that was cancelled minutes ago, so the first cancel
        was the last time the panel ever appeared. The key still fired, the
        app still came to the front, and nothing was logged anywhere.
        """
        src = open(MAIN).read()
        self.assertIn("trackPanel?.isOnScreen == true", src,
                      "showTrackPanel must ask whether a panel is VISIBLE "
                      "before declining to open one -- holding a reference to "
                      "a withdrawn panel is not the same thing")
        self.assertNotIn("if trackPanel != nil", src,
                         "a bare nil check here means a cancelled panel "
                         "blocks every later double press")


if __name__ == "__main__":
    unittest.main()
