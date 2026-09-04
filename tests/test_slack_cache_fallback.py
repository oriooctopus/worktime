#!/usr/bin/env python3
"""Tests for what slack_for() does when search.messages misbehaves.

The menu bar's dot has exactly one red state -- "the probe did not answer" --
and both ways this call can fail have earned it. A truncated body raises
http.client.IncompleteRead, which is neither an OSError nor a ValueError, so it
escaped the handler that exists to serve the stale copy and killed the probe.
And a slow search walking pages at a 30s socket timeout each outlived the 30s
the bar waits before painting red, so the dot went red over a day whose cached
copy was sitting on disk unread.

Both are the same requirement: a bad fetch degrades to the cached day, never to
a dead probe.

Run: pytest tests/test_slack_cache_fallback.py
"""

import http.client
import importlib.util
import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

DAY = "2026-09-02"
CACHED = [{"t": "09:15:00", "ch": "ui-eng", "im": False, "text": "cached"}]


def truncated(day):
    raise http.client.IncompleteRead(b"half", 200)


class SlackCacheFallback(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (wp.SLACK_DIR, wp._slack_fetch, wp.now_local,
                      wp.slack_token, wp.time.monotonic,
                      wp.urllib.request.urlopen)
        wp.SLACK_DIR = self.dir
        self.path = os.path.join(self.dir, f"{DAY}.json")
        # A finished day is cached forever, so the only way to force a refetch
        # is to claim this IS today and let the TTL decide.
        wp.now_local = lambda: wp.datetime.strptime(DAY, "%Y-%m-%d")

    def tearDown(self):
        (wp.SLACK_DIR, wp._slack_fetch, wp.now_local, wp.slack_token,
         wp.time.monotonic, wp.urllib.request.urlopen) = self.saved

    def _cache(self):
        """A cached day old enough that the fetch is still attempted."""
        with open(self.path, "w") as fh:
            json.dump(CACHED, fh)
        os.utime(self.path, (0, 0))

    def test_incomplete_read_serves_the_cached_day(self):
        self._cache()
        wp._slack_fetch = truncated
        self.assertEqual(wp.slack_for(DAY), CACHED)

    def test_incomplete_read_with_no_cache_still_propagates(self):
        wp._slack_fetch = truncated
        with self.assertRaises(http.client.IncompleteRead):
            wp.slack_for(DAY)

    def test_budget_stops_the_walk_and_the_cached_day_is_served(self):
        """A page that eats the whole budget ends the fetch, not the probe."""
        self._cache()
        calls = []
        base = wp.time.monotonic()

        def slow(req, timeout=None):
            calls.append(timeout)
            # Spend the budget without sleeping through it.
            wp.time.monotonic = lambda: base + wp.SLACK_FETCH_BUDGET_SEC + 1
            raise TimeoutError("socket timed out")

        wp.urllib.request.urlopen = slow
        wp.slack_token = lambda: "xoxc-test"
        self.assertEqual(wp.slack_for(DAY), CACHED)
        # One request, with a timeout drawn from the budget rather than the old
        # flat 30s -- ten of those is what outlived the bar's watchdog.
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0], wp.SLACK_FETCH_BUDGET_SEC)

    def test_budget_is_under_the_bar_watchdog(self):
        self.assertLess(wp.SLACK_FETCH_BUDGET_SEC, 30)


if __name__ == "__main__":
    unittest.main()
