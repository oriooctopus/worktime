#!/usr/bin/env python3
"""The probe must find its sibling scripts when reached through a symlink.

Nothing in the repo runs the probe by its real path. launchd, the menu bar app
and the `dot` command all go through ~/.claude/bin/worktime-probe.py, and every
other test imports the module directly -- so a path that resolves correctly
in-repo and wrongly through the symlink passes the whole suite and then fails on
every poll in the one arrangement that ships.

Run: pytest tests/test_probe_paths.py
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "bin", "worktime-probe.py")

# Load the module from whatever path argv[1] names and print the sibling
# scripts it decided on. Run as a subprocess so it sees the symlink as its
# __file__, which an in-process import of the real path never would.
REPORT = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("probe_under_link", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(mod.ROOT)
print(mod.PROMPT_COUNT)
"""


class ProbePathsThroughSymlink(unittest.TestCase):
    def resolved_through_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The real install shape: a bin/ directory of symlinks somewhere
            # else entirely, pointing back into the checkout.
            link_dir = os.path.join(tmp, "bin")
            os.mkdir(link_dir)
            link = os.path.join(link_dir, "worktime-probe.py")
            os.symlink(PROBE, link)
            out = subprocess.run([sys.executable, "-c", REPORT, link],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0,
                             f"probe would not import through a symlink:\n{out.stderr}")
            root, prompt_count = out.stdout.strip().splitlines()[:2]
            return tmp, root, prompt_count

    def test_root_is_the_checkout_not_the_symlink_directory(self):
        tmp, root, _ = self.resolved_through_link()
        self.assertEqual(os.path.realpath(root), os.path.realpath(ROOT))
        self.assertFalse(root.startswith(tmp),
                         f"ROOT followed the symlink's own directory: {root}")

    def test_sibling_scripts_are_found(self):
        _, _, prompt_count = self.resolved_through_link()
        # The one that actually broke: the probe shells out to this on every
        # poll, so a wrong path here is a dead dot rather than a degraded one.
        self.assertTrue(os.path.exists(prompt_count),
                        f"probe would look for prompt-count.py at {prompt_count}")


if __name__ == "__main__":
    unittest.main()
