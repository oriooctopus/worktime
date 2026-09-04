#!/usr/bin/env python3
"""Compile and run the Swift SingleFlight tests as part of the pytest suite.

SingleFlight is what stops the status probe piling up. The failure it prevents
is invisible from anywhere else in the suite: nothing on screen is wrong while
it is happening, there are simply fifteen subprocesses answering the same
question, each slow because of the others. So it is run, not read.

No AppKit here -- SingleFlight is plain Foundation, deliberately, so this needs
no window server and cannot be skipped on a headless run.

Run: pytest tests/test_single_flight.py
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAR = os.path.join(ROOT, "bin", "worktime-bar")
SOURCE = os.path.join(BAR, "SingleFlight.swift")
SUITE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "single_flight_tests.swift")


class SingleFlightSuite(unittest.TestCase):
    def test_swift_single_flight_tests_pass(self):
        swiftc = shutil.which("swiftc")
        # The menu bar app only exists on the Mac, so a missing swiftc on the
        # Linux box is not evidence of anything.
        if swiftc is None:
            self.skipTest("no swiftc on this machine")
        out = tempfile.mkdtemp()
        binary = os.path.join(out, "single_flight_tests")
        build = subprocess.run([swiftc, "-O", SOURCE, SUITE, "-o", binary],
                               capture_output=True, text=True)
        self.assertEqual(build.returncode, 0,
                         f"single flight tests did not compile:\n{build.stderr}")
        run = subprocess.run([binary], capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0,
                         f"single flight tests failed:\n{run.stdout}{run.stderr}")


class TheAppUsesIt(unittest.TestCase):
    """The guard only helps if the poll actually goes through it.

    A refresh() that shells out directly compiles, passes every other test, and
    reinstates the pile -- so the wiring is asserted here rather than left to
    a reviewer noticing.
    """

    def test_the_status_probe_is_launched_only_through_the_guard(self):
        main = open(os.path.join(BAR, "main.swift")).read()
        self.assertIn("SingleFlight", main,
                      "the poll no longer goes through the single-flight guard")
        self.assertEqual(main.count('runProbe(["status"])'), 1,
                         "more than one place launches a status probe")

    def test_every_probe_run_shares_one_serial_queue(self):
        """Serial as well as single-flight.

        The status poll is coalesced, but the clicked rows are not -- each one
        is a distinct instruction and none may be dropped. What keeps THEM from
        piling up is that they queue behind each other rather than running at
        once, which also keeps 'mark' ordered before the status read that has
        to see it. A stray DispatchQueue.global for a probe call would undo
        both.
        """
        lines = open(os.path.join(BAR, "main.swift")).read().splitlines()
        callers = 0
        for i, line in enumerate(lines):
            if "runProbe(" not in line or "func runProbe" in line:
                continue
            # Walk back to the dispatch this call sits inside. Structural
            # rather than a count of probeQueue.async: a new caller that picks
            # the wrong queue is exactly what this has to catch, and it would
            # leave the count alone.
            enclosing = next((lines[j] for j in range(i, -1, -1)
                              if ".async {" in lines[j]), "")
            self.assertIn("probeQueue.async", enclosing,
                          f"line {i + 1} runs a probe off the serial queue:"
                          f"\n  {line.strip()}\n  inside: {enclosing.strip()}")
            callers += 1
        self.assertGreater(callers, 0, "found no probe callers to check")


if __name__ == "__main__":
    unittest.main()
