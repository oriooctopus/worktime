#!/usr/bin/env python3
"""The prompt mark replaces a transcript walk, so it has to be exactly as good.

The walk it replaced read every transcript on the machine, which made it
impossible to miss a prompt and impossible to run fast. The mark is one file,
which makes it fast and makes two new things possible to get wrong: the
fingerprint could stop moving when a prompt happens, or a profile could stop
firing the hook and take its prompts out of the count without anything looking
broken. Those are what these tests are for.

Run: pytest tests/test_prompt_mark.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "bin", "worktime-probe.py")
HOOK = os.path.join(ROOT, "bin", "worktime-prompt-mark.py")

HOOK_ENTRY = {
    "hooks": [{"type": "command",
               "command": "python3 ~/.claude/hooks/worktime-prompt-mark.py"}]
}


def load_probe():
    spec = importlib.util.spec_from_file_location("probe_under_test", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProfiles:
    """A tmpdir shaped like the real ~/.claude* layout the probe reads.

    Each profile is a directory holding `projects/` and `settings.json`, which
    is the shape `PROJECT_ROOTS` derives and `profiles_missing_mark_hook`
    walks back up from.
    """

    def __init__(self, tmp, names=("claude", "claude-personal"), hooked=True):
        self.roots = []
        for name in names:
            base = os.path.join(tmp, "." + name)
            os.makedirs(os.path.join(base, "projects"))
            self.write_settings(base, hooked)
            self.roots.append(os.path.join(base, "projects"))

    @staticmethod
    def write_settings(base, hooked):
        body = {"hooks": {"UserPromptSubmit": [HOOK_ENTRY]}} if hooked else {}
        with open(os.path.join(base, "settings.json"), "w") as fh:
            json.dump(body, fh)


class HookWritesTheMark(unittest.TestCase):
    """The writing half: does a prompt actually move the file?"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, HOME=self.tmp.name)
        self.mark = os.path.join(
            self.tmp.name, ".claude", "stats", "worktime", "prompt-mark.json")

    def fire(self, payload):
        r = subprocess.run([sys.executable, HOOK], env=self.env, text=True,
                           input=json.dumps(payload), capture_output=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_prompt_creates_the_mark(self):
        self.fire({"session_id": "abcdef1234", "transcript_path":
                   "/x/projects/-Users-oliver-p/s.jsonl"})
        with open(self.mark) as fh:
            got = json.load(fh)
        self.assertEqual(got["session"], "abcdef12")
        self.assertEqual(got["profile"], "-Users-oliver-p")

    def test_the_mark_is_overwritten_not_appended(self):
        """An append-only log would grow forever for a one-value signal."""
        for i in range(5):
            self.fire({"session_id": f"session{i}"})
        with open(self.mark) as fh:
            body = fh.read()
        json.loads(body)                       # still exactly one object
        self.assertLess(len(body), 400)

    def test_a_broken_payload_never_fails_the_prompt(self):
        """This runs between the human pressing return and Claude answering."""
        r = subprocess.run([sys.executable, HOOK], env=self.env, text=True,
                           input="not json at all", capture_output=True,
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_temp_files_are_left_behind(self):
        """The write is atomic because several profiles share the path."""
        self.fire({"session_id": "abc"})
        state = os.path.dirname(self.mark)
        self.assertEqual(os.listdir(state), ["prompt-mark.json"])


class FingerprintTracksTheMark(unittest.TestCase):
    """The reading half: does the probe notice, and does it stay cheap?"""

    def setUp(self):
        self.probe = load_probe()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profiles = FakeProfiles(self.tmp.name)
        self.probe.PROMPT_ROOTS = self.profiles.roots
        self.probe.PROMPT_MARK = os.path.join(self.tmp.name, "prompt-mark.json")

    def touch(self, body):
        with open(self.probe.PROMPT_MARK, "w") as fh:
            fh.write(body)

    def test_an_absent_mark_is_a_state_not_an_error(self):
        """No prompt since the file was cleared is a real day, not a bug."""
        self.assertIn("absent", self.probe.prompt_mark_stamp())

    def test_a_new_prompt_changes_the_stamp(self):
        """The whole contract: a prompt must invalidate the cached day."""
        self.touch('{"at": "1"}')
        first = self.probe.prompt_mark_stamp()
        self.touch('{"at": "22"}')
        self.assertNotEqual(self.probe.prompt_mark_stamp(), first)

    def test_an_unchanged_mark_holds_the_stamp_still(self):
        """Otherwise every poll recomputes and the cache buys nothing."""
        self.touch('{"at": "1"}')
        self.assertEqual(self.probe.prompt_mark_stamp(),
                         self.probe.prompt_mark_stamp())

    def test_the_stamp_reads_one_file(self):
        """The point of the change, asserted as a count rather than a time.

        A duration would pass on an idle machine while the shipped
        arrangement -- loaded, swapping, metadata cache evicted between polls
        -- still walked thousands of paths and blew the watchdog.
        """
        self.touch('{"at": "1"}')
        seen = []
        real = os.stat
        os.stat = lambda p, *a, **k: (seen.append(p), real(p, *a, **k))[1]
        try:
            self.probe.prompt_mark_stamp()
        finally:
            os.stat = real
        self.assertEqual(seen, [self.probe.PROMPT_MARK])


class UnhookedProfilesAreLoud(unittest.TestCase):
    """The failure the walk could not have, so the one this must answer for."""

    def setUp(self):
        self.probe = load_probe()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.probe.PROMPT_MARK = os.path.join(self.tmp.name, "prompt-mark.json")

    def use(self, **kw):
        self.probe.PROMPT_ROOTS = FakeProfiles(self.tmp.name, **kw).roots

    def test_every_profile_hooked_is_quiet(self):
        self.use()
        self.assertEqual(self.probe.profiles_missing_mark_hook(), [])

    def test_a_profile_without_the_hook_is_named(self):
        self.use(hooked=False)
        missing = self.probe.profiles_missing_mark_hook()
        self.assertEqual(len(missing), 2)
        self.assertTrue(all(m.endswith(("claude", "claude-personal"))
                            for m in missing))

    def test_the_probe_refuses_to_run_rather_than_undercount(self):
        """A silent undercount reads as a person who stopped working."""
        self.use(hooked=False)
        with self.assertRaises(RuntimeError) as caught:
            self.probe.prompt_mark_stamp()
        self.assertIn("worktime-prompt-mark", str(caught.exception))

    def test_one_unhooked_profile_among_several_still_raises(self):
        """The shared mark keeps moving, which is exactly why this is silent."""
        base = os.path.join(self.tmp.name, ".claude-bench")
        os.makedirs(os.path.join(base, "projects"))
        FakeProfiles.write_settings(base, hooked=False)
        self.use()
        self.probe.PROMPT_ROOTS.append(os.path.join(base, "projects"))
        with self.assertRaises(RuntimeError):
            self.probe.prompt_mark_stamp()


class InstalledProfilesOnThisMachine(unittest.TestCase):
    """Pins the real machine, since a missing registration is invisible."""

    def test_every_profile_the_probe_reads_fires_the_hook(self):
        probe = load_probe()
        self.assertEqual(probe.profiles_missing_mark_hook(), [],
                         "register worktime-prompt-mark.py — see README Install")


if __name__ == "__main__":
    unittest.main()
