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

