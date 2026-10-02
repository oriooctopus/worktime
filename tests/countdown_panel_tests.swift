// Drive a real CountdownPanel: press its button, let its clock run out, and
// withdraw it mid-count, checking that exactly one of the two outcomes happens
// each time.
//
// This is in-process rather than a synthetic click because posting a mouse
// event to another app needs Accessibility, which a test runner does not have;
// a click posted without it is silently dropped, which looks exactly like a
// button that does not work. The press here goes through the button's real
// target and action, so a broken selector or a detached target still fails.
//
// Needs a window server, so it is skipped off a Mac by the wrapper that runs it.
import AppKit

@main
enum CountdownPanelTests {
    static var failures = 0

    static func check(_ ok: Bool, _ what: String) {
        print(ok ? "ok   \(what)" : "FAIL \(what)")
        if !ok { failures += 1 }
    }

    /// Spin the run loop for real time, so the panel's own 1s timer fires the
    /// way it does in the app. Sleeping would not: the timer needs the loop.
    static func spin(_ seconds: Double) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
    }

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        // Every panel here is built with `present: false`. They used to be
        // real windows, which meant running the test suite dropped four
        // countdowns on screen over whatever the machine was doing -- and they
        // look exactly like the app prompting for real, so the only way to
        // tell was to know the suite happened to be running. Nothing below
        // needs the window: the button still clicks and the timer still fires
        // off screen, so the coverage is unchanged and the interruption is
        // gone. A panel built the normal way still shows -- see the default.

        // Pressing the button cancels, and cancelling is not ending.
        var expired = 0, cancelled = 0
        var panel: CountdownPanel? = CountdownPanel(
            meeting: "Design review", seconds: 3, present: false,
            onExpire: { expired += 1 }, onCancel: { cancelled += 1 })
        check(panel!.messageText == "Ended — stopping tracking in 3s",
              "opens at the full countdown, got \(panel!.messageText)")
        panel!.keep.performClick(nil)
        check(cancelled == 1, "the button cancels (cancelled=\(cancelled))")
        check(expired == 0, "the button does not end the meeting (expired=\(expired))")
        // The clock has to be dead, not merely overtaken: a cancel that only
        // hid the panel would still stop tracking a few seconds later.
        spin(5)
        check(expired == 0, "no ending arrives after a cancel (expired=\(expired))")
        check(cancelled == 1, "cancel fires once (cancelled=\(cancelled))")
        panel = nil

        // Left alone, it counts down and ends the meeting once.
        expired = 0; cancelled = 0
        panel = CountdownPanel(meeting: "Standup", seconds: 2, present: false,
                               onExpire: { expired += 1 }, onCancel: { cancelled += 1 })
        spin(1.4)
        check(panel!.messageText == "Ended — stopping tracking in 1s",
              "the number falls while it waits, got \(panel!.messageText)")
        spin(1.2)
        check(expired == 1, "expiry ends the meeting (expired=\(expired))")
        check(cancelled == 0, "expiry is not a cancel (cancelled=\(cancelled))")
        spin(3)
        check(expired == 1, "expiry fires once, not every second (expired=\(expired))")
        panel = nil

        // Withdrawn because the call came back: neither outcome.
        expired = 0; cancelled = 0
        panel = CountdownPanel(meeting: "Retro", seconds: 3, present: false,
                               onExpire: { expired += 1 }, onCancel: { cancelled += 1 })
        spin(1.2)
        panel!.close()
        spin(4)
        check(expired == 0 && cancelled == 0,
              "a withdrawn countdown does nothing (expired=\(expired) cancelled=\(cancelled))")
        panel = nil

        // Held while the mic is back: no expiry however long it waits, and
        // the count carries on from where it stopped once released.
        expired = 0; cancelled = 0
        panel = CountdownPanel(meeting: "Sync", seconds: 2, present: false,
                               onExpire: { expired += 1 }, onCancel: { cancelled += 1 })
        panel!.paused = true
        check(panel!.messageText == "Mic is back — holding",
              "a held countdown says so, got \(panel!.messageText)")
        spin(3.2)
        check(expired == 0, "a held countdown does not expire (expired=\(expired))")
        panel!.paused = false
        check(panel!.messageText == "Ended — stopping tracking in 2s",
              "released, it resumes at the same number, got \(panel!.messageText)")
        spin(2.4)
        check(expired == 1, "released, it still ends the meeting (expired=\(expired))")
        panel = nil

        // A meeting with no name still names something.
        let unnamed = CountdownPanel(meeting: "", seconds: 3, present: false,
                                     onExpire: {}, onCancel: {})
        check(unnamed.messageText.hasPrefix("Ended"), "an unnamed meeting still opens")
        unnamed.close()

        // presentDelay holds the window back but not the clock: a fast second
        // ⌘E closes the panel before it ever shows, and the countdown still
        // ends the session on time when nobody presses again.
        expired = 0; cancelled = 0
        var delayed: CountdownPanel? = CountdownPanel(
            meeting: "End session", seconds: 3, present: false, presentDelay: 1.0,
            onExpire: { expired += 1 }, onCancel: { cancelled += 1 })
        check(!delayed!.revealed, "a delayed panel is not shown at once")
        spin(0.5)
        check(!delayed!.revealed, "a delayed panel is still hidden at 0.5s")
        spin(0.7)
        check(delayed!.revealed, "a delayed panel is shown once its delay passes")
        spin(2.0)
        check(expired == 1, "a delayed panel still expires on the original clock (expired=\(expired))")
        delayed = nil

        let early = CountdownPanel(
            meeting: "End session", seconds: 3, present: false, presentDelay: 1.0,
            onExpire: {}, onCancel: {})
        early.close()
        spin(1.4)
        check(!early.revealed, "a panel closed inside its delay is never shown")

        // ⌘E: a second press within a second is the double press (last entry);
        // a slower one is "end now".
        let t0 = Date()
        check(isEndDoublePress(openedAt: t0, now: t0.addingTimeInterval(0.2)),
              "second ⌘E at 0.2s is a double press")
        check(isEndDoublePress(openedAt: t0, now: t0.addingTimeInterval(1.0)),
              "second ⌘E at exactly 1s is still a double press")
        check(!isEndDoublePress(openedAt: t0, now: t0.addingTimeInterval(1.5)),
              "second ⌘E at 1.5s is end-now, not a double press")

        print(failures == 0 ? "all countdown panel checks passed"
                            : "\(failures) countdown panel check(s) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
