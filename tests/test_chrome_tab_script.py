#!/usr/bin/env python3
"""How the bar addresses Chrome when it asks what page is in front.

This is the one part of the bar that cannot be checked by reading it, and it
has now shipped three silent failures in a row.

The first two were in the text protocol it used to speak. The script looked
exactly right -- `(title ...) & tab & (URL ...)` -- and returned
"Inbox - Gmailtabhttps://mail.google.com/..." on every call, because inside
`tell application "Google Chrome"` the word `tab` is Chrome's own class and
not AppleScript's tab character. Then the parse trimmed the reply before
splitting it and ate the leading delimiter of every untitled page.

The third is what this file is mostly here for, because it is the one the
other two cannot happen without. `tell application "Google Chrome"` addresses
an Apple Event at a NAME, and more than one process answers to that name: a
launchd agent keeps a CDP browser on :9222, and `playwright --browser=chrome`
launches the same binary again for every automated run. The event went to
whichever the system picked. On 2026-09-22 the frontmost browser was on
pools.events and the bar recorded "Log In / http://localhost:3000/" from the
automation instance -- for every Chrome sample since the evening before.

Every one of those exited 0. Nothing anywhere said a word. So the assertion
that matters is structural and unconditional: the bar must address Chrome by
pid. The live half below then runs the real reader against every real Chrome
and checks each one answers for itself.

Run: pytest tests/test_chrome_tab_script.py
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAR = os.path.join(ROOT, "bin", "worktime-bar")
SOURCE = os.path.join(BAR, "ChromeTab.swift")

CHROME_EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# A harness around the shipped reader. Prints one line per pid it was given.
HARNESS = """
import Foundation

@main
enum Harness {
    static func main() {
        for arg in CommandLine.arguments.dropFirst() {
            let pid = pid_t(arg)!
            if let tab = chromeActiveTab(pid: pid) {
                print("OK\\t\\(pid)\\t\\(tab.title)\\t\\(tab.url)")
            } else {
                print("NONE\\t\\(pid)")
            }
        }
    }
}
"""


def chrome_pids():
    """Every top-level Chrome process, newest last. Helpers are not Chrome."""
    got = subprocess.run(["pgrep", "-f", CHROME_EXE],
                         capture_output=True, text=True)
    pids = []
    for pid in got.stdout.split():
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid],
                             capture_output=True, text=True).stdout
        if cmd.startswith(CHROME_EXE) and "Helper" not in cmd:
            pids.append(pid)
    return pids


def read_tabs(pids):
    """Run the shipped reader against these pids, or None if it will not build."""
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        return None
    out = tempfile.mkdtemp()
    harness = os.path.join(out, "harness.swift")
    with open(harness, "w") as f:
        f.write(HARNESS)
    binary = os.path.join(out, "harness")
    build = subprocess.run(
        [swiftc, "-O", SOURCE, harness, "-framework", "ScriptingBridge",
         "-o", binary],
        capture_output=True, text=True)
    assert build.returncode == 0, f"reader did not compile:\n{build.stderr}"
    run = subprocess.run([binary] + list(pids), capture_output=True, text=True)
    assert run.returncode == 0, f"reader crashed:\n{run.stdout}{run.stderr}"
    rows = {}
    for line in run.stdout.splitlines():
        parts = line.split("\t")
        rows[parts[1]] = tuple(parts[2:]) if parts[0] == "OK" else None
    return rows


class ChromeAddressing(unittest.TestCase):
    """The bug as a rule: never ask for Chrome by name."""

    def test_the_reader_takes_a_pid(self):
        source = open(SOURCE).read()
        self.assertRegex(
            source, r"func chromeActiveTab\(pid: pid_t\)",
            "the reader must be addressed at a process, not at a name")

    def test_no_bar_source_addresses_chrome_by_name(self):
        offenders = []
        for name in sorted(os.listdir(BAR)):
            if not name.endswith(".swift"):
                continue
            body = open(os.path.join(BAR, name)).read()
            # Inside a comment is fine -- that is where this bug is explained.
            for line in body.splitlines():
                if line.lstrip().startswith("//"):
                    continue
                if re.search(r'tell application \\?"Google Chrome', line):
                    offenders.append(f"{name}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "an Apple Event addressed at the name 'Google Chrome' reaches "
            "whichever Chrome the system picks, and a CDP or Playwright "
            "browser is routinely running alongside the real one:\n"
            + "\n".join(offenders))


class ChromeReadsPerProcess(unittest.TestCase):
    """The live half: every running Chrome answers for itself."""

    def test_each_chrome_reports_its_own_page(self):
        pids = chrome_pids()
        if not pids:
            self.skipTest("Chrome is not running")
        rows = read_tabs(pids)
        if rows is None:
            self.skipTest("no swiftc on this machine")
        read = {p: t for p, t in rows.items() if t}
        if not read:
            # A refused Apple Event looks exactly like a browser with no
            # windows, and neither is this test's business to fix.
            self.skipTest("no Chrome answered -- no windows, or Automation "
                          "permission is not granted to the test binary")
        for pid, (title, url) in read.items():
            self.assertRegex(url, r"^[a-z][a-z0-9+.-]*:",
                             f"pid {pid} returned no address: {url!r}")

        # The bug itself, when the machine happens to be able to show it: two
        # Chromes must not report the same page. They have separate profiles
        # and separate windows, so identical answers mean one of them was
        # never actually asked.
        if len(read) > 1:
            urls = [u for _, u in read.values()]
            self.assertEqual(
                len(set(urls)), len(urls),
                "two Chrome processes reported the same page, so the event "
                f"was addressed at a name and not a pid: {read}")


if __name__ == "__main__":
    unittest.main()
