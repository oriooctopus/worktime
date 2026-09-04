#!/usr/bin/env python3
"""The QoS of the two bar queues that spawn child processes.

A spawned child inherits the disk policy of the thread that spawned it, and a
throttled child is starved rather than merely slowed: the probe's fingerprint
walk -- every transcript under both profile roots -- measured 140s under an
explicitly throttled policy against 2.3s without one, where the bar kills a
probe at PROBE_TIMEOUT_SEC (30s) and paints the dot red. Whether utility QoS
by itself imposes that tier was never confirmed, so this pins a margin rather
than a proven fix: the queues that spawn children stay out of the tiers the
system is documented to starve, and the 140s figure says what the downside
would be if they did not.

The tier itself is not readable back: getiopolicy_np reports only explicit
iopolicy overrides, and returns IOPOL_IMPORTANT on a utility queue that is in
fact being throttled. There is no in-process observation to assert on, and the
real behaviour needs a saturated disk to show itself. What can be pinned is
the input that decided it, which is what this does -- the two queues that
spawn children must not sit in a tier the system is willing to starve.

Run: pytest tests/test_bar_queue_qos.py
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "bin", "worktime-bar", "main.swift")

# Tiers the system starves first. .default and .userInitiated are unthrottled;
# .utility and .background are not.
STARVED = {"utility", "background"}

# Both spawn a child: probeQueue runs the probe, and the focus queue runs
# osascript to read the front window.
SPAWNING = {"worktime.probe", "worktime.focus"}


def queue_qos(source: str) -> dict[str, str]:
    """label -> qos, for every DispatchQueue built with both in one call."""
    return {
        m.group(1): m.group(2)
        for m in re.finditer(
            r'DispatchQueue\(label:\s*"([^"]+)",\s*qos:\s*\.(\w+)\)', source)
    }


class BarQueueQoS(unittest.TestCase):
    def setUp(self):
        with open(MAIN) as fh:
            self.qos = queue_qos(fh.read())

    def test_both_spawning_queues_are_present(self):
        """A rename that this test cannot see would make it pass vacuously."""
        self.assertEqual(SPAWNING & set(self.qos), SPAWNING,
                         f"queues found: {sorted(self.qos)}")

    def test_child_spawning_queues_are_not_in_a_starved_tier(self):
        for label in sorted(SPAWNING):
            with self.subTest(queue=label):
                self.assertNotIn(
                    self.qos[label], STARVED,
                    f"{label} spawns a child process at .{self.qos[label]},"
                    " whose I/O the system will starve under load")


if __name__ == "__main__":
    unittest.main()
