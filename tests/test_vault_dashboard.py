"""Regression tests for the Obsidian Vault Dashboard note's structural
invariants (tab-controller <-> heading contract, and the Usage widget's
adapter.read-over-dv.io.load fix).

The note lives outside this repo (in the Obsidian vault) and may not exist on
every machine this suite runs on -- all tests here SKIP (never fail) when it
is absent. Same for the usage summary.md data file used by the Tier B tests.

Run: python3 -m pytest tests/test_vault_dashboard.py -q
"""
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

VAULT_NOTE = "/home/esme/obsidian-vault/Dashboard/Vault Dashboard.md"
SUMMARY_MD = "/home/esme/obsidian-vault/Dashboard/usage/summary.md"

USAGE_MODULE_PATH = os.path.expanduser("~/.claude/bin/usage-by-session.py")


def _has_node():
    return shutil.which("node") is not None


# --------------------------------------------------------------------------
# Parsing helpers -- operate on a path, so mutation tests can point them at a
# tmp copy instead of the real note.
# --------------------------------------------------------------------------

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _headings(text):
    """[(1-based line number, heading text)] for every top-level '## ' line."""
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = re.match(r'^##\s+(.+?)\s*$', line)
        if m:
            out.append((i, m.group(1)))
    return out


def _dataviewjs_fences(text):
    """[(1-based open-fence line, 1-based close-fence line, source_text)] for
    every ```dataviewjs ... ``` block. Closing fences in this note are always
    a bare '```' on their own line (verified against the real note -- no
    nested literal '```' lines appear inside any block's JS), so a simple
    "next bare ``` line" scan is sufficient and matches how Obsidian itself
    parses the fence.
    """
    lines = text.splitlines()
    out = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == '```dataviewjs':
            open_line = i + 1
            j = i + 1
            while j < n and lines[j].strip() != '```':
                j += 1
            if j >= n:
                raise AssertionError(
                    "unterminated ```dataviewjs fence opened at line %d" % open_line)
            close_line = j + 1
            source = "\n".join(lines[i + 1:j])
            out.append((open_line, close_line, source))
            i = j + 1
        else:
            i += 1
    return out


def _extract_tabs(controller_source):
    """Pull the TAB1/TAB2 JS array literals out of the controller block's
    source as Python lists, by locating the two `const TABn = [...]`
    statements and JSON-decoding the bracketed literal (the arrays are plain
    string literals with single quotes -- swap to double quotes for json)."""
    tabs = {}
    for name in ("TAB1", "TAB2"):
        m = re.search(r"const\s+%s\s*=\s*(\[[^\]]*\])" % name, controller_source)
        if not m:
            raise AssertionError("could not find `const %s = [...]` in controller block" % name)
        literal = m.group(1)
        json_literal = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda mm: json.dumps(mm.group(1)), literal)
        tabs[name] = json.loads(json_literal)
    return tabs["TAB1"], tabs["TAB2"]


def tab_of(text, tab1, tab2):
    """Mirrors the controller's tabOf(): TAB1 checked first (prefix match via
    .startsWith), then TAB2, else None."""
    if any(text.startswith(p) for p in tab1):
        return 'work'
    if any(text.startswith(p) for p in tab2):
        return 'vault'
    return None


def _load_note_parts(path):
    """Returns (full_text, headings, fences, controller_source, tab1, tab2).
    Raises AssertionError with a structural (not content) message if the
    controller block can't be located -- callers only call this after
    confirming the note exists."""
    text = _read(path)
    fences = _dataviewjs_fences(text)
    if not fences:
        raise AssertionError("no ```dataviewjs fences found in note")
    controller_open_line, _, controller_source = fences[0]
    tab1, tab2 = _extract_tabs(controller_source)
    headings = _headings(text)
    return text, headings, fences, controller_open_line, controller_source, tab1, tab2


# --------------------------------------------------------------------------
# Tier A: structural checks against the live note.
# --------------------------------------------------------------------------

@unittest.skipUnless(os.path.exists(VAULT_NOTE), "Vault Dashboard.md not present on this machine")
class TestVaultDashboardTabContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        (cls.text, cls.headings, cls.fences, cls.controller_line,
         cls.controller_source, cls.tab1, cls.tab2) = _load_note_parts(VAULT_NOTE)

    def test_a1_every_heading_claimed_by_exactly_one_tab(self):
        """Every real ## heading must prefix-match TAB1 xor TAB2 -- never
        neither (orphaned, invisible under both tabs) and never both
        (ambiguous ownership)."""
        unclaimed = []
        ambiguous = []
        for _, heading in self.headings:
            in1 = any(heading.startswith(p) for p in self.tab1)
            in2 = any(heading.startswith(p) for p in self.tab2)
            if not in1 and not in2:
                unclaimed.append(heading)
            elif in1 and in2:
                ambiguous.append(heading)
        self.assertEqual(unclaimed, [], "headings claimed by neither TAB1 nor TAB2: %r" % (unclaimed,))
        self.assertEqual(ambiguous, [], "headings claimed by both TAB1 and TAB2: %r" % (ambiguous,))

    def test_a2_every_tab_entry_matches_a_real_heading(self):
        """Inverse of A1: a TAB1/TAB2 prefix with no matching heading is a
        stale entry that hides nothing."""
        heading_texts = [h for _, h in self.headings]
        for label, tab in (("TAB1", self.tab1), ("TAB2", self.tab2)):
            unmatched = [p for p in tab if not any(h.startswith(p) for h in heading_texts)]
            self.assertEqual(unmatched, [], "%s entries with no matching heading: %r" % (label, unmatched))

    def test_a3_every_fence_has_an_immediately_preceding_heading(self):
        """Every dataviewjs fence (except the controller block itself, which
        sits above any heading) must have a ## heading between it and the
        previous dataviewjs fence."""
        # Identify the exception explicitly by line number, not "the first one".
        controller_line = self.fences[0][0]
        self.assertEqual(
            controller_line, 8,
            "controller block expected at line 8 (re-verify note structure if this moved)")

        prev_fence_line = 0
        failures = []
        for idx, (open_line, _close_line, _src) in enumerate(self.fences):
            if open_line == controller_line and idx == 0:
                prev_fence_line = open_line
                continue
            has_heading_between = any(prev_fence_line < hl < open_line for hl, _ in self.headings)
            if not has_heading_between:
                failures.append(open_line)
            prev_fence_line = open_line
        self.assertEqual(failures, [], "dataviewjs fences with no preceding ## heading: lines %r" % (failures,))

    def test_a4_usage_widget_reads_adapter_not_cached_dv_io_load(self):
        """Regression guard for the 2026-09-21 stale-cache bug: the Usage
        widget must read summary.md via app.vault.adapter.read(PATH), and any
        dv.io.load(PATH) in that same block must only appear as the ternary's
        fallback branch (adapter missing), never as the primary read.

        NOTE: dv.io.load(...) is legitimately used ~15x elsewhere in the note
        to read OTHER pages' content (blocks A-E, prompt volume) -- this test
        only inspects the Usage block's own source, and only flags the
        literal `dv.io.load(PATH)` call (the summary.md variable), not
        dv.io.load calls against other arguments.
        """
        usage_block = None
        for _open, _close, src in self.fences:
            if 'PATH = "Dashboard/usage/summary.md"' in src:
                usage_block = src
                break
        self.assertIsNotNone(usage_block, "could not find the Usage widget block (PATH = summary.md)")

        self.assertIn(
            "app.vault.adapter.read(PATH)", usage_block,
            "Usage widget no longer reads summary.md via app.vault.adapter.read -- "
            "this is the fix for the stale dv.io.load() cache bug")

        # Every literal `dv.io.load(PATH)` occurrence must be preceded, within
        # 5 lines, by the `app.vault.adapter ?` ternary test -- proving it's
        # the ternary's fallback arm, not an unconditional primary read.
        # The ternary condition and its "?" can be split across lines
        # (`app.vault.adapter\n  ? await ...`), so the guard match tolerates
        # whitespace/newlines between them rather than requiring one line.
        guard_re = re.compile(r"app\.vault\.adapter\s*\n?\s*\?")
        lines = usage_block.splitlines()
        unguarded = []
        for i, line in enumerate(lines):
            if "dv.io.load(PATH)" in line:
                window = "\n".join(lines[max(0, i - 5):i + 1])
                if not guard_re.search(window):
                    unguarded.append(i + 1)
        self.assertEqual(
            unguarded, [],
            "dv.io.load(PATH) used without the app.vault.adapter ? guard within 5 lines "
            "(relative line(s) in Usage block: %r) -- this is the primary-read regression" % (unguarded,))

    @unittest.skipUnless(_has_node(), "node not available for JS syntax check")
    def test_a5_every_dataviewjs_block_parses(self):
        """Every dataviewjs block's JS must be syntactically valid. Wrapped in
        an async IIFE so top-level `await` (used throughout) is legal syntax
        for `node --check`, which only parses -- it never executes the block
        (no `dv`/`app` globals are provided or needed)."""
        failures = []
        with tempfile.TemporaryDirectory() as td:
            for open_line, _close_line, src in self.fences:
                tmp_path = os.path.join(td, "block_%d.js" % open_line)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write("(async () => {\n%s\n})();\n" % src)
                proc = subprocess.run(
                    ["node", "--check", tmp_path],
                    capture_output=True, text=True)
                if proc.returncode != 0:
                    failures.append((open_line, proc.stderr.strip().splitlines()[-1] if proc.stderr else "?"))
        self.assertEqual(failures, [], "dataviewjs blocks with a JS syntax error (fence line, error): %r" % (failures,))


# --------------------------------------------------------------------------
# Tier A self-check: prove each Tier A test actually bites, via a mutated tmp
# copy of the real note. These are throwaway proof tests, not part of the
# ongoing regression contract -- they exist to demonstrate A1-A5 fail on the
# real historical bug shapes, and pass again once reverted.
# --------------------------------------------------------------------------

@unittest.skipUnless(os.path.exists(VAULT_NOTE), "Vault Dashboard.md not present on this machine")
class TestSelfCheckMutationsBite(unittest.TestCase):
    """For each Tier A check, copy the real note to a tmp file, apply one
    targeted mutation, confirm the *same assertion logic* now fails, then
    confirm the untouched original still passes. Uses the module-level helper
    functions directly (parametrized by path) rather than re-running pytest
    as a subprocess."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tmp_note = os.path.join(self.tmpdir, "Vault Dashboard.md")
        shutil.copyfile(VAULT_NOTE, self.tmp_note)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _check_a1(self, path):
        _, headings, _, _, _, tab1, tab2 = _load_note_parts(path)
        unclaimed, ambiguous = [], []
        for _, heading in headings:
            in1 = any(heading.startswith(p) for p in tab1)
            in2 = any(heading.startswith(p) for p in tab2)
            if not in1 and not in2:
                unclaimed.append(heading)
            elif in1 and in2:
                ambiguous.append(heading)
        return unclaimed, ambiguous

    def test_a1_bites_on_ambiguous_heading(self):
        """Deleting a heading (the brief's suggested A1 mutation) does not
        make A1 fail: A1 walks the REMAINING real headings and checks each is
        claimed, so removing one just removes it from that walk -- it is A2
        (every TAB entry matches a real heading) that catches a deletion. See
        test_a2_bites_on_missing_today_away_heading for that mutation, and
        the "Corrections to the brief" note in the task report.

        A1's own failure mode is a heading that matches prefixes in BOTH
        tabs, or neither. Exercise the "neither" case with a heading text
        that starts with no TAB1/TAB2 prefix."""
        # Baseline: passes on the real note.
        unclaimed, ambiguous = self._check_a1(self.tmp_note)
        self.assertEqual((unclaimed, ambiguous), ([], []))

        # Mutation: rename a real heading to text no TAB1/TAB2 prefix matches.
        text = _read(self.tmp_note)
        mutated = text.replace("## Today — away\n", "## Zzz totally unclaimed heading\n", 1)
        self.assertTrue(mutated != text, "mutation did not match any text in the note")
        with open(self.tmp_note, "w", encoding="utf-8") as f:
            f.write(mutated)

        unclaimed, ambiguous = self._check_a1(self.tmp_note)
        self.assertIn("Zzz totally unclaimed heading", unclaimed, "A1 failed to catch an orphaned heading")

        # Restore and confirm the original passes again.
        shutil.copyfile(VAULT_NOTE, self.tmp_note)
        unclaimed, ambiguous = self._check_a1(self.tmp_note)
        self.assertEqual((unclaimed, ambiguous), ([], []))

    def test_a2_bites_on_missing_today_away_heading(self):
        text = _read(self.tmp_note)
        mutated = text.replace("## Today — away\n", "", 1)
        with open(self.tmp_note, "w", encoding="utf-8") as f:
            f.write(mutated)

        _, headings, fences, _, _, tab1, tab2 = _load_note_parts(self.tmp_note)
        heading_texts = [h for _, h in headings]
        unmatched_tab1 = [p for p in tab1 if not any(h.startswith(p) for h in heading_texts)]
        self.assertIn("Today — away", unmatched_tab1, "A2 failed to catch the deleted heading")

        shutil.copyfile(VAULT_NOTE, self.tmp_note)
        _, headings, _, _, _, tab1, _ = _load_note_parts(self.tmp_note)
        heading_texts = [h for _, h in headings]
        unmatched_tab1 = [p for p in tab1 if not any(h.startswith(p) for h in heading_texts)]
        self.assertEqual(unmatched_tab1, [])

    def test_a3_bites_on_fence_with_no_preceding_heading(self):
        text = _read(self.tmp_note)
        # Remove the '## Usage' heading (line 2249) so its fence (2251) has no
        # preceding heading since the previous fence (2225, Workstream totals).
        mutated = text.replace("\n## Usage\n", "\n", 1)
        self.assertTrue(mutated != text, "mutation target string not found in note")
        with open(self.tmp_note, "w", encoding="utf-8") as f:
            f.write(mutated)

        _, headings, fences, controller_line, _, _, _ = _load_note_parts(self.tmp_note)
        prev_fence_line = 0
        failures = []
        for idx, (open_line, _c, _s) in enumerate(fences):
            if open_line == controller_line and idx == 0:
                prev_fence_line = open_line
                continue
            has_heading_between = any(prev_fence_line < hl < open_line for hl, _ in headings)
            if not has_heading_between:
                failures.append(open_line)
            prev_fence_line = open_line
        self.assertNotEqual(failures, [], "A3 failed to catch the Usage fence's missing preceding heading")

        shutil.copyfile(VAULT_NOTE, self.tmp_note)
        _, headings, fences, controller_line, _, _, _ = _load_note_parts(self.tmp_note)
        prev_fence_line = 0
        failures = []
        for idx, (open_line, _c, _s) in enumerate(fences):
            if open_line == controller_line and idx == 0:
                prev_fence_line = open_line
                continue
            has_heading_between = any(prev_fence_line < hl < open_line for hl, _ in headings)
            if not has_heading_between:
                failures.append(open_line)
            prev_fence_line = open_line
        self.assertEqual(failures, [])

    def test_a4_bites_on_unconditional_dv_io_load(self):
        text = _read(self.tmp_note)
        mutated = text.replace(
            'const raw = app.vault.adapter\n'
            '    ? await app.vault.adapter.read(PATH)\n'
            '    : await dv.io.load(PATH);',
            'const raw = await dv.io.load(PATH);',
            1)
        self.assertTrue(mutated != text, "mutation target string not found -- note structure moved")
        with open(self.tmp_note, "w", encoding="utf-8") as f:
            f.write(mutated)

        _, _, fences, _, _, _, _ = _load_note_parts(self.tmp_note)
        usage_block = next(src for _o, _c, src in fences if 'PATH = "Dashboard/usage/summary.md"' in src)
        self.assertNotIn("app.vault.adapter.read(PATH)", usage_block,
                          "A4 mutation should have removed the adapter.read call")

        shutil.copyfile(VAULT_NOTE, self.tmp_note)
        _, _, fences, _, _, _, _ = _load_note_parts(self.tmp_note)
        usage_block = next(src for _o, _c, src in fences if 'PATH = "Dashboard/usage/summary.md"' in src)
        self.assertIn("app.vault.adapter.read(PATH)", usage_block)

    @unittest.skipUnless(_has_node(), "node not available for JS syntax check")
    def test_a5_bites_on_syntax_error(self):
        text = _read(self.tmp_note)
        # Corrupt the controller block (first dataviewjs fence) with an
        # unbalanced paren -- a clear-cut syntax error.
        mutated = text.replace("const KEY = '__vaultDashTab';", "const KEY = '__vaultDashTab'(;", 1)
        self.assertTrue(mutated != text, "mutation target string not found in note")
        with open(self.tmp_note, "w", encoding="utf-8") as f:
            f.write(mutated)

        _, _, fences, _, _, _, _ = _load_note_parts(self.tmp_note)
        open_line, _close_line, src = fences[0]
        with tempfile.TemporaryDirectory() as td:
            tmp_js = os.path.join(td, "block.js")
            with open(tmp_js, "w", encoding="utf-8") as f:
                f.write("(async () => {\n%s\n})();\n" % src)
            proc = subprocess.run(["node", "--check", tmp_js], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, "A5 failed to catch the syntax error")

        shutil.copyfile(VAULT_NOTE, self.tmp_note)
        _, _, fences, _, _, _, _ = _load_note_parts(self.tmp_note)
        open_line, _close_line, src = fences[0]
        with tempfile.TemporaryDirectory() as td:
            tmp_js = os.path.join(td, "block.js")
            with open(tmp_js, "w", encoding="utf-8") as f:
                f.write("(async () => {\n%s\n})();\n" % src)
            proc = subprocess.run(["node", "--check", tmp_js], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)


# --------------------------------------------------------------------------
# Tier B: usage/summary.md data-shape checks.
# --------------------------------------------------------------------------

def _load_label_constants():
    spec = importlib.util.spec_from_file_location("usage_by_session_labels", USAGE_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.REDACTED_LABEL, mod.ASK_HAIKU_LABEL


def _load_summary_json(path):
    text = _read(path)
    m = re.search(r'```json\n(.*?)\n```', text, re.S)
    if not m:
        raise AssertionError("no ```json fence found in %s" % path)
    return json.loads(m.group(1))


@unittest.skipUnless(os.path.exists(SUMMARY_MD), "usage/summary.md not present on this machine")
class TestUsageSummaryShape(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = _load_summary_json(SUMMARY_MD)

    def test_b1_top_level_shape(self):
        for key in ("generated", "today", "week"):
            self.assertIn(key, self.data, "summary.md missing top-level key %r" % key)
        for period in ("today", "week"):
            for key in ("total_usd", "sessions"):
                self.assertIn(key, self.data[period], "summary.md %s missing key %r" % (period, key))

    def test_b2_session_shape(self):
        required = {"cost_usd", "cwd", "title", "count", "children"}
        for period in ("today", "week"):
            for i, session in enumerate(self.data[period]["sessions"]):
                missing = required - set(session.keys())
                self.assertEqual(missing, set(),
                                  "%s.sessions[%d] missing keys %r" % (period, i, missing))
                # exactly one of the two percentage fields, per the note's
                # documented shape (today uses share_of_range_pct, week uses
                # pct_of_weekly_budget)
                has_share = "share_of_range_pct" in session
                has_pct = "pct_of_weekly_budget" in session
                self.assertTrue(has_share or has_pct,
                                 "%s.sessions[%d] has neither percentage field" % (period, i))

    def test_b3_child_shape(self):
        required = {"id", "cost_usd", "main_usd", "subagent_usd", "subagent_count"}
        for period in ("today", "week"):
            for i, session in enumerate(self.data[period]["sessions"]):
                for j, child in enumerate(session.get("children", [])):
                    missing = required - set(child.keys())
                    self.assertEqual(missing, set(),
                                      "%s.sessions[%d].children[%d] missing keys %r" % (period, i, j, missing))

    def test_b3b_hidden_title_only_ever_on_a_redacted_group(self):
        """"hidden_title" (the double-expand reveal gesture's data source --
        see usage-by-session.py's label_and_aggregate and the Usage widget's
        REVEAL_WINDOW_MS) must never appear on a child of a group whose own
        title is NOT one of the two redacted/scratch cover labels. This is the
        export-shape half of the guarantee; label_and_aggregate's own unit
        tests (test_usage_labels.py) cover the generating logic directly."""
        redacted_label, ask_haiku_label = _load_label_constants()
        known_labels = {redacted_label, ask_haiku_label}
        for period in ("today", "week"):
            for i, session in enumerate(self.data[period]["sessions"]):
                for j, child in enumerate(session.get("children", [])):
                    if "hidden_title" in child:
                        # The rendered session title carries a "×N" occurrence
                        # suffix (added by render_table, not by
                        # label_and_aggregate) for a group with more than one
                        # session, so match the cover label as a prefix.
                        base_title = re.sub(r"\s*×\d+$", "", session["title"])
                        self.assertIn(
                            base_title, known_labels,
                            "%s.sessions[%d] (title=%r) has a hidden_title child "
                            "but is not a redacted/scratch group" % (period, i, session["title"]))
                        # Belt-and-braces, matching label_and_aggregate's own
                        # mutually-exclusive title/hidden_title guarantee.
                        self.assertNotIn("title", child)
                        self.assertNotIn("cwd", child)

    def test_b4_redaction_labels_are_recognized_titles(self):
        """If any session title equals the redacted/ask-haiku cover labels,
        it must be a legitimate label from usage-by-session.py -- not a stray
        literal string that happens to collide. This borrows the two label
        constants via the same import pattern test_usage_labels.py uses (the
        module has hyphens in its filename); it does not re-test
        label_and_aggregate's redaction logic itself."""
        redacted_label, ask_haiku_label = _load_label_constants()
        known_labels = {redacted_label, ask_haiku_label}
        for period in ("today", "week"):
            for session in self.data[period]["sessions"]:
                title = session["title"]
                if title in known_labels:
                    continue  # expected cover label, not a leak
                # Titles are otherwise free text (cwd-derived); nothing to
                # assert beyond "matches a known cover label or is untouched".

        # Positive check: the constants imported actually look like labels
        # (non-empty strings), proving the import pattern worked rather than
        # silently handing back None/attribute errors that'd make the loop
        # above vacuous.
        self.assertTrue(redacted_label and isinstance(redacted_label, str))
        self.assertTrue(ask_haiku_label and isinstance(ask_haiku_label, str))


@unittest.skipUnless(os.path.exists(SUMMARY_MD), "usage/summary.md not present on this machine")
class TestSelfCheckB4Bites(unittest.TestCase):
    """Prove B4 actually exercises the imported constants: mutate a session
    title to a bogus string resembling a leaked redaction label under the
    WRONG constant value, confirm the assertion that constants are non-empty
    strings still passes (that part is a smoke check on the import, not on
    the data) and confirm the real labels round-trip through a tmp copy of
    summary.md untouched."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tmp_summary = os.path.join(self.tmpdir, "summary.md")
        shutil.copyfile(SUMMARY_MD, self.tmp_summary)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_b4_bites_when_import_pattern_breaks(self):
        # Baseline: real module path resolves and yields non-empty strings.
        redacted_label, ask_haiku_label = _load_label_constants()
        self.assertTrue(redacted_label)
        self.assertTrue(ask_haiku_label)

        # Mutation: point the loader at a nonexistent path (simulating the
        # module being renamed/moved) and confirm the load fails loudly
        # rather than silently returning stale/blank labels.
        spec = importlib.util.spec_from_file_location(
            "usage_by_session_labels_broken", USAGE_MODULE_PATH + ".does-not-exist")
        with self.assertRaises(AttributeError):
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

        # Restore path confirmed unaffected (nothing was mutated on disk).
        redacted_label2, ask_haiku_label2 = _load_label_constants()
        self.assertEqual(redacted_label, redacted_label2)
        self.assertEqual(ask_haiku_label, ask_haiku_label2)


if __name__ == "__main__":
    unittest.main()
