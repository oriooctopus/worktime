#!/usr/bin/env python3
"""The two halves of the menu describing the same moment.

The header line is derived live on every poll; the period list and the sessions
under it come from the snapshot. Everything here is about the ways those two
can end up describing different moments while both look perfectly reasonable.

Two failures, both seen: a snapshot signed with a fingerprint taken AFTER its
periods were derived claims to cover an event it does not, so status() finds a
match and never rebuilds -- "0m since last activity" above a newest period
three quarters of an hour old. And a newest period marked current purely for
being last carries the widget's "now" long after the work stopped.

Run: pytest tests/test_snapshot_freshness.py
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

DAY = "2026-09-03"


def row(t, kind="prompt", what="x", n=1):
    return {"t": t, "kind": kind, "what": what, "n": n}


def period(start, end, what=""):
    return {"start": start, "end": end, "len": end - start, "what": what}


class SnapshotFingerprint(unittest.TestCase):
    """What the published snapshot signs itself with.

    An empty day on purpose: the derivation is exercised everywhere else, and
    what is asserted here is only which fingerprint comes out the other end.
    """

    NAMES = ("snapshot_path", "activity_fingerprint", "calendar_events")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {n: getattr(wp, n) for n in self.NAMES}
        wp.snapshot_path = lambda day: os.path.join(self.tmp, f"{day}.json")

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(wp, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def published(self):
        with open(wp.snapshot_path(DAY)) as fh:
            return json.load(fh)

    def test_publishes_the_fingerprint_the_caller_took(self):
        # The caller took it before reading the events it passed, so its
        # fingerprint is the one that describes them. Recomputing here would
        # sign the file with a later state of the world than it holds.
        wp.activity_fingerprint = lambda day: "taken-after"
        wp.write_vault_snapshot(DAY, [], "taken-before")
        self.assertEqual(self.published()["fp"], "taken-before")

    def test_without_one_it_takes_its_own_before_deriving(self):
        # calendar_events is the first read of the day's inputs, so a
        # fingerprint that still says "before" when the file lands is one taken
        # ahead of every derivation rather than at the end beside the record.
        started = []
        wp.activity_fingerprint = lambda day: "after" if started else "before"

        def calendar_events(day):
            started.append(day)
            return []

        wp.calendar_events = calendar_events
        wp.write_vault_snapshot(DAY, [])
        self.assertTrue(started, "the derivation never ran")
        self.assertEqual(self.published()["fp"], "before")


class SessionIsCurrent(unittest.TestCase):
    """Which session, if any, the widget draws its "now" on."""

    def sessions(self, live):
        return wp.group_sessions([row("13:38", kind="focus", what="Slack")],
                                 [period(818, 819, "Slack")], live=live)

    def test_newest_period_is_current_while_the_run_is_live(self):
        self.assertTrue(self.sessions(True)[0]["current"])

    def test_newest_period_is_not_current_once_the_run_has_lapsed(self):
        # The exact shape of the bug: one focus event at 13:38, the day gone
        # quiet, and the row still saying "now" at half past two.
        self.assertFalse(self.sessions(False)[0]["current"])

    def test_an_older_period_is_never_current(self):
        out = wp.group_sessions([row("13:38"), row("09:10")],
                                [period(550, 555), period(818, 819)],
                                live=True)
        self.assertEqual([s["current"] for s in out], [True, False])

    def test_uncounted_runs_are_never_current(self):
        # Minutes no period covers are minutes the day total left out, so
        # nothing in them is the work in progress.
        out = wp.group_sessions([row("13:38")], [], live=True)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["counted"])
        self.assertFalse(out[0]["current"])


if __name__ == "__main__":
    unittest.main()
