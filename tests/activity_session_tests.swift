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
             what: String = "", counted: Bool = true, current: Bool = false,
             n: Int = 4, kinds: [(String, Int)] = [("prompt", 4)],
             rows: [SessionRow] = []) -> ActSession {
    ActSession(start: start, end: end, lenSec: lenSec, what: what, counted: counted,
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

// -- where the events panel goes -------------------------------------------

// A screen 1600 wide, a menu 300 wide sitting a third of the way across it,
// and a row near the top of that menu.
let screen = NSRect(x: 0, y: 0, width: 1600, height: 1000)
let menuRect = NSRect(x: 500, y: 500, width: 300, height: 400)
let rowRect = NSRect(x: 500, y: 860, width: 300, height: 34)
let panel = NSSize(width: 460, height: 200)

// The gap is the entire reason this is a panel rather than a submenu, which
// AppKit places flush and offers no way to offset.
let left = popoverFrame(row: rowRect, menu: menuRect, size: panel, screen: screen)
check(left.maxX == menuRect.minX - POPOVER_GAP,
      "the panel sat \\(menuRect.minX - left.maxX)pt from the menu, not \\(POPOVER_GAP)")

// Top-aligned with the row that opened it: the panel is taller than the row,
// and centring left its first line -- the newest event, the one being asked
// about -- pointing at nothing.
check(left.maxY == rowRect.maxY,
      "the panel's top was \\(left.maxY), the row's \\(rowRect.maxY)")

// A menu near the left edge has no room on that side, so the panel goes to the
// other one rather than half off the display.
let cornered = popoverFrame(row: NSRect(x: 20, y: 860, width: 300, height: 34),
                            menu: NSRect(x: 20, y: 500, width: 300, height: 400),
                            size: panel, screen: screen)
check(cornered.minX == 320 + POPOVER_GAP,
      "a cornered panel opened at \\(cornered.minX) instead of beside the menu")

// Whatever side it lands on, it stays on the display.
for m in [menuRect, NSRect(x: 20, y: 500, width: 300, height: 400),
          NSRect(x: 1200, y: 500, width: 300, height: 400)] {
    let f = popoverFrame(row: NSRect(x: m.minX, y: 860, width: 300, height: 34),
                         menu: m, size: panel, screen: screen)
    check(f.minX >= screen.minX && f.maxX <= screen.maxX,
          "a panel ran off the side: \\(f)")
}

// A session long enough to be taller than the screen is pushed down to fit
// rather than starting level with its row and running off the top.
let tall = popoverFrame(row: NSRect(x: 500, y: 980, width: 300, height: 34),
                        menu: menuRect, size: NSSize(width: 460, height: 900),
                        screen: screen)
check(tall.minY >= screen.minY && tall.maxY <= screen.maxY,
      "a tall panel ran off the screen: \\(tall)")

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
