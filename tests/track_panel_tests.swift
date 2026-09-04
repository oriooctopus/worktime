// Drive a real TrackPanel: type into its field, move its picker, press its
// button, and check what the callback is handed.
//
// In-process for the same reason the countdown suite is: a synthetic click
// posted to another app needs Accessibility, which a test runner does not
// have, and one posted without it is silently dropped -- indistinguishable
// from a button that does not work. The press here goes through the button's
// real target and action, so a broken selector still fails.
//
// Every panel is built with `present: false`. This one matters more than the
// countdown's did: TrackPanel is the one surface in this app that deliberately
// takes the keyboard, so a suite that showed it would not merely put windows
// on screen, it would swallow whatever was being typed at the time.
//
// Needs a window server, so it is skipped off a Mac by the wrapper that runs it.
import AppKit

@main
enum TrackPanelTests {
    static var failures = 0

    static func check(_ ok: Bool, _ what: String) {
        print(ok ? "ok   \(what)" : "FAIL \(what)")
        if !ok { failures += 1 }
    }

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        // A fresh store per run: the panel remembers the last count and rule,
        // and a suite that inherited them from the machine it runs on would
        // pass or fail depending on what the person last tracked.
        let suite = "worktime.track.tests"
        UserDefaults.standard.removePersistentDomain(forName: suite)
        UserDefaults.standard.removeObject(forKey: TrackPanel.minutesKey)
        UserDefaults.standard.removeObject(forKey: TrackPanel.modeKey)

        // The two lists are read in parallel -- the picker reports an index and
        // the mode is looked up by it -- so a row added to one and not the
        // other would name a rule that runs as its neighbour.
        check(TrackPanel.modes.count == TrackPanel.modeTitles.count,
              "every offered row names a rule "
              + "(\(TrackPanel.modes.count) vs \(TrackPanel.modeTitles.count))")
        check(TrackPanel.modes.first == "clip",
              "the rule that cannot overstate the day is the default, "
              + "got \(TrackPanel.modes.first ?? "none")")

        // Opens on a usable number and hands it over as typed.
        var got: (Int, String)?
        var panel: TrackPanel? = TrackPanel(present: false) { got = ($0, $1) }
        check(panel!.minutes == TrackPanel.defaultMinutes,
              "opens on a count that can be banked as-is, "
              + "got \(String(describing: panel!.minutes))")
        panel!.minutesField.stringValue = "12"
        panel!.trackButton.performClick(nil)
        check(got?.0 == 12, "banks the typed count, got \(String(describing: got?.0))")
        check(got?.1 == "clip", "banks the picked rule, got \(String(describing: got?.1))")
        panel = nil

        // The picker's second row is the second rule, not merely a second row.
        got = nil
        panel = TrackPanel(present: false) { got = ($0, $1) }
        panel!.modePicker.selectItem(at: 1)
        panel!.minutesField.stringValue = "7"
        panel!.trackButton.performClick(nil)
        check(got?.1 == "split", "the second row is split, got \(String(describing: got?.1))")
        panel = nil

        // Both are remembered, because the next call is usually the same
        // length as the last one and retyping it every time is how a feature
        // ends up unused.
        panel = TrackPanel(present: false) { _, _ in }
        check(panel!.minutes == 7, "remembers the count, got \(String(describing: panel!.minutes))")
        check(panel!.mode == "split", "remembers the rule, got \(panel!.mode)")
        panel = nil

        // Nothing that is not a positive number of minutes may reach the
        // probe. A panel that fell back to a default here would bank minutes
        // nobody typed, and they would land in the day either way -- only one
        // of those outcomes can be noticed.
        for bad in ["", "  ", "abc", "0", "-3", "2.5"] {
            got = nil
            let p = TrackPanel(present: false) { got = ($0, $1) }
            p.minutesField.stringValue = bad
            p.trackButton.performClick(nil)
            check(got == nil, "refuses \"\(bad)\" (banked \(String(describing: got)))")
            p.close()
        }

        // Cancelling does nothing at all, which is the only thing it may do.
        got = nil
        panel = TrackPanel(present: false) { got = ($0, $1) }
        panel!.minutesField.stringValue = "9"
        panel!.cancelTapped()
        check(got == nil, "cancel banks nothing (got \(String(describing: got)))")
        panel = nil

        // And a cancelled count is not remembered: it was never a claim.
        panel = TrackPanel(present: false) { _, _ in }
        check(panel!.minutes == 7,
              "a cancelled count is not remembered, got \(String(describing: panel!.minutes))")
        panel!.close()
        panel = nil

        // ...but it does have to SAY it closed, and it has to stop reporting
        // itself as on screen. This is the bug that killed the shortcut on the
        // day it shipped: the caller kept the cancelled panel and asked whether
        // it held one rather than whether one was visible, so the first cancel
        // was the last time the panel ever appeared. Nothing about that is
        // observable from the outside -- the key still fires, the app still
        // activates, no error is logged anywhere -- so it is pinned here.
        var closed = 0
        let cancelled = TrackPanel(present: false, onClose: { closed += 1 }) { _, _ in }
        check(cancelled.isOnScreen == false, "a panel built unpresented is not on screen")
        cancelled.cancelTapped()
        check(closed == 1, "cancel reports the close (closed=\(closed))")
        check(cancelled.isOnScreen == false,
              "a cancelled panel does not claim to be on screen")

        // Tracking is not a close: the caller drops the panel on the track
        // callback, and a second onClose there would be a second attempt to
        // drop something already gone.
        closed = 0
        let banked = TrackPanel(present: false, onClose: { closed += 1 }) { _, _ in }
        banked.minutesField.stringValue = "4"
        banked.trackButton.performClick(nil)
        check(closed == 0, "tracking is not reported as a close (closed=\(closed))")
        banked.close()

        UserDefaults.standard.removeObject(forKey: TrackPanel.minutesKey)
        UserDefaults.standard.removeObject(forKey: TrackPanel.modeKey)

        print(failures == 0 ? "all track panel checks passed"
                            : "\(failures) track panel check(s) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
