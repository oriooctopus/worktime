#!/usr/bin/env python3
"""Tests for the probe-side readers of the chunked activity export
(bin/worktime-probe.py's activity_day_lines / github_export_rows /
desktop_prompts_for / activity_fingerprint).

commits b44cddb/66047fa/bb5583b replaced <activity>/<day>.md with
<activity>/<day>/HH.md + periods.md + _status.md. These four functions are
the probe's only readers of that new layout, and an audit found none of
them had coverage against the actual chunk directory shape -- each test
here is built to FAIL under a named mutation, not just to pass against the
current code (see the class docstrings for which mutation each guards).

Run: pytest tests/test_worktime_probe_chunks.py -v
"""

import importlib.util
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)


def write_chunk(day_dir, hour, lines):
    os.makedirs(day_dir, exist_ok=True)
    with open(os.path.join(day_dir, f"{hour}.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


class ActivityDayLines(unittest.TestCase):
    """activity_day_lines: hour-order concatenation + conflict-copy filter.

    Guards:
      P3 -- HOUR_CHUNK_RE loosened to also accept "10 (conflict).md"
            (Obsidian Sync's collision file, which must never be read as a
            second copy of hour 10's rows).
      P4 -- hours read in reversed (or otherwise non-numeric) order.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = wp.ACTIVITY_DIR
        wp.ACTIVITY_DIR = self.tmp
        self.day = "2026-09-09"
        self.day_dir = os.path.join(self.tmp, self.day)

    def tearDown(self):
        wp.ACTIVITY_DIR = self.saved
        shutil.rmtree(self.tmp)

    def test_hours_come_back_in_ascending_order_regardless_of_write_order(self):
        # Written out of order on purpose -- if the reader ever iterated
        # os.listdir()'s own (arbitrary) order instead of sorting, this
        # would catch it just as well as a reversed-sort mutation would.
        write_chunk(self.day_dir, "23", ["from 23"])
        write_chunk(self.day_dir, "02", ["from 02"])
        write_chunk(self.day_dir, "09", ["from 09"])
        lines = wp.activity_day_lines(self.day)
        content = "".join(lines)
        self.assertLess(content.index("from 02"), content.index("from 09"))
        self.assertLess(content.index("from 09"), content.index("from 23"))

    def test_conflict_copy_is_never_read_as_a_second_hour_10(self):
        write_chunk(self.day_dir, "10", ["real hour 10 row"])
        # Obsidian Sync's collision filename -- must not match HOUR_CHUNK_RE.
        with open(os.path.join(self.day_dir, "10 (conflict).md"), "w") as fh:
            fh.write("conflict copy row -- must never be read\n")
        lines = wp.activity_day_lines(self.day)
        content = "".join(lines)
        self.assertIn("real hour 10 row", content)
        self.assertNotIn("conflict copy row", content)
        # Exactly one copy of hour 10's content, not two.
        self.assertEqual(content.count("real hour 10 row"), 1)

    def test_missing_day_directory_reads_as_no_lines(self):
        self.assertEqual(wp.activity_day_lines(self.day), [])

    def test_empty_day_directory_reads_as_no_lines(self):
        os.makedirs(self.day_dir)
        self.assertEqual(wp.activity_day_lines(self.day), [])


class GithubExportRowsReadsChunks(unittest.TestCase):
    """github_export_rows must actually read rows out of the hour chunks,
    not just parse the CHROME_ROW regex against nothing (a stub that always
    returned [] would pass every test that never calls it against real
    chunk content)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = wp.ACTIVITY_DIR
        wp.ACTIVITY_DIR = self.tmp
        self.day = "2026-09-09"
        self.day_dir = os.path.join(self.tmp, self.day)

    def tearDown(self):
        wp.ACTIVITY_DIR = self.saved
        shutil.rmtree(self.tmp)

    def test_a_github_visit_in_an_hour_chunk_is_returned(self):
        write_chunk(self.day_dir, "14", [
            "| 14:05 | chrome | visit | Windows | Some PR — https://github.com/x/y/pull/1 |",
        ])
        rows = wp.github_export_rows(self.day)
        self.assertEqual(len(rows), 1)
        when, detail = rows[0]
        self.assertEqual((when.hour, when.minute), (14, 5))
        self.assertIn("github.com", detail)

    def test_a_non_work_visit_is_not_returned(self):
        write_chunk(self.day_dir, "14", [
            "| 14:05 | chrome | visit | Windows | Shopping — https://amazon.com/x |",
        ])
        self.assertEqual(wp.github_export_rows(self.day), [])

    def test_rows_across_two_chunks_come_back_sorted_by_time(self):
        write_chunk(self.day_dir, "15", [
            "| 15:00 | chrome | visit | Windows | later — https://github.com/a |",
        ])
        write_chunk(self.day_dir, "09", [
            "| 09:00 | chrome | visit | Windows | earlier — https://github.com/b |",
        ])
        rows = wp.github_export_rows(self.day)
        self.assertEqual([w.hour for w, _ in rows], [9, 15])


class DesktopPromptsForReadsChunks(unittest.TestCase):
    """desktop_prompts_for must read real 'claude | prompt' rows out of the
    chunk files, not just exercise the regex in isolation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = wp.ACTIVITY_DIR
        wp.ACTIVITY_DIR = self.tmp
        self.day = "2026-09-09"
        self.day_dir = os.path.join(self.tmp, self.day)

    def tearDown(self):
        wp.ACTIVITY_DIR = self.saved
        shutil.rmtree(self.tmp)

    def test_a_desktop_prompt_row_is_returned(self):
        write_chunk(self.day_dir, "11", [
            "| 11:22 | claude | prompt | Desktop | wrote some code |",
        ])
        out = wp.desktop_prompts_for(self.day)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].hour, out[0].minute), (11, 22))

    def test_no_chunks_means_no_prompts(self):
        self.assertEqual(wp.desktop_prompts_for(self.day), [])


class ActivityFingerprintSeesChunkChanges(unittest.TestCase):
    """activity_fingerprint must change when a chunk file in the day
    directory is added or its content changes (P6: fingerprint drops the
    per-chunk stat() loop and stops noticing chunk writes -- the failure
    mode is a stale-cache dashboard that never recomputes)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.day = "2026-09-09"
        self.day_dir = os.path.join(self.tmp, self.day)
        os.makedirs(self.day_dir)
        # Point every OTHER fingerprint input at a path that can never exist,
        # and disable the prompt-mark hook-registration check (empty
        # PROMPT_ROOTS -> profiles_missing_mark_hook() returns [] -> no
        # RuntimeError) -- see tests/test_notes.py's StubbedInputs for the
        # same pattern. This isolates the assertion to "did the chunk
        # directory listing move the hash", not any of the other inputs.
        self.saved = {}
        for name in ("MARKS", "APPROVALS", "MODEFILE", "CAL_FILE",
                     "IDLE_CLAIMS", "SLACK_DIR", "FOCUS_DIR", "ACTIVITY_DIR",
                     "PROMPT_ROOTS", "chrome_history_path"):
            self.saved[name] = getattr(wp, name)
        absent = os.path.join(self.tmp, "absent")
        for name in ("MARKS", "APPROVALS", "MODEFILE", "CAL_FILE", "IDLE_CLAIMS"):
            setattr(wp, name, absent)
        wp.SLACK_DIR = wp.FOCUS_DIR = self.tmp
        wp.ACTIVITY_DIR = self.tmp
        wp.PROMPT_ROOTS = []
        wp.chrome_history_path = lambda: None

    def tearDown(self):
        for name, v in self.saved.items():
            setattr(wp, name, v)
        shutil.rmtree(self.tmp)

    def test_adding_an_hour_chunk_changes_the_fingerprint(self):
        before = wp.activity_fingerprint(self.day)
        write_chunk(self.day_dir, "09", ["| 09:00 | chrome | visit | W | x |"])
        after = wp.activity_fingerprint(self.day)
        self.assertNotEqual(before, after)

    def test_rewriting_an_existing_chunk_changes_the_fingerprint(self):
        write_chunk(self.day_dir, "09", ["| 09:00 | chrome | visit | W | x |"])
        before = wp.activity_fingerprint(self.day)
        write_chunk(self.day_dir, "09", ["| 09:00 | chrome | visit | W | x |",
                                          "| 09:05 | chrome | visit | W | y |"])
        after = wp.activity_fingerprint(self.day)
        self.assertNotEqual(before, after)

    def test_untouched_directory_gives_a_stable_fingerprint(self):
        write_chunk(self.day_dir, "09", ["| 09:00 | chrome | visit | W | x |"])
        first = wp.activity_fingerprint(self.day)
        second = wp.activity_fingerprint(self.day)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
