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

// Fixture for the dollars-to-percentages checks (Sept 2026 rework: the
// widget no longer shows cost_usd anywhere, only derived percentages). One
// session sits on the real pct_of_weekly_budget path with a clean
// proportional split (group 20% of week, cost 10 -> child A1 cost 6 gets
// 20*6/10=12%, child A2 cost 4 gets 20*4/10=8%) so the expected percentage
// strings are exact, not approximated. A second session sits on the
// share_of_range_pct fallback path (no pct_of_weekly_budget at all) to prove
// child percentages are omitted rather than invented there. A third session
// has cost_usd 0 with pct_of_weekly_budget present, to prove the zero-cost
// guard renders blank rather than NaN/Infinity.
function makePctSummary() {
  return {
    generated: new Date().toISOString(),
    today: {
      total_usd: 50,
      sessions: [
        {
          title: "weekly-budget group",
          cwd: null,
          cost_usd: 10,
          pct_of_weekly_budget: 20,
          count: 2,
          children: [
            // subagent split chosen to sum to exactly 100% after rounding:
            // main 3/10=30%, sub 7/10=70%.
            { id: "wb111111", cost_usd: 6, main_usd: 1.8, subagent_usd: 4.2, subagent_count: 5, hidden_title: "WB Child 1" },
            { id: "wb222222", cost_usd: 4, main_usd: 4, subagent_usd: 0, subagent_count: 0, hidden_title: "WB Child 2" },
          ],
        },
        {
          title: "fallback group",
          cwd: null,
          cost_usd: 10,
          share_of_range_pct: 33.3,
          count: 2,
          children: [
            { id: "fb111111", cost_usd: 5, main_usd: 2, subagent_usd: 3, subagent_count: 2, hidden_title: "FB Child 1" },
            { id: "fb222222", cost_usd: 5, main_usd: 5, subagent_usd: 0, subagent_count: 0, hidden_title: "FB Child 2" },
          ],
        },
        {
          title: "zero-cost group",
          cwd: null,
          cost_usd: 0,
          pct_of_weekly_budget: 5,
          count: 2,
          children: [
            { id: "zc111111", cost_usd: 0, main_usd: 0, subagent_usd: 0, subagent_count: 0, hidden_title: "ZC Child 1" },
            { id: "zc222222", cost_usd: 0, main_usd: 0, subagent_usd: 0, subagent_count: 3, hidden_title: "ZC Child 2" },
          ],
        },
      ],
    },
    week: { total_usd: 50, sessions: [] },
  };
}

// Fixture for the header-derivation fix (Sept 2026: summing only the
// DISPLAYED (slice(0,6)) sessions' pct_of_weekly_budget silently undercounts
// whenever the range has a tail past the display cutoff -- the header must
// instead derive the range's TRUE share of the week from a single session's
// pct_of_weekly_budget / share_of_range_pct ratio, which is exact regardless
// of how many sessions are shown).
//
// Both `today` and `week` here use the SAME 9-session range, built so the
// numbers are exact and the "displayed vs true" gap is unmistakable:
//   - total range cost = 100 (60 + 5*6 + 3*(10/3))
//   - week budget = 300 (so pct_of_weekly_budget = cost/3)
//   - costliest session: cost 60, share_of_range_pct 60, pct_of_weekly_budget 20
//     -> derived range pct = 20 * 100 / 60 = 33.333...% -> "33%"
//   - the top 6 by cost (costliest + the five cost-6 sessions) sum to
//     pct_of_weekly_budget 20 + 5*2 = 30% -- the OLD (buggy) summed header
//     would show "30%", visibly different from the true "33%".
//   - the remaining 3 sessions (cost 10/3 each, i.e. the tail past the
//     display cutoff) are what the sum silently drops.
function makeHeaderSummary() {
  const tailCost = 10 / 3;
  const sessions = [
    { title: "costliest", cwd: null, cost_usd: 60, share_of_range_pct: 60, pct_of_weekly_budget: 20, count: 1, children: [] },
    ...Array.from({ length: 5 }, (_, i) => ({
      title: `mid-${i}`, cwd: null, cost_usd: 6, share_of_range_pct: 6, pct_of_weekly_budget: 2, count: 1, children: [],
    })),
    ...Array.from({ length: 3 }, (_, i) => ({
      title: `tail-${i}`, cwd: null, cost_usd: tailCost, share_of_range_pct: tailCost, pct_of_weekly_budget: tailCost / 3, count: 1, children: [],
    })),
  ];
  return {
    generated: new Date().toISOString(),
    today: { total_usd: 100, sessions },
    week: { total_usd: 100, sessions },
  };
}

// Fixture for the share_of_range_pct-zero/absent guard: the only session in
// the range has pct_of_weekly_budget but share_of_range_pct is 0 (would
// divide by zero in the derivation), so the header must fall back to the
// plain title with no percentage figure at all, not NaN/Infinity.
function makeZeroShareSummary() {
  return {
    generated: new Date().toISOString(),
    today: { total_usd: 0, sessions: [] },
    week: {
      total_usd: 40,
      sessions: [
        { title: "zero-share", cwd: null, cost_usd: 40, share_of_range_pct: 0, pct_of_weekly_budget: 25, count: 1, children: [] },
      ],
    },
  };
}

// Pulls the two section header <div> texts (rendered via dv.el, so they're
// plain <div>s in document.body, same as the footnote) out by their
// distinguishing prefix.
function sectionHeaders(window) {
  const divs = Array.from(window.document.querySelectorAll("div")).map((d) => d.textContent);
  return {
    today: divs.find((t) => t.startsWith("Today")),
    week: divs.find((t) => t.startsWith("This week")),
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
      "single expand: id+pct only (3 cells: id, pct, detail -- no title column)",
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

  // --- Scenario 6: no dollar figures anywhere in the widget's rendered
  // output (Sept 2026 rework -- Oliver only wants percentages). Covers both
  // the section headers/footnote (rendered via dv.el into document.body)
  // and every group/child table cell. ---
  {
    const { window } = await loadWidget(makePctSummary());
    // Excludes the injected <script> element itself -- its textContent is
    // the widget's SOURCE code (which legitimately mentions "$" in a
    // comment and in template-literal syntax), not rendered output. Every
    // dv.el()/table cell the widget actually renders lands elsewhere in
    // document.body, so summing sibling text minus the script's own is the
    // real check.
    const scriptEl = window.document.querySelector("script");
    const bodyText = Array.from(window.document.body.childNodes)
      .filter((n) => n !== scriptEl)
      .map((n) => n.textContent)
      .join(" ");
    check(
      "no dollar figures anywhere in rendered output",
      !bodyText.includes("$"),
      bodyText
    );
  }

  // --- Scenario 7: a group row is exactly two columns (Session, %) now
  // that the $ column is gone. ---
  {
    const { groupRows } = await loadWidget(makePctSummary());
    const rows = Array.from(groupRows);
    check(
      "group row renders exactly two columns",
      rows.every((tr) => tr.children.length === 2),
      rows.map((tr) => tr.children.length).join(",")
    );
  }

  // --- Scenario 8: a child's derived percentage matches the expected
  // proportional value (group.pct_of_weekly_budget * child.cost_usd /
  // group.cost_usd) for a known fixture, using the same fmtPct rounding
  // rule as the session rows (toFixed(1) under 10, toFixed(0) at/above). ---
  {
    const { groupRows } = await loadWidget(makePctSummary());
    const wbRow = groupRows[0]; // "weekly-budget group", cost 10, pct_of_weekly_budget 20
    withClock(wbRow.ownerDocument.defaultView, 1000, () => wbRow.click());
    const cells = childCellTexts(wbRow);
    // Row shape here is [title, id, pct, detail] since these children carry
    // hidden_title but this is an ordinary (non-reveal) single expand, so
    // title is omitted -- shape is [id, pct, detail].
    const pctById = Object.fromEntries(cells.map((row) => [row[0], row[1]]));
    check(
      "child pct: 20 * 6/10 = 12% (>=10 -> toFixed(0))",
      pctById["wb111111"] === "12%",
      JSON.stringify(cells)
    );
    check(
      "child pct: 20 * 4/10 = 8% (<10 -> toFixed(1))",
      pctById["wb222222"] === "8.0%",
      JSON.stringify(cells)
    );
  }

  // --- Scenario 9: the main/sub split renders as percentages summing to
  // 100 (allowing for rounding), and is omitted entirely when
  // subagent_count is 0. Also covers the fallback (share_of_range_pct)
  // group, where the child pct column is omitted but main/sub still
  // renders since it needs no weekly-budget basis. ---
  {
    const { groupRows } = await loadWidget(makePctSummary());
    const win = groupRows[0].ownerDocument.defaultView;

    withClock(win, 1000, () => groupRows[0].click()); // weekly-budget group
    const wbCells = childCellTexts(groupRows[0]);
    const wbChild1Detail = wbCells.find((row) => row.includes("wb111111"))[2];
    const wbMatch = wbChild1Detail.match(/^main (\d+)% \/ sub (\d+)% \(5 agents\)$/);
    check(
      "main/sub split: percentages present and sum to 100 (allowing rounding)",
      wbMatch !== null && Math.abs((+wbMatch[1] + +wbMatch[2]) - 100) <= 1,
      wbChild1Detail
    );
    const wbChild2Detail = wbCells.find((row) => row.includes("wb222222"))[2];
    check(
      "main/sub split omitted when subagent_count is 0",
      wbChild2Detail === "",
      JSON.stringify(wbChild2Detail)
    );

    withClock(win, 1000, () => groupRows[1].click()); // fallback group
    const fbCells = childCellTexts(groupRows[1]);
    const fbChild1 = fbCells.find((row) => row.includes("fb111111"));
    check(
      "fallback path: child pct column omitted (no weekly-budget basis)",
      fbChild1[1] === "",
      JSON.stringify(fbChild1)
    );
    const fbMatch = fbChild1[2].match(/^main (\d+)% \/ sub (\d+)% \(2 agents\)$/);
    check(
      "fallback path: main/sub split still renders (needs no budget basis)",
      fbMatch !== null && Math.abs((+fbMatch[1] + +fbMatch[2]) - 100) <= 1,
      JSON.stringify(fbChild1)
    );
  }

  // --- Scenario 10: a group with cost 0 (and thus an undefined proportional
  // basis) or a missing pct never renders NaN/Infinity/"undefined", for
  // either the child pct column or the main/sub split. ---
  {
    const { groupRows } = await loadWidget(makePctSummary());
    const win = groupRows[0].ownerDocument.defaultView;
    const zcRow = groupRows[2]; // "zero-cost group", cost_usd 0
    withClock(win, 1000, () => zcRow.click());
    const zcCells = childCellTexts(zcRow);
    const zcText = JSON.stringify(zcCells);
    check(
      "zero-cost group renders no NaN/Infinity/undefined",
      !/NaN|Infinity|undefined/.test(zcText),
      zcText
    );
  }

  // --- Scenario 11: header derivation must use the costliest session's
  // pct_of_weekly_budget/share_of_range_pct ratio, not a sum over the
  // displayed (top-6) sessions -- see makeHeaderSummary's comment for the
  // exact numbers (true 33%, buggy-sum 30%). ---
  {
    const { window } = await loadWidget(makeHeaderSummary());
    const headers = sectionHeaders(window);
    check(
      "header renders the DERIVED range pct (33%), not the displayed-sessions sum (30%)",
      headers.today === "Today — 33% of week",
      JSON.stringify(headers)
    );
  }

  // --- Scenario 12: "of week" suffix appears on the Today header but not
  // on the This week header (the suffix is circular there). Same fixture as
  // scenario 11 -- both ranges carry identical data, so this isolates the
  // suffix logic from the derivation logic. ---
  {
    const { window } = await loadWidget(makeHeaderSummary());
    const headers = sectionHeaders(window);
    check(
      "This week header has no 'of week' suffix",
      headers.week === "This week — 33%",
      JSON.stringify(headers)
    );
    check(
      "Today header keeps the 'of week' suffix",
      headers.today.endsWith(" of week"),
      JSON.stringify(headers)
    );
  }

  // --- Scenario 13: share_of_range_pct zero (or absent) guards the
  // derivation -- falls back to the plain title, no percentage, and never
  // NaN/Infinity/undefined anywhere in the rendered output. ---
  {
    const { window } = await loadWidget(makeZeroShareSummary());
    const headers = sectionHeaders(window);
    check(
      "share_of_range_pct=0 falls back to plain title, no percentage",
      headers.week === "This week",
      JSON.stringify(headers)
    );
    // Excludes the injected <script> element's own textContent (the
    // widget's source, which legitimately mentions these words in
    // comments/code) -- same rationale as the no-dollars check above.
    const scriptEl = window.document.querySelector("script");
    const bodyText = Array.from(window.document.body.childNodes)
      .filter((n) => n !== scriptEl)
      .map((n) => n.textContent)
      .join(" ");
    check(
      "zero-share guard renders no NaN/Infinity/undefined",
      !/NaN|Infinity|undefined/.test(bodyText),
      bodyText
    );
  }

  console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
  console.error("ERROR:", e.stack || e);
  process.exit(1);
});
