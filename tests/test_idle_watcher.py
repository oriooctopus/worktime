#!/usr/bin/env python3
"""Compile and run the Swift IdleWatcher tests as part of the pytest suite.

The watcher is what decides whether an absence interrupts anybody, so it is
run rather than read. It no longer opens a window while doing it: the suite
used to press the real button on a real panel, which meant running the tests
put a countdown on screen over whatever the machine was doing. The other half
of the feature -- what the probe does with an absence -- is covered by
IdleExclusion in test_worktime_model.py.

Run: pytest tests/test_idle_watcher.py
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAR = os.path.join(ROOT, "bin", "worktime-bar")
PANEL = os.path.join(BAR, "CountdownPanel.swift")
WATCHER = os.path.join(BAR, "IdleWatcher.swift")
SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "idle_watcher_tests.swift")


class IdleWatcherSuite(unittest.TestCase):
    def test_swift_idle_watcher_tests_pass(self):
        swiftc = shutil.which("swiftc")
        # The menu bar app only exists on the Mac, so a missing swiftc on the
        # Linux box is not evidence of anything.
        if swiftc is None:
            self.skipTest("no swiftc on this machine")
        out = tempfile.mkdtemp()
        binary = os.path.join(out, "idle_watcher_tests")
        build = subprocess.run([swiftc, "-O", PANEL, WATCHER, SUITE, "-o", binary],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0,
                         f"idle watcher tests did not compile:\n{build.stderr}")
        # The watcher still builds an NSApplication, so this needs a window
        # server even though it now shows nothing. AppKit aborts without one,
        # which would read as a broken watcher; skip rather than report a lie.
        run = subprocess.run([binary], capture_output=True, text=True, timeout=120)
        if run.returncode != 0 and "NSWindow" in run.stderr:
            self.skipTest("no window server in this session")
        self.assertEqual(run.returncode, 0,
                         f"idle watcher tests failed:\n{run.stdout}{run.stderr}")


if __name__ == "__main__":
    unittest.main()
