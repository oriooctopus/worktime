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
