#!/usr/bin/env python3
"""The prompt index tells the counter where to look instead of it searching.

`count_for_day` used to find a day's sessions by walking every transcript on
the machine -- 2,271 metadata operations to locate the fourteen files that
actually mattered. Warm that is milliseconds. With the machine swapping, the
filesystem metadata cache is evicted between polls and the same walk runs for
tens of seconds in uninterruptible disk wait, which is what put the probe past
the menu bar's 30s watchdog.

The index is written by the prompt hook, which already knows the transcript
path. What it deliberately does NOT do is record the prompts themselves: the
counter's filters (entrypoint, isMeta, isSidechain, the running session title,
the fork/rewind dedup) decide what counts as a human at a keyboard, and the
UserPromptSubmit payload carries no `entrypoint` to reproduce them from. So
the index is a set of paths, every one of those decisions stays in
prompts_with_time, and the two sources have to be interchangeable -- which is
what most of this file checks.

Run: pytest tests/test_prompt_index.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNTER = os.path.join(ROOT, "bin", "prompt-count.py")
HOOK = os.path.join(ROOT, "bin", "worktime-prompt-mark.py")

DAY = "2026-03-05"
YESTERDAY = "2026-03-04"
TOMORROW = "2026-03-06"


def load_counter():
    spec = importlib.util.spec_from_file_location("counter_under_test", COUNTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class IndexBase(unittest.TestCase):
    def setUp(self):
        self.pc = load_counter()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pc.INDEX_DIR = os.path.join(self.tmp.name, "prompt-index")
        self.pc.SINCE = os.path.join(self.pc.INDEX_DIR, "since")
        os.makedirs(self.pc.INDEX_DIR)
        self.roots = os.path.join(self.tmp.name, "projects")
        self.pc.PROJECT_ROOTS = [self.roots]

    def since(self, day):
        with open(self.pc.SINCE, "w") as fh:
            fh.write(day)

    def transcript(self, name, prompts, project="-a-project"):
        """A transcript holding `prompts` as (HH:MM, text) on DAY, in UTC."""
        d = os.path.join(self.roots, project)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            for hh_mm, text in prompts:
                # The counter converts UTC to local; DAY at 12:00Z is safely
                # mid-day in any zone this runs in.
                fh.write(json.dumps({
                    "type": "user", "entrypoint": "cli",
                    "message": {"role": "user", "content": text},
                    "timestamp": f"{DAY}T{hh_mm}:00.000Z",
                }) + "\n")
        return path

    def index(self, day, paths):
        with open(os.path.join(self.pc.INDEX_DIR, f"{day}.jsonl"), "w") as fh:
            for p in paths:
                fh.write(json.dumps({"transcript": p, "session": "s"}) + "\n")

    def cutoff(self, day=DAY):
        return datetime.strptime(day, "%Y-%m-%d").replace(
            tzinfo=self.pc.LOCAL).timestamp()


class WhichSourceAnswersTheDay(IndexBase):
    """The choice is made on the date, never on whether a read succeeded."""

    def test_a_day_after_the_index_started_uses_the_index(self):
        self.since(YESTERDAY)
        self.transcript("walked.jsonl", [("12:00", "hi")])
        self.index(DAY, ["/only/from/the/index.jsonl"])
        # The indexed path does not exist, so an empty result proves the walk
        # never ran -- the walk would have found walked.jsonl.
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [])

    def test_the_day_the_index_started_is_still_walked(self):
        """Its earlier prompts happened before the hook existed."""
        self.since(DAY)
        path = self.transcript("walked.jsonl", [("12:00", "hi")])
        self.index(DAY, [])
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [path])

    def test_a_day_before_the_index_is_walked(self):
        self.since(TOMORROW)
        path = self.transcript("walked.jsonl", [("12:00", "hi")])
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [path])

    def test_no_index_at_all_walks(self):
        """Every day predates the index until the hook has run once."""
        os.remove(self.pc.SINCE) if os.path.exists(self.pc.SINCE) else None
        path = self.transcript("walked.jsonl", [("12:00", "hi")])
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [path])

    def test_a_covered_day_with_no_index_file_has_no_prompts(self):
        """The hook creates a day's index on its first prompt, so until then
        the day is empty -- every morning before the first prompt. Raising
        here turned the dot red each midnight; walking would reintroduce the
        cost the index removes, and would find transcripts that are not
        today's anyway."""
        self.since(YESTERDAY)
        self.transcript("walked.jsonl", [("12:00", "hi")])
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [])


class IndexContents(IndexBase):
    def setUp(self):
        super().setUp()
        self.since(YESTERDAY)

    def test_repeated_lines_yield_one_path(self):
        """One line per prompt, so a busy session appears dozens of times."""
        p = self.transcript("s.jsonl", [("12:00", "hi")])
        self.index(DAY, [p, p, p, p])
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [p])

    def test_order_follows_the_index(self):
        a = self.transcript("a.jsonl", [("12:00", "hi")])
        b = self.transcript("b.jsonl", [("12:00", "hi")])
        self.index(DAY, [b, a])
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [b, a])

    def test_a_deleted_transcript_is_dropped(self):
        """The walk would not have found it either -- it is simply gone."""
        p = self.transcript("gone.jsonl", [("12:00", "hi")])
        self.index(DAY, [p])
        os.remove(p)
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [])

    def test_a_transcript_moved_into_a_worktree_is_found_by_session(self):
        """Entering a worktree moves the transcript to a new project folder.

        The prompts are all still in it, so dropping it would erase a whole
        background job from the day until its next prompt was recorded.
        """
        old = os.path.join(self.roots, "-repo", "abc.jsonl")
        new = self.transcript("abc.jsonl", [("12:00", "hi")],
                              project="-repo-worktrees-x")
        with open(os.path.join(self.pc.INDEX_DIR, f"{DAY}.jsonl"), "w") as fh:
            fh.write(json.dumps({"transcript": old, "session": "abc"}) + "\n")
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [new])

    def test_a_blank_line_is_not_a_transcript(self):
        p = self.transcript("s.jsonl", [("12:00", "hi")])
        with open(os.path.join(self.pc.INDEX_DIR, f"{DAY}.jsonl"), "w") as fh:
            fh.write(json.dumps({"transcript": p}) + "\n\n\n")
        self.assertEqual(self.pc.transcripts_for(DAY, self.cutoff()), [p])


class TheTwoSourcesAgree(IndexBase):
    """The property that makes the index safe: same day, same answer."""

    def test_identical_output_from_index_and_walk(self):
        a = self.transcript("a.jsonl", [("12:00", "morning"), ("13:00", "noon")])
        b = self.transcript("b.jsonl", [("14:00", "later")], project="-b-project")

        self.since(TOMORROW)                       # forces the walk
        walked = self.pc.count_for_day(DAY)

        self.since(YESTERDAY)                      # forces the index
        self.index(DAY, [b, a])                    # deliberately reversed
        indexed = self.pc.count_for_day(DAY)

        self.assertEqual(walked["total"], 3)
        self.assertEqual(walked, indexed)

    def test_sessions_come_back_in_the_order_they_happened(self):
        """Not in whatever order the filesystem or the index handed them over."""
        late = self.transcript("late.jsonl", [("15:00", "last")])
        early = self.transcript("early.jsonl", [("09:00", "first")],
                                project="-b-project")
        self.since(YESTERDAY)
        self.index(DAY, [late, early])
        got = [s["first"] for s in self.pc.count_for_day(DAY)["sessions"]]
        self.assertEqual(got, sorted(got))

    def test_the_index_reads_one_file_per_session_and_nothing_else(self):
        """The point of the change, counted rather than timed.

        A duration would pass on an idle machine while the shipped
        arrangement -- swapping, metadata cache cold between polls -- still
        walked thousands of paths and blew the watchdog.
        """
        paths = [self.transcript(f"s{i}.jsonl", [("12:00", "hi")])
                 for i in range(3)]
        for i in range(40):                        # noise the walk would visit
            self.transcript(f"noise{i}.jsonl", [("12:00", "x")], "-noisy")
        self.since(YESTERDAY)
        self.index(DAY, paths)

        seen = []
        real = os.stat
        os.stat = lambda p, *a, **k: (seen.append(p), real(p, *a, **k))[1]
        try:
            self.pc.transcripts_for(DAY, self.cutoff())
        finally:
            os.stat = real
        self.assertEqual(len(seen), 3)


class HookWritesTheIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, HOME=self.tmp.name)
        self.dir = os.path.join(self.tmp.name, ".claude", "stats", "worktime",
                                "prompt-index")

    def fire(self, **payload):
        r = subprocess.run([sys.executable, HOOK], env=self.env, text=True,
                           input=json.dumps(payload), capture_output=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)

    def today(self):
        wc = importlib.util.spec_from_file_location(
            "wc", os.path.join(ROOT, "bin", "worktime_common.py"))
        m = importlib.util.module_from_spec(wc)
        wc.loader.exec_module(m)
        return datetime.now(m.local_tz()).strftime("%Y-%m-%d")

    def test_a_prompt_records_its_transcript(self):
        self.fire(session_id="abc", transcript_path="/p/projects/-x/s.jsonl")
        with open(os.path.join(self.dir, f"{self.today()}.jsonl")) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual(rows[0]["transcript"], "/p/projects/-x/s.jsonl")

    def test_the_index_is_appended_so_a_day_keeps_every_session(self):
        for i in range(3):
            self.fire(session_id=f"s{i}", transcript_path=f"/p/s{i}.jsonl")
        with open(os.path.join(self.dir, f"{self.today()}.jsonl")) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual([r["transcript"] for r in rows],
                         ["/p/s0.jsonl", "/p/s1.jsonl", "/p/s2.jsonl"])

    def test_since_is_stamped_once_and_never_moves(self):
        """It marks where the index became trustworthy; rewriting it daily
        would keep declaring every day uncovered."""
        self.fire(session_id="a", transcript_path="/p/a.jsonl")
        first = open(os.path.join(self.dir, "since")).read()
        self.fire(session_id="b", transcript_path="/p/b.jsonl")
        self.assertEqual(open(os.path.join(self.dir, "since")).read(), first)
        self.assertEqual(first, self.today())

    def test_a_prompt_with_no_transcript_path_indexes_nothing(self):
        """Nothing to record where to look, so there is nothing to record."""
        self.fire(session_id="abc")
        self.assertFalse(os.path.exists(self.dir))


if __name__ == "__main__":
    unittest.main()
