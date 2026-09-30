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

func session(start: Int = 540, end: Int = 570, lenSec: Int = 1800,
             deadSec: Int = 0,
             what: String = "", counted: Bool = true, current: Bool = false,
             n: Int = 4, kinds: [(String, Int)] = [("prompt", 4)],
             rows: [SessionRow] = [], special: Bool = false,
             convert: SessionConvert? = nil) -> ActSession {
    ActSession(start: start, end: end, lenSec: lenSec, deadSec: deadSec,
               what: what, counted: counted,
               current: current, special: special, convert: convert,
               n: n, kinds: kinds, rows: rows)
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
let stray = sessionStrings(session(start: 240, end: 242, lenSec: 0, counted: false,
                                   n: 2, kinds: [("approval", 2)]))
check(stray.top == "04:00–04:02 · not counted   2 events",
      "an uncounted session read as \"\(stray.top)\"")
check(!stray.top.contains("now"),
      "an uncounted session was marked as the current one")

// Hours once a session passes sixty minutes, matching the period rows.
let long = sessionStrings(session(start: 540, end: 660, lenSec: 7200))
check(long.top.contains("2h 0m"), "a two-hour session read as \"\(long.top)\"")

// A glance is under a minute and says so, rather than reading "0m".
let glance = sessionStrings(session(start: 540, end: 540, lenSec: 30))
check(glance.top.contains("· 30s "), "a thirty-second session read as \"\(glance.top)\"")

// Time lost to the other machine is reported as the LOSS itself, not as a
// total the reader has to subtract from. "30m / 26m" was the first attempt
// and it buried the one quantity the row exists to report.
let cost = sessionStrings(session(deadSec: 300))
check(cost.top == "09:00–09:30 · 30m / 5m   4 events",
      "a session with dead time read as \"\(cost.top)\"")

// A sub-minute loss survives. Reporting a total meant "30m / 30m" for a 30s
// cost -- both sides rounded to the same label and the guard against that
// then hid the fraction entirely, so the loss vanished twice over.
let small = sessionStrings(session(deadSec: 30))
check(small.top.contains("30m / 30s"),
      "a thirty-second loss read as \"\(small.top)\"")

// No tail at all when nothing was lost: a "/ 0s" on every ordinary row would
// be noise standing in for the normal case.
check(sessionDeadTail(session()) == nil,
      "a session with no dead time still drew a tail")

// The colored range main.swift applies has to be findable in the string
// sessionStrings actually drew. The two were computed separately once, drifted,
// and a range that matches nothing silently colors nothing.
for d in [30, 60, 300, 3600] {
    let s = session(deadSec: d)
    guard let tail = sessionDeadTail(s) else {
        check(false, "no tail for a \(d)s loss"); continue
    }
    check(sessionStrings(s).top.range(of: tail) != nil,
          "the tail \"\(tail)\" is not present in the drawn top line")
    check(tail == " / \(human(d))",
          "the tail for a \(d)s loss was \"\(tail)\", not the loss itself")
}

// An uncounted run reports no loss either: its minutes were never credited,
// so there is no fraction of them to explain.
check(sessionDeadTail(session(deadSec: 300, counted: false)) == nil,
      "an uncounted session drew a dead-time tail")

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

// -- the submenu ----------------------------------------------------------

// What the probe's cap left out is said out loud. Silently showing twenty
// events of a hundred would contradict the count on the row the submenu hangs
// off, which is the number it is read against.
let capped = session(n: 30, rows: [
    SessionRow(t: "09:03", kind: "prompt", what: "one", n: 1)])
check(sessionMoreLine(capped) == "\u{2026} and 29 more",
      "a capped submenu said \"\(sessionMoreLine(capped) ?? "nothing")\"")

// A collapsed row stands for several events, so what is missing is counted in
// events too -- counting rows would report a session of eight as a session of
// two, and the row above says eight.
let collapsed = session(n: 8, rows: [
    SessionRow(t: "09:03", kind: "browsing", what: "the same PR", n: 4),
    SessionRow(t: "09:01", kind: "prompt", what: "go", n: 2)])
check(sessionMoreLine(collapsed) == "\u{2026} and 2 more",
      "a collapsed row was counted as one: \"\(sessionMoreLine(collapsed) ?? "nothing")\"")

// A session whose events all travelled says nothing, rather than "and 0 more".
let whole = session(n: 1, rows: [
    SessionRow(t: "09:03", kind: "prompt", what: "one", n: 1)])
check(sessionMoreLine(whole) == nil,
      "a complete submenu claimed there was more: \"\(sessionMoreLine(whole) ?? "")\"")

// -- special sessions and conversion ---------------------------------------

// A special session says so on its row, after the count and before "now".
let sp = sessionStrings(session(start: 600, end: 625, lenSec: 1500, current: true,
                                n: 2, special: true))
check(sp.top == "10:00–10:25 · 25m   2 events   ·  special   ·  now",
      "a special row read \"\(sp.top)\"")
check(!sessionStrings(session()).top.contains("special"),
      "a main session claimed to be special")

// The row words name the bucket the session is going to.
check(SessionConvert(to: "special", from: 540, until: 570).title == "Convert to Special Time",
      "the to-special row was titled wrong")
check(SessionConvert(to: "main", from: 540, until: 570).title == "Convert to Main Time",
      "the to-main row was titled wrong")

// What goes back to the probe: minutes as clock times, "now" for a running one.
check(SessionConvert(to: "special", from: 510, until: nil).probeArgs == ["special", "08:30", "now"],
      "a running session's args were \(SessionConvert(to: "special", from: 510, until: nil).probeArgs)")
check(SessionConvert(to: "main", from: 600, until: 625).probeArgs == ["main", "10:00", "10:25"],
      "a finished session's args were wrong")

// The key covers the conversion: two sessions identical on screen whose
// conversions differ would otherwise leave the wrong range behind the row.
check(sessionKey(session(convert: SessionConvert(to: "special", from: 510, until: nil)))
        != sessionKey(session(convert: SessionConvert(to: "special", from: 540, until: nil))),
      "the menu key ignored where a conversion starts")
check(sessionKey(session(special: true)) != sessionKey(session()),
      "the menu key could not tell special from main")

// -- the menu key ---------------------------------------------------------

// The key has to cover what the submenu draws, not just the two visible lines:
// these two sessions are identical on screen until you hover one, and a key
// that could not tell them apart would leave the wrong events behind the
// arrow.
let a = session(n: 1, rows: [SessionRow(t: "09:03", kind: "prompt", what: "one", n: 1)])
let b = session(n: 1, rows: [SessionRow(t: "09:03", kind: "prompt", what: "two", n: 1)])
check(sessionStrings(a).top == sessionStrings(b).top,
      "the fixtures were meant to be identical on the row itself")
check(sessionKey(a) != sessionKey(b),
      "the menu key could not tell two different submenus apart")

// And it covers the row itself, so a session that changed length still
// rebuilds.
check(sessionKey(a) != sessionKey(session(
    lenSec: 2700, n: 1,
    rows: [SessionRow(t: "09:03", kind: "prompt", what: "one", n: 1)])),
      "the menu key ignored the session's own line")

// The same session twice is the same key -- the point of it is that a poll
// which changed nothing costs no rebuild and cannot flicker an open menu.
check(sessionKey(a) == sessionKey(session(
    n: 1, rows: [SessionRow(t: "09:03", kind: "prompt", what: "one", n: 1)])),
      "an unchanged session produced a different key")

if failures.isEmpty {
    print("activity session strings: all checks passed")
} else {
    for f in failures { print("FAIL: \(f)") }
    exit(1)
}

}  // static func main
}  // enum ActivitySessionTests
