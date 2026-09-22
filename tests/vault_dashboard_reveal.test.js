"use strict";
/*
 * jsdom test for the Usage widget's double-expand reveal gesture (Obsidian
 * vault note "Dashboard/Vault Dashboard.md", the "## Usage" dataviewjs
 * block, REVEAL_WINDOW_MS). Runs the block's REAL source (extracted from the
 * note verbatim -- no reimplementation to drift out of sync) inside a jsdom
 * window, with `app.vault.adapter.read` stubbed to return synthetic
 * summary.md JSON, and `Date.now` overridden per click so the timing window
 * can be driven deterministically instead of with a real sleep.
 *
 * Run: node tests/vault_dashboard_reveal.test.js   (from the worktime repo,
 * after `npm install` has fetched jsdom into node_modules).
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const VAULT_NOTE = "/home/esme/obsidian-vault/Dashboard/Vault Dashboard.md";

// Skip gracefully (exit 0) when the vault note isn't present on this
// machine -- matches test_vault_dashboard.py's own skip-if-absent policy for
// the same file, since it lives outside this repo.
if (!fs.existsSync(VAULT_NOTE)) {
  console.log("SKIP: vault note not present on this machine");
  process.exit(0);
}

/* Extract the "## Usage" section's ```dataviewjs ... ``` block verbatim --
 * the same fence-scanning shape as test_vault_dashboard.py's
 * _dataviewjs_fences(), just in JS, so a future edit to the widget's real
 * source is what this test exercises, not a hand-copied duplicate of it. */
function extractUsageBlock(noteText) {
  const lines = noteText.split("\n");
  const headingIdx = lines.findIndex((l) => l.trim() === "## Usage");
  if (headingIdx === -1) throw new Error('no "## Usage" heading found');
  let openIdx = -1;
  for (let i = headingIdx; i < lines.length; i++) {
    if (lines[i].trim() === "```dataviewjs") { openIdx = i; break; }
  }
  if (openIdx === -1) throw new Error("no dataviewjs fence after ## Usage");
  let closeIdx = -1;
  for (let i = openIdx + 1; i < lines.length; i++) {
    if (lines[i].trim() === "```") { closeIdx = i; break; }
  }
  if (closeIdx === -1) throw new Error("unterminated dataviewjs fence");
  return lines.slice(openIdx + 1, closeIdx).join("\n");
}

const usageSource = extractUsageBlock(fs.readFileSync(VAULT_NOTE, "utf-8"));

const FAKE_ID_A1 = "aaa11111";
const FAKE_ID_A2 = "aaa22222";
const FAKE_ID_B1 = "bbb11111";
const FAKE_ID_B2 = "bbb22222";

// Two separate redacted, multi-child groups ("gen work" ×2 and a second
// redacted-looking group) so the cross-group leak scenario has two distinct
// groups to toggle. Real usage-by-session.py output has both "title" absent
// and "hidden_title" present for every child here -- exactly the shape
// label_and_aggregate produces for a redacted group's children.
function makeSummary() {
  return {
    generated: new Date().toISOString(),
    today: {
      total_usd: 20,
      sessions: [
        {
          title: "gen work",
          cwd: null,
          cost_usd: 10,
          share_of_range_pct: 50,
          count: 2,
          children: [
            { id: FAKE_ID_A1, cost_usd: 6, main_usd: 6, subagent_usd: 0, subagent_count: 0, hidden_title: "Real Title A1" },
            { id: FAKE_ID_A2, cost_usd: 4, main_usd: 4, subagent_usd: 0, subagent_count: 0, hidden_title: "Real Title A2" },
          ],
        },
        {
          title: "gen work",
          cwd: null,
          cost_usd: 10,
          share_of_range_pct: 50,
          count: 2,
          children: [
            { id: FAKE_ID_B1, cost_usd: 7, main_usd: 7, subagent_usd: 0, subagent_count: 0, hidden_title: "Real Title B1" },
            { id: FAKE_ID_B2, cost_usd: 3, main_usd: 3, subagent_usd: 0, subagent_count: 0, hidden_title: "Real Title B2" },
          ],
        },
      ],
    },
    week: { total_usd: 20, sessions: [] },
  };
}

/* Runs the real widget source in a fresh jsdom window, stubbing just enough
 * of Obsidian's `app`/`dv` API for this block to execute end to end, and
 * returns the window plus a helper to read back the two group `<tr>` rows
 * (data-driven by document order: Today's two sessions render in the order
 * `sessions` lists them). */
async function loadWidget(summary) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "dangerously" });
  const { window } = dom;
  const fenced = "```json\n" + JSON.stringify(summary) + "\n```\n";

  window.app = { vault: { adapter: { read: async () => fenced } } };
  window.dv = {
    io: { load: async () => { throw new Error("adapter.read should have been used, not dv.io.load"); } },
    paragraph: () => {},
    table: () => {},
    el: (tag, text, opts) => {
      const el = window.document.createElement(tag);
      el.textContent = text;
      if (opts && opts.attr && opts.attr.style) el.style.cssText = opts.attr.style;
      window.document.body.appendChild(el);
      return el;
    },
    container: window.document.body,
  };

  // The dataviewjs block's top level is `await`-ing directly (Obsidian runs
  // it inside an async wrapper) -- reproduce that here, and surface a thrown
  // error instead of leaving the promise to reject silently.
  window.__ready = new Promise((resolve, reject) => {
    window.__resolve = resolve;
    window.__reject = reject;
  });
  const script = window.document.createElement("script");
  script.textContent =
    "(async () => {\n" + usageSource + "\n})().then(window.__resolve, window.__reject);";
  window.document.body.appendChild(script);
  await window.__ready;

  // Every expandable group renders one top-level <tr> with a click listener
  // per section() (this test only ever populates "today", so all group rows
  // are inside the first <table>'s <tbody>, in `sessions` order).
  const groupRows = Array.from(window.document.querySelectorAll("table"))[0]
    .querySelectorAll("tbody > tr");
  return { window, groupRows };
}

// Fixes `Date.now()` (as the widget's own code calls it, via the window's
// global Date -- overriding it here, not the Node process's own Date, is
// what makes this deterministic instead of a real sleep) to `ts` for the
// duration of `fn`, then restores the original.
function withClock(window, ts, fn) {
  const real = window.Date.now;
  window.Date.now = () => ts;
  try {
    return fn();
  } finally {
    window.Date.now = real;
  }
}

function childCellTexts(groupRow) {
  // The child table is the sole <table> inside the row immediately after
  // the group row (built by buildChildTable, appended by the click handler).
  const childRow = groupRow.nextElementSibling;
  if (!childRow) return null;
  return Array.from(childRow.querySelectorAll("table tbody tr")).map((tr) =>
    Array.from(tr.children).map((td) => td.textContent)
  );
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`PASS: ${name}`);
  } else {
    failures++;
    console.log(`FAIL: ${name}${detail ? " -- " + detail : ""}`);
  }
}

(async () => {
  // --- Scenario 1: a single ordinary expand stays fully redacted. ---
  {
    const { groupRows } = await loadWidget(makeSummary());
    const rowA = groupRows[0];
    withClock(rowA.ownerDocument.defaultView, 1000, () => rowA.click());
    const cells = childCellTexts(rowA);
    check(
      "single expand: no hidden title text",
      cells !== null && !cells.some((row) => row.some((t) => t.includes("Real Title"))),
      JSON.stringify(cells)
    );
    check(
      "single expand: id+cost only (3 cells: id, cost, detail -- no title column)",
      cells.every((row) => row.length === 3),
      JSON.stringify(cells)
    );
  }

  // --- Scenario 2: expand, collapse, expand within the window reveals. ---
  {
    const { groupRows } = await loadWidget(makeSummary());
    const rowA = groupRows[0];
    const win = rowA.ownerDocument.defaultView;
    withClock(win, 1000, () => rowA.click()); // expand 1
    withClock(win, 1100, () => rowA.click()); // collapse
    withClock(win, 1200, () => rowA.click()); // expand 2, 200ms after expand 1
    const cells = childCellTexts(rowA);
    check(
      "double expand within window: hidden titles shown",
      cells.some((row) => row.some((t) => t === "Real Title A1")) &&
        cells.some((row) => row.some((t) => t === "Real Title A2")),
      JSON.stringify(cells)
    );
  }

  // --- Scenario 3: same sequence, but slower than the window -- no reveal. ---
  {
    const { groupRows } = await loadWidget(makeSummary());
    const rowA = groupRows[0];
    const win = rowA.ownerDocument.defaultView;
    withClock(win, 1000, () => rowA.click()); // expand 1
    withClock(win, 1100, () => rowA.click()); // collapse
    withClock(win, 1000 + 1600, () => rowA.click()); // expand 2, 1600ms later -- past REVEAL_WINDOW_MS
    const cells = childCellTexts(rowA);
    check(
      "double expand slower than window: still redacted",
      !cells.some((row) => row.some((t) => t.includes("Real Title"))),
      JSON.stringify(cells)
    );
  }

  // --- Scenario 4: collapsing after a reveal resets it. ---
  {
    const { groupRows } = await loadWidget(makeSummary());
    const rowA = groupRows[0];
    const win = rowA.ownerDocument.defaultView;
    withClock(win, 1000, () => rowA.click()); // expand 1
    withClock(win, 1100, () => rowA.click()); // collapse
    withClock(win, 1200, () => rowA.click()); // expand 2 -- reveals
    withClock(win, 1300, () => rowA.click()); // collapse again -- should reset
    withClock(win, 10000, () => rowA.click()); // ordinary expand, long after -- must be redacted
    const cells = childCellTexts(rowA);
    check(
      "collapse after reveal resets: next ordinary expand is redacted",
      !cells.some((row) => row.some((t) => t.includes("Real Title"))),
      JSON.stringify(cells)
    );
  }

  // --- Scenario 5: revealing group A then rapid-toggling group B must not
  // leak A's state (or A's titles) into B. ---
  {
    const { groupRows } = await loadWidget(makeSummary());
    const [rowA, rowB] = groupRows;
    const win = rowA.ownerDocument.defaultView;
    withClock(win, 1000, () => rowA.click()); // A: expand 1
    withClock(win, 1100, () => rowA.click()); // A: collapse
    withClock(win, 1200, () => rowA.click()); // A: expand 2 -- A is now revealed
    const cellsA = childCellTexts(rowA);
    check(
      "group A revealed as expected (sanity check before the leak test)",
      cellsA.some((row) => row.some((t) => t === "Real Title A1")),
      JSON.stringify(cellsA)
    );
    // B has never been touched -- a single ordinary expand, right after A's
    // reveal, must stay redacted. A shared/global (rather than per-group)
    // timer or reveal flag would leak A's revealed state onto this click.
    withClock(win, 1250, () => rowB.click());
    const cellsB = childCellTexts(rowB);
    check(
      "group B's first expand right after A's reveal stays redacted (no cross-group leak)",
      cellsB !== null && !cellsB.some((row) => row.some((t) => t.includes("Real Title"))),
      JSON.stringify(cellsB)
    );
  }

  console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
  console.error("ERROR:", e.stack || e);
  process.exit(1);
});
