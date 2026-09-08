#!/usr/bin/env python3
"""prompts_for() must answer repeated asks for a day without respawning.

The probe's own cost is not where its failures come from. A status poll asks
seven separate times for today's prompts, and before this cache each ask paid
a fresh `python3 prompt-count.py` -- seven interpreter startups where one
answer was wanted. Under memory pressure a cold Python launch sits in
uninterruptible disk wait faulting extension modules back in, and seven of
those in series overrun the menu bar's 30s watchdog: SIGTERM, exit 15, red dot.

So the property under test is a count of subprocesses, not a duration. A timing
assertion would pass on an unloaded machine while the shipped arrangement --
loaded, swapping -- still blew the watchdog.

Run: pytest tests/test_prompts_for_cache.py
"""

import importlib.util
import json
import os
import subprocess
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "bin", "worktime-probe.py")
DAY = "2024-03-05"

# One session, two prompts, in the shape prompt-count.py emits. `times` rather
# than `prompts` because the latter is capped at 20 and reading it is what once
# made every long day look like it ended at its 20th message.
COUNTER_OUTPUT = json.dumps(
    {"sessions": [{"times": ["09:15:00", "14:30:45"]}]}
)


def load_probe():
    """A module object of its own per test, and so a cache of its own.

    Deliberately not `import` -- that would hand every test the same module and
    the first test's memoised day would decide what the rest measured.
    """
    spec = importlib.util.spec_from_file_location("probe_under_test", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCounter:
    """Stands in for `subprocess.run`, counting only prompt-count launches."""

    def __init__(self, stdout=COUNTER_OUTPUT, returncode=0):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, cmd, *a, **k):
        assert any("prompt-count" in str(c) for c in cmd), cmd
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode,
                                           stdout=self.stdout, stderr="")


class PromptsForCache(unittest.TestCase):
    def install(self, counter):
        """Give the probe module its own `subprocess`, not a patched global.

        `probe.subprocess` is the one shared module object every test file in
        the suite imports, so assigning `.run` on it rewrites `subprocess.run`
        process-wide -- which silently broke eleven unrelated tests before this
        was a namespace of its own.
        """
        self.probe.subprocess = types.SimpleNamespace(
            run=counter, CompletedProcess=subprocess.CompletedProcess,
        )
        return counter

    def setUp(self):
        self.probe = load_probe()
        self.counter = self.install(FakeCounter())

    def test_seven_asks_for_one_day_spawn_one_counter(self):
        """The whole point: a poll's seven call sites, one interpreter."""
        for _ in range(7):
            self.probe.prompts_for(DAY)
        self.assertEqual(len(self.counter.calls), 1)

    def test_repeat_asks_return_the_same_prompts(self):
        """Cheaper is worthless if it is also different."""
        first = self.probe.prompts_for(DAY)
        self.assertEqual([t.strftime("%H:%M:%S") for t in first],
                         ["09:15:00", "14:30:45"])
        self.assertEqual(self.probe.prompts_for(DAY), first)

    def test_distinct_days_are_counted_separately(self):
        """Memoising on the day means each real day still gets asked once."""
        self.probe.prompts_for(DAY)
        self.probe.prompts_for("2024-03-06")
        self.assertEqual(len(self.counter.calls), 2)

    def test_callers_cannot_corrupt_each_others_answer(self):
        """lru_cache hands every caller one object; a list would be shared.

        Two of the four call sites build on what they get back, so a returned
        list would let the first caller's edit rewrite what the second sees --
        a corruption that only appears once the cache exists, which is why the
        cached value is a tuple and `prompts_for` unpacks a fresh list.
        """
        first = self.probe.prompts_for(DAY)
        first.append("mutated")
        self.assertEqual(len(self.probe.prompts_for(DAY)), 2)

    def test_a_crashed_counter_still_raises_rather_than_reading_as_idle(self):
        """The cache must not turn a failure into a cached empty day.

        Treating a dead counter as "no prompts today" is the bug that had the
        tracker reporting idle with full confidence for hours; caching that
        answer would make one crash stick for the rest of the run.
        """
        self.install(FakeCounter(stdout="", returncode=1))
        with self.assertRaises(RuntimeError):
            self.probe.prompts_for(DAY)
        self.install(self.counter)
        self.assertEqual(len(self.probe.prompts_for(DAY)), 2)


if __name__ == "__main__":
    unittest.main()
