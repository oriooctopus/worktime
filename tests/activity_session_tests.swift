// Tests for the two lines a grouped session row draws.
//
// These strings are the whole grouped view -- the row is two labels and
// nothing else -- and they are the one part of the menu a screenshot is the
// only other way to check. Compiled and run by
// tests/test_activity_session_strings.py so it lands in the same `pytest
// tests` everything else does.

import Foundation

var failures: [String] = []

func check(_ cond: Bool, _ what: String) {
    if !cond { failures.append(what) }
}

func session(start: Int = 540, end: Int = 570, len: Int = 30,
             what: String = "", counted: Bool = true, current: Bool = false,
             n: Int = 4, kinds: [(String, Int)] = [("prompt", 4)],
             rows: [SessionRow] = []) -> ActSession {
    ActSession(start: start, end: end, len: len, what: what, counted: counted,
               current: current, n: n, kinds: kinds, rows: rows)
}

// Swift allows loose statements only in a file called main.swift, and this one
// is compiled alongside ActivitySession.swift, so the checks live in a @main
// entry point instead.
@main
enum ActivitySessionTests {
static func main() {

let plain = sessionStrings(session(what: "PR review", kinds: [("prompt", 3), ("slack", 1)]))
check(plain.top == "09:00–09:30 · 30m   4 events",
      "the top line was \"\(plain.top)\"")
check(plain.what == "3 prompt · 1 slack  —  PR review",
      "the second line was \"\(plain.what)\"")

// A session that is one event says "1 event", not "1 events" -- the row is
// three words long and a plural that does not agree is the first thing read.
let one = sessionStrings(session(n: 1, kinds: [("prompt", 1)]))
check(one.top.hasSuffix("1 event"), "a single event read as \"\(one.top)\"")

// The stretch in progress is marked, the same way the current period's row is
// set in semibold: without it the newest row and the one below it are two time
// ranges with nothing saying which one is now.
let live = sessionStrings(session(current: true))
check(live.top.hasSuffix("·  now"), "the current session read as \"\(live.top)\"")

// An uncounted run reports no length. Its minutes are precisely the ones the
// day total left out, so a span here would read as time that was credited.
let stray = sessionStrings(session(start: 240, end: 242, len: 0, counted: false,
                                   n: 2, kinds: [("approval", 2)]))
check(stray.top == "04:00–04:02 · not counted   2 events",
      "an uncounted session read as \"\(stray.top)\"")
check(!stray.top.contains("now"),
      "an uncounted session was marked as the current one")

// Hours once a session passes sixty minutes, matching the period rows.
let long = sessionStrings(session(start: 540, end: 660, len: 120))
check(long.top.contains("2h 0m"), "a two-hour session read as \"\(long.top)\"")

// The tally arrives biggest-first from the probe and must stay in that order:
// what falls off the end of a truncated line should be the smallest
// contributor, not whichever kind happened to sort first.
let many = sessionStrings(session(
    n: 30, kinds: [("prompt", 20), ("browsing", 7), ("focus", 3)]))
check(many.what.hasPrefix("20 prompt · 7 browsing · 3 focus"),
      "the tally read as \"\(many.what)\"")

// Truncation is done here rather than left to the label, so what a test reads
// is what the menu draws.
let wordy = sessionStrings(session(
    what: String(repeating: "summary ", count: 20), kinds: [("prompt", 4)]))
check(wordy.what.count == SESSION_WHAT_CHARS,
      "a long line came out \(wordy.what.count) characters")
check(wordy.what.hasSuffix("…"), "a truncated line did not end in an ellipsis")

// No test for an empty tally: every session is built from at least one row, so
// `kinds` is never empty, and a branch here for the case that cannot happen
// would only make a broken payload draw as though it were fine.

// -- the hover ------------------------------------------------------------

// The tooltip opens with the row's own top line, so what is being read is
// anchored to the session it came from rather than floating free of it.
let tip = sessionTip(session(
    n: 3, kinds: [("prompt", 2), ("approval", 1)],
    rows: [SessionRow(t: "09:03", kind: "prompt", what: "run the tests", n: 1),
           SessionRow(t: "09:01", kind: "approval", what: "approved Bash", n: 1),
           SessionRow(t: "09:00", kind: "prompt", what: "hello", n: 1)]))
let tipLines = tip.split(separator: "\n", omittingEmptySubsequences: false)
check(String(tipLines[0]) == sessionStrings(session(n: 3, kinds: [("prompt", 2), ("approval", 1)])).top,
      "the hover did not open with the row's own line: \"\(tipLines[0])\"")
check(tipLines.count == 4, "the hover drew \(tipLines.count) lines for 3 events")
check(tipLines[1] == "09:03  prompt · run the tests",
      "an event line read as \"\(tipLines[1])\"")

// A collapsed row carries its count, and an uncollapsed one does not -- the
// same rule the raw list follows, since this is that list scoped to one
// session.
let counted = sessionTip(session(n: 5, rows: [
    SessionRow(t: "09:03", kind: "browsing", what: "the same PR", n: 4),
    SessionRow(t: "09:01", kind: "prompt", what: "go", n: 1)]))
check(counted.contains("09:03  browsing · the same PR  ×4"),
      "a collapsed row lost its count: \"\(counted)\"")
check(counted.contains("09:01  prompt · go\n") || counted.hasSuffix("09:01  prompt · go"),
      "a single event was given a ×1: \"\(counted)\"")

// What the probe's cap left out is said out loud. Silently showing twelve of a
// hundred would contradict the event count on the row being hovered, which is
// the number this is read against.
let capped = sessionTip(session(n: 30, rows: [
    SessionRow(t: "09:03", kind: "prompt", what: "one", n: 1)]))
check(capped.hasSuffix("… and 29 more"),
      "a capped hover did not say what it left out: \"\(capped)\"")

// A session whose rows all fit says nothing about more, rather than "and 0
// more".
let whole = sessionTip(session(n: 1, rows: [
    SessionRow(t: "09:03", kind: "prompt", what: "one", n: 1)]))
check(!whole.contains("more"), "a complete hover claimed there was more: \"\(whole)\"")

// A pasted stack trace as a prompt must not set the width of every other line.
let wide = sessionTip(session(n: 1, rows: [
    SessionRow(t: "09:03", kind: "prompt",
               what: String(repeating: "x", count: 300), n: 1)]))
for line in wide.split(separator: "\n") {
    check(line.count <= SESSION_TIP_CHARS + 20,
          "a hover line ran to \(line.count) characters")
}

if failures.isEmpty {
    print("activity session strings: all checks passed")
} else {
    for f in failures { print("FAIL: \(f)") }
    exit(1)
}

}  // static func main
}  // enum ActivitySessionTests
