#!/usr/bin/env python3
"""Tests for hand-logged entries -- the ⌥W stream.

Every other input infers presence from a trace the work left behind. This one
is asserted outright, and the risk that carries is the opposite of the focus
signal's: not that it credits time nobody worked, but that a press produces
nothing visible and the person keeps pressing a key that does not work. So
what is pinned here is that a bare press reaches all three places a real event
reaches -- the event stream that moves the dot, the activity list that explains
it, and the fingerprint that decides whether either gets recomputed.

Run: pytest tests/test_notes.py
"""

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

DAY = "2026-03-04"


class NotesCase(unittest.TestCase):
    """Writes a real notes log to a temp dir and reads it back through the probe.

    Points NOTES at the temp file rather than stubbing note_rows_for(), so the
    JSONL format, the day filter and the ordering are exercised by the same
    tests -- the writer and the reader are the two halves most likely to drift
    apart, and nothing in the menu would show it if they did.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.notes = os.path.join(self.tmp, "notes.jsonl")
        self._real = wp.NOTES
        wp.NOTES = self.notes

    def tearDown(self):
        wp.NOTES = self._real

    def write(self, *recs):
        with open(self.notes, "w") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def rec(self, t, text="typed"):
        return {"day": DAY, "t": t, "text": text}

    def test_absent_log_is_not_an_error(self):
        """A machine that has never had an entry pressed is the normal case."""
        self.assertEqual(wp.note_rows_for(DAY), [])
        self.assertEqual(wp.notes_for(DAY), [])

    def test_only_todays_entries(self):
        """Yesterday's entries must not leak into today's evidence."""
        self.write(self.rec("09:00:00"),
                   {"day": "2026-03-03", "t": "23:59:00", "text": "yesterday"})
        self.assertEqual(wp.notes_for(DAY), ["09:00:00"])

    def test_rows_come_back_in_order(self):
        """Written in press order, but nothing guarantees the file is sorted."""
        self.write(self.rec("14:00:00", "b"), self.rec("09:00:00", "a"))
        self.assertEqual([r["text"] for r in wp.note_rows_for(DAY)], ["a", "b"])

    def test_bare_press_gets_a_label(self):
        """The hotkey sends no text; a blank row would read as a bug."""
        rec = wp.add_note()
        self.assertEqual(rec["text"], wp.GENERIC_NOTE)
        self.assertEqual(wp.add_note("   ")["text"], wp.GENERIC_NOTE)

    def test_typed_text_is_kept_on_one_line(self):
        """`note <text>` from the CLI arrives as separate argv words."""
        self.assertEqual(wp.add_note("  reading   a  PR ")["text"],
                         "reading a PR")

    def test_add_note_is_readable_by_the_reader(self):
        """The round trip, which is what a press actually depends on."""
        rec = wp.add_note("round trip")
        rows = wp.note_rows_for(rec["day"])
        self.assertEqual(rows, [rec])

    def test_entry_is_an_event(self):
        """The dot moves on it: an entry counts exactly as a prompt does."""
        self.write(self.rec("09:17:23"))
        with StubbedStreams(events=True):
            evs = wp.events_for(DAY)
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0].hour, evs[0].minute, evs[0].second),
                         (9, 17, 23))

    def test_entry_is_listed(self):
        """And the list explains the minute the dot is counting."""
        self.write(self.rec("09:17:23", "whiteboarding"))
        with StubbedStreams(rows=True):
            acts = wp.activity_rows(DAY)
        self.assertEqual(acts, [{"t": "09:17", "kind": "note",
                                 "what": "whiteboarding", "n": 1,
                                 "at": acts[0]["at"]}])

    def test_repeated_presses_collapse(self):
        """Two presses a second apart are one thing, carrying its count."""
        self.write(self.rec("09:17:23", wp.GENERIC_NOTE),
                   self.rec("09:17:24", wp.GENERIC_NOTE))
        with StubbedStreams(rows=True):
            acts = wp.activity_rows(DAY)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["n"], 2)

    def test_fingerprint_sees_the_log(self):
        """Without this the menu serves a cached day and the press does
        nothing for as long as the cache holds -- the failure that looks
        exactly like a hotkey that never registered."""
        self.write(self.rec("09:17:23"))
        with StubbedInputs(self.tmp):
            before = wp.activity_fingerprint(DAY)
            self.write(self.rec("09:17:23"), self.rec("10:00:00"))
            after = wp.activity_fingerprint(DAY)
        self.assertNotEqual(before, after)


class StubbedStreams:
    """Silences every stream but notes, so a failure names one thing."""

    NAMES = {
        "events": ["prompts_for", "focus_for", "approvals_for",
                   "github_visits_for"],
        "rows": ["full_day", "slack_for", "approval_rows_for",
                 "github_rows_for", "bridged_idle", "mode_timeline",
                 "focus_app_by_minute", "focus_for"],
    }
    EMPTY = {"full_day": lambda *a: {"sessions": []},
             "focus_app_by_minute": lambda *a: {}}

    def __init__(self, events=False, rows=False):
        self.names = []
        if events:
            self.names += self.NAMES["events"]
        if rows:
            self.names += self.NAMES["rows"]
        self.saved = {}

    def __enter__(self):
        for n in self.names:
            self.saved[n] = getattr(wp, n)
            setattr(wp, n, self.EMPTY.get(n, lambda *a: []))
        return self

    def __exit__(self, *exc):
        for n, v in self.saved.items():
            setattr(wp, n, v)


class StubbedInputs:
    """Points every other fingerprint input at an empty temp dir, so the only
    thing that can move the signature is the notes log."""

    PATHS = ["MARKS", "APPROVALS", "MODEFILE", "CAL_FILE", "IDLE_CLAIMS",
             "SLACK_DIR", "FOCUS_DIR", "ACTIVITY_DIR"]

    def __init__(self, tmp):
        self.tmp = tmp
        self.saved = {}

    def __enter__(self):
        for n in self.PATHS + ["PROMPT_ROOTS", "chrome_history_path"]:
            self.saved[n] = getattr(wp, n)
        for n in self.PATHS:
            setattr(wp, n, os.path.join(self.tmp, "absent"))
        wp.SLACK_DIR = wp.FOCUS_DIR = wp.ACTIVITY_DIR = self.tmp
        wp.PROMPT_ROOTS = []
        wp.chrome_history_path = lambda: None
        return self

    def __exit__(self, *exc):
        for n, v in self.saved.items():
            setattr(wp, n, v)


if __name__ == "__main__":
    unittest.main()
