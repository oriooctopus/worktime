// Drive a real IdleWatcher: take it past the threshold, press the button on
// the panel it raises, and read the claim it wrote.
//
// In-process for the same reason the countdown suite is: the button is the
// only way to say "I was here", and a button that silently does nothing looks
// exactly like no button at all. Idle is injected rather than read from the
// system, because a test cannot make the machine go untouched for two minutes.
import AppKit

@main
enum IdleWatcherTests {
    static var failures = 0

    static func check(_ ok: Bool, _ what: String) {
        print(ok ? "ok   \(what)" : "FAIL \(what)")
        if !ok { failures += 1 }
    }

    static func spin(_ seconds: Double) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
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

        // Below the threshold nobody is asked anything. The machine being
        // briefly quiet is the normal state of reading a line of code.
        let quiet = IdleWatcher(claimsPath: dir + "/quiet.jsonl")
        quiet.tick(idle: IDLE_PROMPT_SEC - 1)
        check(quiet.prompt == nil, "a short quiet stretch raises no panel")

        // Past it, the panel comes up and names when the silence started.
        let path = dir + "/asked.jsonl"
        let asked = IdleWatcher(claimsPath: path)
        asked.tick(idle: 200)
        check(asked.prompt != nil, "an absence past the threshold asks")
        check(asked.prompt?.messageText == "Not counting this time — \(IDLE_CLAIM_SEC)s",
              "the panel opens at the full window, got \(asked.prompt?.messageText ?? "nil")")

        // One question per absence: still away 5s later, still the same
        // silence, so it must not ask again.
        let before = asked.prompt
        asked.tick(idle: 205)
        check(asked.prompt === before, "a continuing absence is not asked twice")

        // Pressing the button claims it, and the claim covers the silence AND
        // the grace window after it -- otherwise the same reading session
        // would be asked about again two minutes later.
        asked.prompt?.keep.performClick(nil)
        let rows = claims(path)
        check(rows.count == 1, "the button writes exactly one claim (got \(rows.count))")
        if let r = rows.first {
            let from = r["from"] as? Int ?? -1
            let until = r["until"] as? Int ?? -1
            check(until - from >= IDLE_GRACE_SEC,
                  "the claim covers the silence and the grace window (\(until - from)s)")
        }
        check(asked.prompt == nil, "the panel goes away once answered")

        // Nobody answering writes nothing at all: exclusion is the default, so
        // silence needs no record. A file appearing here would mean the two
        // halves were both trying to describe the same absence.
        let ignoredPath = dir + "/ignored.jsonl"
        let ignored = IdleWatcher(claimsPath: ignoredPath)
        ignored.tick(idle: 200)
        check(ignored.prompt != nil, "the unanswered case still asks")
        spin(Double(IDLE_CLAIM_SEC) + 1.5)
        check(ignored.prompt == nil, "the panel withdraws when the window runs out")
        check(claims(ignoredPath).isEmpty,
              "an unanswered absence records nothing (got \(claims(ignoredPath).count))")

        // Back at the machine, then away again: a second, different absence
        // does get its own question.
        ignored.tick(idle: 1)
        ignored.tick(idle: 200)
        check(ignored.prompt != nil, "a later absence is asked about again")
        ignored.prompt?.close()

        try? FileManager.default.removeItem(atPath: dir)
        print(failures == 0 ? "all idle watcher checks passed"
                            : "\(failures) idle watcher check(s) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
