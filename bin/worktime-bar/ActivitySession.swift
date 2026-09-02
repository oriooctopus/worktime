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
    // The session's own events, oldest-last like the raw list, capped by the
    // probe. `n` above counts every event the session held; these are as many
    // of them as a tooltip can show, and what the two disagree by is what the
    // hover says it left out.
    var rows: [SessionRow] = []
}

// One event inside a session. The same shape as the raw list's rows and for
// the same reason -- the hover is the raw list, scoped to one session, so it
// carries the same four fields and shows them the same way round.
struct SessionRow {
    var t = ""
    var kind = ""
    var what = ""
    var n = 1
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

// Wider than a menu row, because a tooltip is drawn over the menu rather than
// inside it and has the screen to work with -- but still capped, since a
// pasted stack trace as a prompt would otherwise set the width of every line.
let SESSION_TIP_CHARS = 72

// What hovering a session row shows: the events it collapsed, in the order and
// the shape the raw list shows them.
//
// The grouped view answers "what was this stretch" by summing its rows away,
// which is the point of it and also the one thing it costs -- the evidence the
// dot's verdict rests on stops being readable the moment it is grouped.
// Toggling back to the raw list is not that answer either: it shows the newest
// ten events of the whole day, not this session's. The hover is the only place
// a single session's rows can be read, which is why it exists.
//
// Clock times here, not ages: the row above already places the session in the
// day, so what these are being read against is each other and the range in the
// header line.
func sessionTip(_ s: ActSession) -> String {
    var lines = [sessionStrings(s).top]
    for r in s.rows {
        var what = r.what
        if what.count > SESSION_TIP_CHARS {
            what = String(what.prefix(SESSION_TIP_CHARS - 1)) + "…"
        }
        // Same "×N" as the raw list, and absent for the ordinary single event
        // for the same reason: a "×1" on every other line is noise standing in
        // for the normal case.
        lines.append("\(r.t)  \(r.kind) · \(what)" + (r.n > 1 ? "  ×\(r.n)" : ""))
    }
    // The cap the probe applied, said out loud. A tooltip that silently showed
    // twelve of a hundred events would be a smaller day than the count on the
    // row right above it, which is the one number it is being read against.
    let shown = s.rows.reduce(0) { $0 + $1.n }
    if s.n > shown { lines.append("… and \(s.n - shown) more") }
    return lines.joined(separator: "\n")
}
