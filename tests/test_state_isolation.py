#!/usr/bin/env python3
"""The suite must never be able to write the real state dir.

A test that ends the day (`end_session`) turns special time off, and
SPECIAL_LOG was derived from the real STATE at import -- so patching only
`wp.STATE` left every such test appending an `off` row to the real
special.jsonl, switching the person's special time back to main within seconds
of them turning it on. tests/conftest.py now moves STATE for every test.

Run: pytest tests/test_state_isolation.py
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_STATE = os.path.expanduser("~/.claude/stats/worktime")

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)


class StateIsolation(unittest.TestCase):
    def test_every_state_path_is_outside_the_real_state_dir(self):
        for name in ("STATE", "SPECIAL_LOG", "SPECIAL_TARGETS", "MARKS",
                     "MEETINGS", "SESSION_END", "FOCUS_DIR"):
            path = getattr(wp, name)
            self.assertFalse(
                path == REAL_STATE or path.startswith(REAL_STATE + os.sep),
                f"{name} = {path} is inside the real state dir")

    def test_the_goals_file_follows_the_test_dashboard_dir(self):
        self.assertTrue(wp.GOALS_FILE.startswith(os.environ["WORKTIME_DASHBOARD"]))
