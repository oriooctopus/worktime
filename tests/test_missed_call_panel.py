#!/usr/bin/env python3
"""Compile and run the Swift MeetingPromptPanel tests as part of the pytest suite.

Run: pytest tests/test_missed_call_panel.py
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "bin", "worktime-bar", "MissedCallPanel.swift")
SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "missed_call_panel_tests.swift")


class MissedCallPanelSuite(unittest.TestCase):
    def test_swift_missed_call_panel_tests_pass(self):
        swiftc = shutil.which("swiftc")
        # Same reasoning as the call detector suite: the menu bar app only
        # exists on the Mac, so a missing swiftc on the Linux box is not
        # evidence of anything.
        if swiftc is None:
            self.skipTest("no swiftc on this machine")
        out = tempfile.mkdtemp()
        binary = os.path.join(out, "missed_call_panel_tests")
        build = subprocess.run([swiftc, "-O", PANEL, SUITE, "-o", binary],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0,
                         f"missed call panel tests did not compile:\n{build.stderr}")
        # The panel opens real windows, so this needs a window server. In a
        # session without one AppKit aborts rather than returning, which would
        # read as a failing button; skip instead of reporting a lie.
        run = subprocess.run([binary], capture_output=True, text=True, timeout=120)
        if run.returncode != 0 and "NSWindow" in run.stderr:
            self.skipTest("no window server in this session")
        self.assertEqual(run.returncode, 0,
                         f"missed call panel tests failed:\n{run.stdout}{run.stderr}")


if __name__ == "__main__":
    unittest.main()

