#!/usr/bin/env python3
"""Nothing in the test suite may put a window on the machine running it.

This is a regression test for a real interruption, not a style rule. The Swift
suites build AppKit objects in-process, and one of them built four countdown
panels the normal way -- so every `pytest` run dropped four windows on top of
whatever the user was doing. They are the same widget the app prompts with, so
there was no way to tell a test artefact from the tracker genuinely asking
something; the idle prompt had just been switched off for interrupting, and the
suite quietly went on doing it in its place.

The failure is invisible from inside a test run: the panels appear, the checks
pass, the suite reports green. Nobody reading the output would ever see it. So
the rule is enforced by reading the sources rather than by watching a run, and
it is enforced on the WHOLE class of ways to show a window -- not just the one
that caused the trouble -- because the next test to do this will not be
another CountdownPanel.

Run: pytest tests/test_no_ui_in_tests.py
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

# Every AppKit call that puts something in front of a person. Ordering a window
# out, closing it, or building one without showing it are all fine -- the tests
# need to construct real objects, that was never the problem.
PRESENTING = [
    ("orderFront", "orders a window on screen"),
    ("orderFrontRegardless", "orders a window on screen"),
    ("makeKeyAndOrderFront", "orders a window on screen and takes focus"),
    ("makeKeyWindow", "takes focus"),
    ("orderWindow", "orders a window on screen"),
    ("runModal", "blocks the machine behind a modal"),
    ("beginSheet", "attaches a visible sheet"),
    ("NSAlert", "is a dialog"),
    ("NSApp.activate", "pulls the app in front of what is being used"),
    ("setActivationPolicy(.regular)", "gives the test a Dock icon and windows"),
]

# Constructors that show themselves unless told not to. The value is the
# argument that suppresses it, which the call must pass.
SELF_PRESENTING = {"CountdownPanel(": "present: false",
                   "TrackPanel(": "present: false"}

# Each of those, and the guard it must keep on the app side. The string search
# above would go on passing if the parameter were renamed or dropped -- the
# Swift suites would stop compiling, but only once somebody ran them -- so the
# parameter's existence and its default are pinned here too. TrackPanel is the
# sharper case of the two: it is the one surface in this app that deliberately
# takes the keyboard, so a test that showed it would not merely appear, it
# would swallow whatever was being typed at the time.
PANEL_GUARDS = {
    "CountdownPanel.swift": "orderFrontRegardless",
    "TrackPanel.swift": "makeKeyAndOrderFront",
}


def swift_test_sources() -> list[str]:
    return sorted(n for n in os.listdir(HERE) if n.endswith("_tests.swift"))


class NoUIInTests(unittest.TestCase):
    def test_there_are_swift_suites_to_check(self):
        # A guard that silently checks nothing is worse than no guard: it
        # reports green forever. If the suites are renamed, this fails first
        # and says so, rather than the rest of the file passing vacuously.
        self.assertTrue(swift_test_sources(),
                        f"no *_tests.swift found in {HERE} -- if they were "
                        "renamed, update swift_test_sources()")

    def test_no_swift_test_presents_a_window(self):
        offenders = []
        for name in swift_test_sources():
            text = open(os.path.join(HERE, name)).read()
            for line_no, line in enumerate(text.splitlines(), 1):
                code = line.split("//", 1)[0]
                for call, why in PRESENTING:
                    if call in code:
                        offenders.append(f"{name}:{line_no} {call}() {why}")
        self.assertEqual(offenders, [], "these would appear on screen during "
                                        "a test run:\n  " + "\n  ".join(offenders))

    def test_no_swift_test_builds_a_self_presenting_view(self):
        offenders = []
        for name in swift_test_sources():
            text = open(os.path.join(HERE, name)).read()
            for ctor, suppressor in SELF_PRESENTING.items():
                # A constructor call runs over several lines, so each one is
                # read from its opening paren to well past its last argument
                # rather than line by line.
                for i, chunk in enumerate(text.split(ctor)[1:], 1):
                    if suppressor not in chunk[:400]:
                        offenders.append(
                            f"{name}: {ctor} call {i} does not pass "
                            f"`{suppressor}`")
        self.assertEqual(offenders, [], "these build a window that would "
                                        "appear during a test run:\n  "
                                        + "\n  ".join(offenders))

    def test_the_suppressor_is_a_real_argument_of_the_real_type(self):
        # The contract, read as one: visible by default for the app,
        # suppressible for a test, and the showing itself guarded by the flag
        # rather than merely accompanied by it.
        for name, shows in PANEL_GUARDS.items():
            src = open(os.path.join(os.path.dirname(HERE), "bin",
                                    "worktime-bar", name)).read()
            self.assertRegex(
                src, r"present:\s*Bool\s*=\s*true",
                f"{name} must keep a `present: Bool = true` parameter -- the "
                "app relies on the default, the tests rely on being able to "
                "pass false")
            self.assertRegex(
                src, r"if present\s*\{[^}]*" + shows,
                f"{name} must only put itself on screen when `present`")


if __name__ == "__main__":
    unittest.main()
