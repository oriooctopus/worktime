#!/usr/bin/env python3
"""Compile and run the Swift CallDetector tests as part of the pytest suite.

The call detector is Swift because it feeds a Swift menu bar app, but its rules
-- how long capture must run to count as a meeting, how long silence must last
before the meeting is over -- are the part most likely to be got wrong, and a
Swift test that no runner invokes is a file rather than a test. This wrapper
puts it in the same `pytest tests` as everything else.

Run: pytest tests/test_call_detector.py
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DETECTOR = os.path.join(ROOT, "bin", "worktime-bar", "CallDetector.swift")
SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "call_detector_tests.swift")


class CallDetectorSuite(unittest.TestCase):
    def test_swift_call_detector_tests_pass(self):
        swiftc = shutil.which("swiftc")
        # Skipped rather than failed off a Mac: the Linux box runs the
        # exporters and this suite, but the menu bar app only exists on the
        # Mac, so there is nothing there for a missing swiftc to be evidence of.
        if swiftc is None:
            self.skipTest("no swiftc on this machine")
        out = tempfile.mkdtemp()
        binary = os.path.join(out, "call_detector_tests")
        build = subprocess.run([swiftc, "-O", DETECTOR, SUITE, "-o", binary],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0,
                         f"call detector tests did not compile:\n{build.stderr}")
        run = subprocess.run([binary], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0,
                         f"call detector tests failed:\n{run.stdout}{run.stderr}")


if __name__ == "__main__":
    unittest.main()
