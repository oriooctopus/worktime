#!/usr/bin/env python3
"""Tests for the live Slack send signal -- the desktop client's console log.

search.messages is cached for SLACK_TTL_SEC because a network round trip
cannot sit on a five-second poll, so a send can be four minutes old before the
API path can see it. The client writes a line locally the instant the message
leaves the compose box, which is the same trade github_live_rows() makes
against Chrome's history: local for the live verdict, remote for the record.

The log is undocumented and unversioned, so the thing most likely to break is
the parse -- silently, and in the direction that just looks like a quiet
afternoon. These pin the format, the tail-follow, and the rotation reset.

Run: pytest tests/test_slack_live.py
"""

import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

DAY = "2026-09-02"

# Verbatim from ~/Library/Application Support/Slack/logs/default/, including
# the surrounding queue chatter -- ENQUEUED and ACTIVE and RESOLVED all name
# chat.postMessage and only the first carries the send reason, so a looser
# match would count one message four times.
SEND = ("[09/02/26, {t}:833] info: [API-Q] (T038H14JA) d41f3295-1788367043.833"
        " chat.postMessage called with reason: webapp_message_send \n")
ACTIVE = ("[09/02/26, {t}:839] info: [API-Q] (T038H14JA)"
          " d41f3295-1788367043.833 chat.postMessage is ACTIVE \n")
NOISE = ("[09/02/26, {t}:230] info: [RTM-ACTIVITY] (T038H14JA)"
         " RTM-ACTIVITY activity_updated \n")


class SlackLogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "webapp-console.log")
        self.orig = wp.SLACK_LOG
        wp.SLACK_LOG = self.path
        wp._slack_log_state.clear()

    def tearDown(self):
        wp.SLACK_LOG = self.orig
        wp._slack_log_state.clear()
        self.tmp.cleanup()

    def write(self, text, mode="a"):
        with open(self.path, mode) as fh:
            fh.write(text)

    def at(self, day=DAY):
        got = wp.last_slack_send(day)
        return got.strftime("%H:%M:%S") if got else None


class TestParse(SlackLogCase):
    def test_reads_the_send_line(self):
        self.write(NOISE.format(t="12:37:20") + SEND.format(t="12:37:23"))
        self.assertEqual(self.at(), "12:37:23")

    def test_the_queue_chatter_around_a_send_is_not_a_second_send(self):
        # Only the "called with reason: webapp_message_send" line is the act.
        # ACTIVE and RESOLVED repeat the endpoint milliseconds later, and
        # matching on chat.postMessage alone would read one message as several.
        self.write(SEND.format(t="12:37:23") + ACTIVE.format(t="12:37:23"))
        self.write(ACTIVE.format(t="12:39:00"))
        self.assertEqual(self.at(), "12:37:23")

    def test_reports_the_newest_send(self):
        self.write(SEND.format(t="09:02:00") + NOISE.format(t="09:30:00")
                   + SEND.format(t="12:37:23"))
        self.assertEqual(self.at(), "12:37:23")

    def test_yesterdays_sends_are_not_todays_evidence(self):
        # The log is not rotated daily, so the date on the line is the only
        # thing separating this morning from last night.
        self.write("[09/01/26, 23:50:00:833] info: [API-Q] (T038H14JA) x"
                   " chat.postMessage called with reason: webapp_message_send\n")
        self.assertIsNone(self.at())

    def test_a_missing_log_reports_nothing_rather_than_failing(self):
        # Slack not installed, or never launched since the last cleanup. The
        # API path is unaffected, so this is quiet rather than an error.
        wp.SLACK_LOG = os.path.join(self.tmp.name, "absent.log")
        self.assertIsNone(self.at())

    def test_a_log_with_no_sends_reports_nothing(self):
        self.write(NOISE.format(t="09:00:00") * 5)
        self.assertIsNone(self.at())


class TestTailFollow(SlackLogCase):
    """The log grows continuously -- RTM events land every few seconds whether
    or not anybody is typing -- so a poll must cost the new bytes, not the file.
    """

    def test_only_the_bytes_since_the_last_read_are_scanned(self):
        self.write(SEND.format(t="09:02:00"))
        self.assertEqual(self.at(), "09:02:00")
        offset = wp._slack_log_state["offset"]
        self.assertEqual(offset, os.stat(self.path).st_size)

        self.write(SEND.format(t="12:37:23"))
        self.assertEqual(self.at(), "12:37:23")
        self.assertGreater(wp._slack_log_state["offset"], offset)

    def test_an_unchanged_log_is_not_re_read(self):
        self.write(SEND.format(t="09:02:00"))
        self.assertEqual(self.at(), "09:02:00")
        # Deleting the file would break a re-read; the cached answer must come
        # back without touching it.
        size = os.stat(self.path).st_size
        with open(self.path, "w") as fh:
            fh.write("x" * size)
        self.assertEqual(self.at(), "09:02:00")

    def test_rotation_restarts_the_read(self):
        # Slack rolls webapp-console.log into webapp-console1.log and starts a
        # fresh one. The offset then points past the end of a smaller file, and
        # seeking there would make every later send invisible for the rest of
        # the day.
        self.write(SEND.format(t="09:02:00") + NOISE.format(t="09:10:00") * 20)
        self.assertEqual(self.at(), "09:02:00")
        self.write(SEND.format(t="12:37:23"), mode="w")
        self.assertEqual(self.at(), "12:37:23")

    def test_a_new_day_clears_the_answer_carried_from_the_old_one(self):
        self.write(SEND.format(t="12:37:23"))
        self.assertEqual(self.at(), "12:37:23")
        self.assertIsNone(self.at(day="2026-09-03"))


if __name__ == "__main__":
    unittest.main()
