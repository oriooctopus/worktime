#!/usr/bin/env python3
"""The Mac-to-desktop mailbox: a request in, a reply out, nothing else run.

Run: pytest tests/test_wsl_inbox.py
"""

import importlib.util
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, file):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "bin", file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inbox = load("wsl_inbox", "wsl-inbox.py")
askmod = load("wsl_ask", "wsl-ask.py")


class Mailbox(unittest.TestCase):
    def setUp(self):
        self.dash = tempfile.mkdtemp()
        # New chunked layout (see activity-export.py): one dir per day, one
        # .md per local hour that had events.
        os.makedirs(os.path.join(self.dash, "activity", "2026-09-06"))
        open(os.path.join(self.dash, "activity", "2026-09-06", "09.md"), "w").close()
        self.ran = []

    def tearDown(self):
        shutil.rmtree(self.dash)

    def runner(self, argv):
        self.ran.append(argv)
        return 0, "fine\n"

    def test_request_is_answered_and_removed(self):
        rid = askmod.ask(self.dash, "status")
        self.assertEqual(inbox.process(self.dash, self.runner), [rid])
        self.assertEqual(os.listdir(os.path.join(self.dash, "wsl-inbox")), [])
        reply = open(os.path.join(self.dash, "wsl-outbox", f"{rid}.md")).read()
        self.assertEqual(self.ran, inbox.ACTIONS["status"])
        self.assertIn("→ exit 0", reply)
        self.assertIn("2026-09-06", reply)

    def test_unknown_action_runs_nothing(self):
        rid = askmod.ask(self.dash, "rm -rf ~")
        inbox.process(self.dash, self.runner)
        reply = open(os.path.join(self.dash, "wsl-outbox", f"{rid}.md")).read()
        self.assertEqual(self.ran, [])
        self.assertIn("unknown action", reply)

    def test_heartbeat_written_on_first_run(self):
        now = datetime.now(timezone.utc) - timedelta(minutes=7)
        inbox.process(self.dash, self.runner, now=now)
        self.assertIn("(7m ago)", askmod.heartbeat(self.dash))

    def test_no_heartbeat_says_so(self):
        self.assertIn("no heartbeat yet", askmod.heartbeat(self.dash))

    def test_heartbeat_throttle_writes_at_0_and_10_min_but_not_5(self):
        # This timer runs every 1 minute; rewriting the heartbeat every
        # single run would create an Obsidian Sync version every minute --
        # see HEARTBEAT_FRESHNESS_SECONDS. Only a run that's stale by 10min
        # (or finds no/malformed prior heartbeat) actually rewrites.
        outbox = os.path.join(self.dash, "wsl-outbox")
        os.makedirs(outbox, exist_ok=True)
        T0 = datetime.now(timezone.utc)
        self.assertTrue(inbox.maybe_write_heartbeat(outbox, T0))
        path = os.path.join(outbox, "heartbeat.md")
        mtime0 = os.stat(path).st_mtime_ns
        with open(path) as f:
            content0 = f.read()

        # +5min: still fresh, no rewrite.
        wrote5 = inbox.maybe_write_heartbeat(outbox, T0 + timedelta(minutes=5))
        self.assertFalse(wrote5)
        self.assertEqual(os.stat(path).st_mtime_ns, mtime0)
        with open(path) as f:
            self.assertEqual(f.read(), content0)

        # +10min: stale (>= 10min), rewrite happens.
        wrote10 = inbox.maybe_write_heartbeat(outbox, T0 + timedelta(minutes=10))
        self.assertTrue(wrote10)
        with open(path) as f:
            self.assertNotEqual(f.read(), content0)

    def test_missing_command_is_reported_not_raised(self):
        code, out = inbox.run_command(["definitely-not-a-command-xyz"])
        self.assertEqual(code, 127)


if __name__ == "__main__":
    unittest.main()
