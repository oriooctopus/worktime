#!/usr/bin/env python3
"""Compile and run the Swift session-row tests as part of the pytest suite.

The grouped activity view is two labels per row and nothing else, so those two
strings are the entire view. Everything else about the menu can be checked by
reading the probe's payload; these can only be checked by running the Swift
that formats them, and a screenshot is the only other way to see them at all.

Run: pytest tests/test_activity_session_strings.py
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "bin", "worktime-bar", "ActivitySession.swift")
SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "activity_session_tests.swift")


class ActivitySessionSuite(unittest.TestCase):
    def test_swift_session_string_tests_pass(self):
        swiftc = shutil.which("swiftc")
        # Skipped rather than failed off a Mac, same as the call detector
        # suite: the Linux box runs the exporters and these tests, and the menu
        # bar app does not exist there for a missing swiftc to be evidence of.
        if swiftc is None:
            self.skipTest("no swiftc on this machine")
        out = tempfile.mkdtemp()
        binary = os.path.join(out, "activity_session_tests")
        build = subprocess.run([swiftc, "-O", SOURCE, SUITE, "-o", binary],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0,
                         f"session string tests did not compile:\n{build.stderr}")
        run = subprocess.run([binary], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0,
                         f"session string tests failed:\n{run.stdout}{run.stderr}")


if __name__ == "__main__":
    unittest.main()
