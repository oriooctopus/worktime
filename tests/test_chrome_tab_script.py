#!/usr/bin/env python3
"""The AppleScript the bar uses to read the front Chrome tab.

This is the one part of the bar that cannot be checked by reading it. The
script that shipped looked exactly right -- `(title ...) & tab & (URL ...)`,
with a comment explaining why a tab character is the safe delimiter -- and
returned "Inbox - Gmailtabhttps://mail.google.com/..." on every call, because
inside `tell application "Google Chrome"` the word `tab` is Chrome's own class
and not AppleScript's tab character. osascript exited 0. Nothing anywhere
said a word. Every Chrome sample in the focus log went out with no tab and no
url, which the probe reads as "not a page worth counting", so the browser
earned nothing for as long as the feature existed.

So this runs the real script against the real Chrome and asserts the reply
splits in two. Anything less -- asserting the source contains a delimiter,
mocking osascript -- passes on the exact bug it is here to catch.

Run: pytest tests/test_chrome_tab_script.py
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "bin", "worktime-bar", "main.swift")

# The literals of `p.arguments = [ ... ]`, in order. Swift joins adjacent ones
# with `+` inside a single element, so the elements are rebuilt by splitting on
# the "-e" flags rather than on commas -- a part is never itself "-e".
ARGUMENTS = re.compile(
    r"func chromeActiveTab\(.*?p\.arguments\s*=\s*\[(.*?)\n\s*\]", re.S)
LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')


def script_arguments():
    """The osascript argv the shipped bar builds, read out of the source."""
    block = ARGUMENTS.search(open(MAIN).read())
    assert block, "p.arguments not found in main.swift"
    args, current = [], None
    for raw in LITERAL.findall(block.group(1)):
        literal = raw.replace('\\"', '"').replace("\\\\", "\\")
        if literal == "-e":
            args.append("-e")
            current = None
        elif current is None:
            args.append(literal)
            current = len(args) - 1
        else:
            args[current] += literal
    return args


def chrome_has_a_window():
    got = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to (name of processes) contains '
         '"Google Chrome"'],
        capture_output=True, text=True)
    if got.stdout.strip() != "true":
        return False
    got = subprocess.run(
        ["osascript", "-e",
         'tell application "Google Chrome" to count windows'],
        capture_output=True, text=True)
    return got.returncode == 0 and got.stdout.strip() not in ("", "0")


class ChromeTabScript(unittest.TestCase):
    def test_the_arguments_parse(self):
        args = script_arguments()
        self.assertEqual(args[0], "-e")
        self.assertIn("active tab of front window", " ".join(args))

    def test_the_script_returns_a_title_and_an_address(self):
        if not chrome_has_a_window():
            self.skipTest("Chrome is not running with a window")
        got = subprocess.run(["osascript"] + script_arguments(),
                             capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        parts = got.stdout.strip().split("\t")
        # Two fields, and the second one an address. The bug produced one
        # field with the word "tab" welded into the middle of it.
        self.assertEqual(len(parts), 2, f"reply did not split: {got.stdout!r}")
        self.assertRegex(parts[1], r"^[a-z-]+:")

    def test_the_delimiter_is_not_named_inside_the_tell_block(self):
        """The bug itself, as a rule: Chrome's terminology shadows `tab`."""
        for arg in script_arguments():
            if "tell application" in arg:
                self.assertNotRegex(
                    arg, r"&\s*tab\s*&",
                    "`tab` inside a tell block is Chrome's tab class, not the "
                    "tab character -- build the delimiter outside it")


if __name__ == "__main__":
    unittest.main()
