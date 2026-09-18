#!/usr/bin/env python3
"""write_vault_snapshot must not touch the vault files when nothing but the
clock changed.

WorktimeBar polls the probe every 60s, and for most of those polls nothing
about the day actually changed -- only `updated` (the JSON's own field) and
`generated:` (the markdown's frontmatter stamp) differ, because both are
this run's wall-clock time rather than a fact derived from events. Before
this file existed, write_vault_snapshot rewrote both files on every poll
regardless, which pushed 1440 JSON versions/day against Obsidian Sync's 1 GB
quota and forced the markdown sidecar's Dataview block to re-render on every
open dashboard once a minute (the visible "page jumps every 60s" bug).

The markdown side has an extra wrinkle: Obsidian's update-time-on-edit
plugin adds `created:`/`updated:` frontmatter next to the exporter's own
`generated:` line and rewrites them within seconds of any write, including a
no-op one. A byte comparison would see the plugin's own edit as a content
change and defeat the skip on the very next run -- see the real file this
was diagnosed against, ~/obsidian-vault/Dashboard/worktime/2026-09-17.md,
which carries all three keys though render_markdown_snapshot only ever
emits `generated:`.

Run: pytest tests/test_snapshot_idle_write.py
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

DAY = "2026-09-03"  # a fixed past day: elapsed_s(DAY) is a constant 24*3600
# regardless of wall-clock time, so nothing in the derivation depends on
# `now` except the `updated` field and the `generated:` line themselves --
# what makes "identical content, different run time" reproducible here
# without mocking every helper write_vault_snapshot calls.


class SkipsTheNoOpWrite(unittest.TestCase):
    """Two runs, same inputs, different wall clock -- one write, not two."""

    NAMES = ("snapshot_path", "markdown_snapshot_path", "now_local",
             "marks_for")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = {n: getattr(wp, n) for n in self.NAMES}
        wp.snapshot_path = lambda day: os.path.join(self.tmp, f"{day}.json")
        wp.markdown_snapshot_path = (
            lambda day: os.path.join(self.tmp, f"{day}.md"))
        # No marks file exists in the temp dir, but marks_for() also reads
        # now_local() for its own bookkeeping (open_mark_end); pinning it
        # directly keeps the two runs from differing only because the marks
        # path is briefly a different one on disk.
        wp.marks_for = lambda day, stamps=None: []

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(wp, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def jpath(self):
        return wp.snapshot_path(DAY)

    def mpath(self):
        return wp.markdown_snapshot_path(DAY)

    def age_files(self):
        """Back-date both files' mtimes so an untouched-mtime assertion
        actually proves os.replace never ran, rather than merely landing
        inside the same clock tick a real rewrite would also produce."""
        past = os.path.getmtime(self.jpath()) - 10_000
        os.utime(self.jpath(), (past, past))
        os.utime(self.mpath(), (past, past))
        return past

    def tmp_files(self):
        return [f for f in os.listdir(self.tmp) if f.endswith(".tmp")]

    def test_identical_content_leaves_both_files_untouched(self):
        wp.now_local = lambda: datetime(2026, 9, 18, 10, 0, 0)
        wp.write_vault_snapshot(DAY, [], "fp1")
        j1, m1 = open(self.jpath()).read(), open(self.mpath()).read()
        past = self.age_files()

        # A later run, one minute on -- the shape of a real poll -- with the
        # same events and fingerprint, so nothing about the day changed.
        wp.now_local = lambda: datetime(2026, 9, 18, 10, 1, 0)
        wp.write_vault_snapshot(DAY, [], "fp1")

        self.assertEqual(open(self.jpath()).read(), j1,
                          "JSON bytes changed on a run with no real change")
        self.assertEqual(open(self.mpath()).read(), m1,
                          "markdown bytes changed on a run with no real change")
        self.assertEqual(os.path.getmtime(self.jpath()), past,
                          "JSON was rewritten (mtime moved) though nothing changed")
        self.assertEqual(os.path.getmtime(self.mpath()), past,
                          "markdown was rewritten (mtime moved) though nothing changed")
        self.assertEqual(self.tmp_files(), [],
                          "a .tmp file was left behind by a skipped write")

    def test_updated_field_is_the_only_json_difference_ignored(self):
        # Same scenario as above, read back as JSON: `updated` is exactly the
        # field this feature is allowed to treat as noise, so prove that's
        # what actually differed rather than the skip accidentally also
        # covering a real field.
        wp.now_local = lambda: datetime(2026, 9, 18, 10, 0, 0)
        wp.write_vault_snapshot(DAY, [], "fp1")
        snap1 = json.load(open(self.jpath()))
        self.age_files()

        wp.now_local = lambda: datetime(2026, 9, 18, 10, 1, 0)
        wp.write_vault_snapshot(DAY, [], "fp1")
        snap2 = json.load(open(self.jpath()))
        # The write was skipped, so what's on disk is still run 1's -- this
        # assertion is really "the file wasn't touched", restated in terms
        # a reader of the JSON would recognise.
        self.assertEqual(snap1["updated"], "10:00")
        self.assertEqual(snap2["updated"], "10:00")

    def test_plugin_added_frontmatter_does_not_defeat_the_skip(self):
        # Simulates Obsidian's update-time-on-edit plugin: it rewrites the
        # file on disk within seconds of the probe's own write, inserting
        # `created:`/`updated:` next to `generated:` -- lines
        # render_markdown_snapshot itself never emits. See the real file
        # this was diagnosed against for the shape being reproduced here:
        # ~/obsidian-vault/Dashboard/worktime/2026-09-17.md.
        wp.now_local = lambda: datetime(2026, 9, 18, 10, 0, 0)
        wp.write_vault_snapshot(DAY, [], "fp1")
        lines = open(self.mpath()).read().split("\n")
        idx = next(i for i, l in enumerate(lines) if l.startswith("generated:"))
        lines[idx + 1:idx + 1] = [
            "created: 2026-09-18T10:05", "updated: 2026-09-18T10:07"]
        plugin_text = "\n".join(lines)
        open(self.mpath(), "w").write(plugin_text)
        past = os.path.getmtime(self.jpath()) - 10_000
        os.utime(self.mpath(), (past, past))

        # A later probe run with nothing new to report.
        wp.now_local = lambda: datetime(2026, 9, 18, 10, 10, 0)
        wp.write_vault_snapshot(DAY, [], "fp1")

        self.assertEqual(open(self.mpath()).read(), plugin_text,
                          "the probe overwrote the plugin's created/updated "
                          "lines though the actual content did not change")
        self.assertEqual(os.path.getmtime(self.mpath()), past)
        self.assertEqual(self.tmp_files(), [])

    def test_a_real_change_still_writes(self):
        # `fp` is not a clock field -- it signs the actual inputs the
        # periods were derived from (see write_vault_snapshot's docstring),
        # so a different one is a real change and must not be swallowed by
        # the same comparison that skips a clock-only run.
        wp.now_local = lambda: datetime(2026, 9, 18, 10, 0, 0)
        wp.write_vault_snapshot(DAY, [], "fp1")
        j1 = open(self.jpath()).read()
        past = self.age_files()

        wp.now_local = lambda: datetime(2026, 9, 18, 10, 1, 0)
        wp.write_vault_snapshot(DAY, [], "fp2")

        j2 = json.load(open(self.jpath()))
        self.assertNotEqual(open(self.jpath()).read(), j1)
        self.assertEqual(j2["fp"], "fp2")
        self.assertGreater(os.path.getmtime(self.jpath()), past)
        self.assertEqual(self.tmp_files(), [])

    def test_json_and_markdown_skip_independently(self):
        # `fp` never appears in the rendered markdown (see
        # render_markdown_snapshot's docstring: it deliberately omits
        # everything but times and counts), so a change that is real for the
        # JSON is invisible to the markdown, and the markdown write must
        # still be skipped on its own terms.
        wp.now_local = lambda: datetime(2026, 9, 18, 10, 0, 0)
        wp.write_vault_snapshot(DAY, [], "fp1")
        m1 = open(self.mpath()).read()
        self.age_files()
        past_md = os.path.getmtime(self.mpath())

        wp.now_local = lambda: datetime(2026, 9, 18, 10, 1, 0)
        wp.write_vault_snapshot(DAY, [], "fp2")

        self.assertEqual(json.load(open(self.jpath()))["fp"], "fp2",
                          "the JSON should have picked up the real fp change")
        self.assertEqual(open(self.mpath()).read(), m1,
                          "the markdown was rewritten for a change it "
                          "never renders")
        self.assertEqual(os.path.getmtime(self.mpath()), past_md)

    def test_missing_file_writes(self):
        wp.now_local = lambda: datetime(2026, 9, 18, 10, 0, 0)
        self.assertFalse(os.path.exists(self.jpath()))
        self.assertFalse(os.path.exists(self.mpath()))
        wp.write_vault_snapshot(DAY, [], "fp1")
        self.assertTrue(os.path.exists(self.jpath()))
        self.assertTrue(os.path.exists(self.mpath()))
        self.assertEqual(self.tmp_files(), [])


if __name__ == "__main__":
    unittest.main()
