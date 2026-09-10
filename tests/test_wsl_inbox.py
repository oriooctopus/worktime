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
        os.makedirs(os.path.join(self.dash, "activity"))
        open(os.path.join(self.dash, "activity", "2026-09-06.md"), "w").close()
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
        self.assertIn("2026-09-06.md", reply)

    def test_unknown_action_runs_nothing(self):
        rid = askmod.ask(self.dash, "rm -rf ~")
        inbox.process(self.dash, self.runner)
        reply = open(os.path.join(self.dash, "wsl-outbox", f"{rid}.md")).read()
        self.assertEqual(self.ran, [])
        self.assertIn("unknown action", reply)

    def test_heartbeat_written_every_run(self):
        now = datetime.now(timezone.utc) - timedelta(minutes=7)
        inbox.process(self.dash, self.runner, now=now)
        self.assertIn("(7m ago)", askmod.heartbeat(self.dash))

    def test_no_heartbeat_says_so(self):
        self.assertIn("no heartbeat yet", askmod.heartbeat(self.dash))

    def test_missing_command_is_reported_not_raised(self):
        code, out = inbox.run_command(["definitely-not-a-command-xyz"])
        self.assertEqual(code, 127)


if __name__ == "__main__":
    unittest.main()
