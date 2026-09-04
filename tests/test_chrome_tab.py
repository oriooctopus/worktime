#!/usr/bin/env python3
"""Compile and run the Swift ChromeTab tests as part of the pytest suite.

The parse lived in main.swift until it had shipped two silent failures, which
is where anything unreachable by a test ends up: the Swift suites bring their
own entry point, so a function beside `main` is one nothing can call.

Run: pytest tests/test_chrome_tab.py
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "bin", "worktime-bar", "ChromeTab.swift")
SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "chrome_tab_tests.swift")


class ChromeTabSuite(unittest.TestCase):
    def test_swift_chrome_tab_tests_pass(self):
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            self.skipTest("no swiftc on this machine")
        out = tempfile.mkdtemp()
        binary = os.path.join(out, "chrome_tab_tests")
        build = subprocess.run([swiftc, "-O", SOURCE, SUITE, "-o", binary],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0,
                         f"chrome tab tests did not compile:\n{build.stderr}")
        run = subprocess.run([binary], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0,
                         f"chrome tab tests failed:\n{run.stdout}{run.stderr}")


if __name__ == "__main__":
    unittest.main()
