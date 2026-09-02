// The grouped ("sessions") reading of the activity list: the model the probe
// sends, and the two lines a row of it shows.
//
// Its own file, and Foundation-only, for the same reason CallDetector.swift is
// one: everything here is rules rather than drawing, and rules that no test can
// reach are the ones that quietly go wrong. main.swift imports AppKit and ends
// in a running application, so nothing in it can be compiled into a test
// binary; this can, and tests/activity_session_tests.swift does exactly that.
import Foundation

// The same evidence as `activities`, folded into the periods it happened in.
// Not a second division of the day: the probe groups by the very spans the
// period list is built from, so a session's range is a period's range and the
// only thing new here is the tally of what that stretch was made of.
//
// `counted` is false for a run of evidence in minutes no period covers -- real
// events in time the day total deliberately left out. They keep a row because
// the raw list shows them, and a grouped view holding fewer events than the
// list it toggles with would be a different account of the day rather than the
// same one, gathered up.
struct ActSession {
    var start = 0
    var end = 0
    var len = 0
    var what = ""
    var counted = true
    var current = false
    var n = 0
    var kinds: [(String, Int)] = []
}

func human(_ m: Int) -> String {
    m >= 60 ? "\(m / 60)h \(m % 60)m" : "\(m)m"
}

// Periods carry minute-of-day integers, not wall-clock strings -- the probe
// publishes them that way so the widget can do arithmetic on them too.
func hhmm(_ m: Int) -> String {
    String(format: "%02d:%02d", m / 60, m % 60)
}

// How much of the second line fits before the widget would rather truncate
// than push the row wider than the menu. Enforced here rather than left to the
// label so the string a test reads is the string the menu draws.
let SESSION_WHAT_CHARS = 52

// The two lines a session row shows, kept out of the view so a test can read
// them and so the menu key can be built from exactly the strings on screen.
//
// Clock times rather than the raw list's ages: a session is a stretch with two
// ends, and "28m ago" for something that ran for half an hour names only the
// moment it started. The raw rows are point events and read better as ages;
// these are spans and read better as spans.
func sessionStrings(_ s: ActSession) -> (top: String, what: String) {
    let events = "\(s.n) event\(s.n == 1 ? "" : "s")"
    var top = s.counted
        ? "\(hhmm(s.start))–\(hhmm(s.end)) · \(human(s.len))   \(events)"
        // No length, because these minutes were not credited and printing a
        // span here would read as time that was.
        : "\(hhmm(s.start))–\(hhmm(s.end)) · not counted   \(events)"
    if s.current && s.counted { top += "   ·  now" }

    var what = s.kinds.map { "\($0.1) \($0.0)" }.joined(separator: " · ")
    // The period's own summary, when it has earned one, after the tally. The
    // tally says what the stretch was made of; this says what it was about,
    // and the two together are the whole reason to collapse the rows.
    if !s.what.isEmpty { what += "  —  \(s.what)" }
    if what.count > SESSION_WHAT_CHARS {
        what = String(what.prefix(SESSION_WHAT_CHARS - 1)) + "…"
    }
    return (top, what)
}
