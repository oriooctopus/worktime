// Drive a real IdleWatcher past the threshold and check what it does with an
// absence now that the prompt is off: notice it once, write nothing, and put
// nothing on screen.
//
// Nothing here raises a panel, deliberately. The previous version of this file
// did -- it pressed the real button, which meant a real window appeared on
// whatever the machine was doing every time the suite ran, including in the
// middle of somebody's work. With IDLE_PROMPT_VISIBLE off that panel is not
// what ships, so testing it was buying an interruption for coverage of a path
// no user reaches. If the prompt is ever turned back on, the panel tests come
// back with it -- and behind a switch that keeps them off a shared machine.
//
// Idle is injected rather than read from the system, because a test cannot
// make the machine go untouched for two minutes.
import AppKit

@main
enum IdleWatcherTests {
    static var failures = 0

    static func check(_ ok: Bool, _ what: String) {
        print(ok ? "ok   \(what)" : "FAIL \(what)")
        if !ok { failures += 1 }
    }

    static func claims(_ path: String) -> [[String: Any]] {
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap {
            (try? JSONSerialization.jsonObject(with: Data($0.utf8))) as? [String: Any]
        }
    }

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        let dir = NSTemporaryDirectory() + "idle-watcher-tests-\(getpid())"
        try? FileManager.default.createDirectory(atPath: dir,
                                                 withIntermediateDirectories: true)

        // What ships: the prompt is off, so however long the machine is left
        // alone, nothing is put in front of whoever comes back to it.
        check(!IDLE_PROMPT_VISIBLE, "the idle prompt ships off")

        let path = dir + "/shipped.jsonl"
        let w = IdleWatcher(claimsPath: path)

        // Below the threshold nothing happens at all. The machine being
        // briefly quiet is the normal state of reading a line of code.
        w.tick(idle: IDLE_PROMPT_SEC - 1)
        check(w.noticed == 0, "a short quiet stretch is not an absence")
        check(w.prompt == nil, "a short quiet stretch raises no panel")

        // Past it the absence is noticed -- that half is kept on purpose, it
        // is the record the timeline row is built from -- but still silent.
        w.tick(idle: 200)
        check(w.noticed == 1, "an absence past the threshold is noticed")
        check(w.prompt == nil, "an absence past the threshold raises no panel")

        // One notice per absence: still away 5s later, still the same silence.
        w.tick(idle: 205)
        check(w.noticed == 1, "a continuing absence is not noticed twice")

        // Back at the machine, then away again: a second, different absence is
        // its own.
        w.tick(idle: 1)
        w.tick(idle: 200)
        check(w.noticed == 2, "a later absence is noticed again")

        // The session end is the other half, and it runs off a shorter clock
        // than the notice above: 45s, not two minutes.
        let e = IdleWatcher(claimsPath: dir + "/ends.jsonl")
        var firedWith: [Double] = []
        e.onIdleEnd = { firedWith.append($0) }
        e.tick(idle: IDLE_END_SEC - 1)
        check(e.idleEnds == 0, "under 45s of no input does not end the session")
        e.tick(idle: 50)
        check(firedWith == [50], "45s of no input ends the session, carrying the idle seconds")
        e.tick(idle: 55)
        check(e.idleEnds == 1, "a continuing absence ends the session once")
        e.tick(idle: 1)
        e.tick(idle: 60)
        check(e.idleEnds == 2, "input then another absence ends it again")

        // A call is somebody there with no input. Ending at the last keystroke
        // would cut the whole call out, so the end is skipped -- and consumed:
        // the call finishing while the machine is still untouched must not
        // end the session retroactively.
        let m = IdleWatcher(claimsPath: dir + "/meeting.jsonl")
        m.onIdleEnd = { _ in }
        m.tick(idle: 50, inMeeting: true)
        m.tick(idle: 400, inMeeting: false)
        check(m.idleEnds == 0, "no session end during a meeting, nor after it for the same absence")
        m.tick(idle: 1)
        m.tick(idle: 50)
        check(m.idleEnds == 1, "the next absence after the meeting ends it normally")

        // The panel that offers to take the end back holds through the absence
        // and is released by the first input afterwards -- once, and only for an
        // absence that reached the end threshold.
        let r = IdleWatcher(claimsPath: dir + "/resume.jsonl")
        var resumed = 0
        r.onInputResumed = { resumed += 1 }
        r.tick(idle: IDLE_END_SEC - 1)
        check(resumed == 0, "input without a prior absence is not a resume")
        r.tick(idle: 50)
        r.tick(idle: 80)
        check(resumed == 0, "no resume while the absence continues")
        r.tick(idle: 1)
        check(resumed == 1, "first input after the absence is a resume")
        r.tick(idle: 2)
        check(resumed == 1, "continued input is not a second resume")

        // The idle-end panel itself: holds with its message until released,
        // then counts down, and the button reports the cancel.
        var closed = 0, kept = 0
        let p = CountdownPanel(
            meeting: "Session ended", seconds: 2,
            buttonTitle: "Keep tracking through now",
            messageFor: { "closing in \($0)s" },
            holdMessage: "Session ended at 16:39", present: false,
            onExpire: { closed += 1 }, onCancel: { kept += 1 })
        p.paused = true
        check(p.messageText == "Session ended at 16:39", "held panel shows its hold message")
        RunLoop.main.run(until: Date().addingTimeInterval(3))
        check(closed == 0, "a held panel does not expire (closed=\(closed))")
        p.paused = false
        check(p.messageText == "closing in 2s", "released panel counts down, got \(p.messageText)")
        RunLoop.main.run(until: Date().addingTimeInterval(3))
        check(closed == 1 && kept == 0, "released panel expires once (closed=\(closed))")
        let q = CountdownPanel(
            meeting: "Session ended", seconds: 2, holdMessage: "held", present: false,
            onExpire: { closed += 1 }, onCancel: { kept += 1 })
        q.paused = true
        q.keep.performClick(nil)
        check(kept == 1 && closed == 1, "the button works while held (kept=\(kept))")

        // Nothing is recorded. A claim only exists to protect time from being
        // cut, and with the probe's IDLE_SUBTRACTS off there is no cut to
        // protect anything from -- a file here would be a record of an answer
        // to a question nobody was asked.
        check(claims(path).isEmpty,
              "an absence records nothing (got \(claims(path).count))")

        try? FileManager.default.removeItem(atPath: dir)
        print(failures == 0 ? "all idle watcher checks passed"
                            : "\(failures) idle watcher check(s) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
