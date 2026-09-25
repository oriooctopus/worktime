"use strict";
/*
 * jsdom test for the Usage widget's Day/Week click-through nav (Obsidian
 * vault note "Dashboard/Vault Dashboard.md", the "## Usage" dataviewjs
 * block). Runs the block's REAL source (extracted verbatim, same approach
 * as vault_dashboard_reveal.test.js) inside a jsdom window, with
 * `app.vault.adapter.read` stubbed to return synthetic summary.md JSON that
 * includes the new "days"/"weeks" keys.
 *
 * Run: node tests/vault_dashboard_usage_nav.test.js
 */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const VAULT_NOTE = "/home/esme/obsidian-vault/Dashboard/Vault Dashboard.md";

if (!fs.existsSync(VAULT_NOTE)) {
  console.log("SKIP: vault note not present on this machine");
  process.exit(0);
}

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

// 5 days (today .. 4 days back) + 2 weeks (current + 1 previous), each with
// a distinct, recognizable total so a test can tell which range's data is
// on screen just from the rendered header/session row.
function makeHistorySummary() {
  const today = new Date();
  const iso = (d) => d.toISOString().slice(0, 10);
  const days = {};
  for (let i = 0; i < 5; i++) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    const key = iso(d);
    days[key] = {
      total_usd: 10 + i,
      sessions: [{ title: `day-${key}`, cwd: null, cost_usd: 10 + i, share_of_range_pct: 100, count: 1, children: [] }],
    };
  }
  const weekStart = (k) => {
    const d = new Date(today);
    d.setDate(d.getDate() - today.getDay() - 7 * k);
    return d;
  };
  const weeks = [1, 0].map((k) => {
    const s = weekStart(k);
    const e = new Date(s);
    e.setDate(e.getDate() + 7);
    return {
      start: iso(s),
      end: iso(e),
      current: k === 0,
      total_usd: 100 + k,
      sessions: [{ title: `week-k${k}`, cwd: null, cost_usd: 100 + k, share_of_range_pct: 100, count: 1, children: [] }],
    };
  });
  return {
    generated: new Date().toISOString(),
    today: days[iso(today)],
    week: weeks[1],
    days,
    weeks,
    // Dollars-per-1%-of-weekly-budget, from usage-export.py's current
    // window (see its own docstring). None of the fixture's day/week
    // sessions above carry pct_of_weekly_budget (only today/current-week
    // rows do in the real export), so this is what lets a PAST day/week
    // still show a "% week" figure -- rate=20 makes the arithmetic easy to
    // eyeball: a $10 day -> 50%, a $11 day -> 55% ($ per 1% of budget, so
    // pct = cost_usd / rate -- NOT rate / cost_usd).
    usd_per_week_pct: 0.2,
  };
}

async function loadWidget(summary) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", { runScripts: "dangerously" });
  const { window } = dom;
  let fenced = "```json\n" + JSON.stringify(summary) + "\n```\n";

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
  window.setInterval = () => 1;
  window.clearInterval = () => {};

  window.__ready = new Promise((resolve, reject) => {
    window.__resolve = resolve;
    window.__reject = reject;
  });
  const script = window.document.createElement("script");
  script.textContent =
    "(async () => {\n" + usageSource + "\n})().then(window.__resolve, window.__reject);";
  window.document.body.appendChild(script);
  await window.__ready;

  return { window };
}

function navLabel(window) {
  // The nav bar is the first leafless <div> containing exactly the Day/Week
  // buttons + ‹ label › -- its label <span> is the middle child.
  const buttons = Array.from(window.document.querySelectorAll("button"));
  const btnDay = buttons.find((b) => b.textContent === "Day");
  const btnWeek = buttons.find((b) => b.textContent === "Week");
  const prev = buttons.find((b) => b.textContent === "‹");
  const next = buttons.find((b) => b.textContent === "›");
  const label = prev.nextElementSibling;
  return { btnDay, btnWeek, prev, next, label };
}

function sectionHeader(window) {
  const divs = Array.from(window.document.querySelectorAll("div")).filter((d) => d.children.length === 0);
  // The header div is the one immediately preceding the results <table>.
  return divs.find((d) => d.nextElementSibling && d.nextElementSibling.tagName === "TABLE");
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log(`PASS: ${name}`);
  else { failures++; console.log(`FAIL: ${name}${detail ? " -- " + detail : ""}`); }
}

(async () => {
  const summary = makeHistorySummary();
  const todayKey = Object.keys(summary.days).sort().reverse()[0];
  const yesterdayKey = Object.keys(summary.days).sort().reverse()[1];

  // --- Default view is today, in day mode. ---
  {
    const { window } = await loadWidget(summary);
    const { btnDay, next } = navLabel(window);
    check("default mode is Day (bold)", btnDay.style.fontWeight === "bold", btnDay.style.fontWeight);
    check("default day is today -- forward (›) disabled at newest", next.disabled === true, "");
    const header = sectionHeader(window);
    check("today's session row renders", header.parentElement.textContent.includes(`day-${todayKey}`) ||
      Array.from(window.document.querySelectorAll("td")).some((td) => td.textContent.includes(`day-${todayKey}`)));
  }

  // --- Clicking ‹ steps back one day; the row shown changes accordingly. ---
  {
    const { window } = await loadWidget(summary);
    navLabel(window).prev.click();
    const tds = Array.from(window.document.querySelectorAll("td")).map((td) => td.textContent);
    check("‹ once shows yesterday's session, not today's",
      tds.some((t) => t.includes(`day-${yesterdayKey}`)) && !tds.some((t) => t.includes(`day-${todayKey}`)),
      tds.join(" | "));
  }

  // --- › is disabled at the newest day and never overshoots. Every click
  // rebuilds the nav bar (render() replaces the DOM wholesale each time), so
  // each button is re-fetched right before it's clicked or inspected --
  // reusing a stale reference would read a detached element's frozen state,
  // not what's actually on screen (this bit a first draft of this test: it
  // reused one `next` reference across all three steps and passed for the
  // wrong reason -- that button's `disabled` had been true since the very
  // first render, before any click, so the assertion never touched the
  // overshoot-guard code at all). ---
  {
    const { window } = await loadWidget(summary);
    navLabel(window).prev.click();
    navLabel(window).next.click(); // back to today
    navLabel(window).next.click(); // no-op, already at newest
    check("› does not go past today", navLabel(window).next.disabled === true, "");
  }

  // --- ‹ is disabled once the oldest cached day is reached. Each click
  // rebuilds the nav bar (render() replaces the DOM wholesale), so the
  // button must be re-fetched after every click -- reusing a stale
  // reference would read a detached element's (frozen) disabled state. ---
  {
    const { window } = await loadWidget(summary);
    for (let i = 0; i < 10; i++) navLabel(window).prev.click(); // far more than the 5 days available
    check("‹ stops at the oldest available day (no crash, no undefined label)",
      navLabel(window).prev.disabled === true, "");
  }

  // --- A past (closed) day derives its "% week" from usd_per_week_pct
  // rather than the misleading "% of range" (share_of_range_pct) -- this is
  // the follow-up fix: yesterday's fixture session costs $11 against a
  // rate of 20 (usd_per_week_pct), so its row should read 55%, and the
  // header should carry the same "— 55% of week" suffix a current-window
  // day would get, not the old plain title (canDeriveRangePct is false
  // here since none of the fixture's rows set pct_of_weekly_budget). ---
  {
    const { window } = await loadWidget(summary);
    navLabel(window).prev.click(); // now on yesterday
    const header = sectionHeader(window);
    check("past day header derives '% week' suffix from usd_per_week_pct",
      header.textContent.includes("55%") && header.textContent.includes("of week"),
      header.textContent);
    const tds = Array.from(window.document.querySelectorAll("td")).map((td) => td.textContent);
    check("past day's row % is cost_usd / usd_per_week_pct (11/0.2 = 55%)",
      tds.some((t) => t.trim() === "55%"), tds.join(" | "));
  }

  // --- Toggling to Week mode shows the current week by default, with its
  // own label and a disabled ›. ---
  {
    const { window } = await loadWidget(summary);
    navLabel(window).btnWeek.click();
    const { next, label } = navLabel(window);
    check("Week mode: › disabled at current window", next.disabled === true, "");
    const tds = Array.from(window.document.querySelectorAll("td")).map((td) => td.textContent);
    check("Week mode: current week's session renders", tds.some((t) => t.includes("week-k0")), tds.join(" | "));
    check("Week mode label is a date range (contains an en dash)", label.textContent.includes("–"), label.textContent);
  }

  // --- Week mode ‹ steps to the previous window. ---
  {
    const { window } = await loadWidget(summary);
    navLabel(window).btnWeek.click();
    navLabel(window).prev.click();
    const tds = Array.from(window.document.querySelectorAll("td")).map((td) => td.textContent);
    check("Week ‹ shows the previous window's session, not the current one",
      tds.some((t) => t.includes("week-k1")) && !tds.some((t) => t.includes("week-k0")),
      tds.join(" | "));
  }

  // --- Selection survives a repaint (simulates the 5-min poll re-render
  // calling render(data) again with the same data). ---
  {
    const { window } = await loadWidget(summary);
    navLabel(window).prev.click(); // now on yesterday
    // Nothing in this widget exposes render() directly to the test, but the
    // poll timer's own callback does exactly this: re-invoke render(data).
    // Simulate it by clicking Day again, which the widget itself implements
    // as `render(data)` -- since navDayIdx is module-level, it must still
    // read "yesterday" afterward, proving the state is not reset by a
    // fresh render() call.
    navLabel(window).btnDay.click();
    const tds = Array.from(window.document.querySelectorAll("td")).map((td) => td.textContent);
    check("selection (yesterday) survives a render(data) re-run",
      tds.some((t) => t.includes(`day-${yesterdayKey}`)), tds.join(" | "));
  }

  // --- A days/weeks payload that lacks usd_per_week_pct (older synced
  // export, before this fix) must keep the pre-fix "% of range" fallback on
  // a past day, not silently show a blank or NaN%. ---
  {
    const noRate = JSON.parse(JSON.stringify(summary));
    delete noRate.usd_per_week_pct;
    const { window } = await loadWidget(noRate);
    navLabel(window).prev.click(); // yesterday
    const tds = Array.from(window.document.querySelectorAll("td")).map((td) => td.textContent);
    check("no usd_per_week_pct -> past day falls back to share_of_range_pct (100.0%)",
      tds.some((t) => t.trim() === "100.0%"), tds.join(" | "));
  }

  // --- Older payload without days/weeks falls back to the static view. ---
  {
    const old = { generated: new Date().toISOString(), today: { total_usd: 5, sessions: [] }, week: { total_usd: 5, sessions: [] } };
    const { window } = await loadWidget(old);
    const buttons = Array.from(window.document.querySelectorAll("button"));
    check("no nav buttons rendered for a days/weeks-less payload", buttons.length === 0, `${buttons.length} buttons found`);
    const divs = Array.from(window.document.querySelectorAll("div")).map((d) => d.textContent);
    check("static Today/This week headers still render", divs.some((t) => t.startsWith("Today")) && divs.some((t) => t.startsWith("This week")));
  }

  console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
  console.error("ERROR:", e.stack || e);
  process.exit(1);
});
