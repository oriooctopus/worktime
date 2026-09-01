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

        // Pressing the button cancels, and cancelling is not ending.
        var expired = 0, cancelled = 0
        var panel: CountdownPanel? = CountdownPanel(
            meeting: "Design review", seconds: 3,
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
        panel = CountdownPanel(meeting: "Standup", seconds: 2,
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
        panel = CountdownPanel(meeting: "Retro", seconds: 3,
                               onExpire: { expired += 1 }, onCancel: { cancelled += 1 })
        spin(1.2)
        panel!.close()
        spin(4)
        check(expired == 0 && cancelled == 0,
              "a withdrawn countdown does nothing (expired=\(expired) cancelled=\(cancelled))")
        panel = nil

        // A meeting with no name still names something.
        let unnamed = CountdownPanel(meeting: "", seconds: 3, onExpire: {}, onCancel: {})
        check(unnamed.messageText.hasPrefix("Ended"), "an unnamed meeting still opens")
        unnamed.close()

        print(failures == 0 ? "all countdown panel checks passed"
                            : "\(failures) countdown panel check(s) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
