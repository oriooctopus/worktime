// Source parsing, the profile store, and the panel's buttons, driven for real.
import AppKit

@main
enum WorkSourceTests {
    static var failures = 0

    static func check(_ ok: Bool, _ what: String) {
        print(ok ? "ok   \(what)" : "FAIL \(what)")
        if !ok { failures += 1 }
    }

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        let port = localSource(url: "http://127.0.0.1:9110/")
        check(port == LocalSource(kind: .port, value: "9110"), "127.0.0.1 port parsed, got \(String(describing: port))")
        check(localSource(url: "http://localhost:8118/x?y=1")?.value == "8118", "localhost with path")
        check(localSource(url: "https://example.com:9110/") == nil, "remote host with a port is not local")
        check(localSource(url: "http://localhost.evil.com:9110/") == nil, "lookalike host is not local")
        let dir = localSource(url: "file:///Users/o/coding/ruby-memory-eval/viewer.html")
        check(dir == LocalSource(kind: .directory, value: "file:///Users/o/coding/ruby-memory-eval/"),
              "file maps to its directory, got \(String(describing: dir))")

        let path = NSTemporaryDirectory() + "worksource-\(getpid()).json"
        try! #"{"_comment": "keep me", "work_url_keywords": ["rubrik"]}"#
            .write(toFile: path, atomically: true, encoding: .utf8)
        let store = WorkSourceStore(path: path)

        check(store.isKnown(LocalSource(kind: .port, value: "3000")), "default port is known")
        check(!store.isKnown(port!), "9110 unknown at first")
        store.track(port!)
        check(store.isKnown(port!), "tracked port is known")
        check(store.isKnown(LocalSource(kind: .port, value: "3005")), "defaults kept when a port is added")

        check(!store.isKnown(dir!), "directory unknown at first")
        store.track(dir!)
        check(store.isKnown(dir!), "tracked directory is known")
        check(store.isKnown(LocalSource(kind: .directory, value: dir!.value + "sub/")),
              "subdirectory of a tracked one is known")
        check(!store.isKnown(LocalSource(kind: .directory, value: "file:///Users/o/coding/ruby-memory-eval-old/")),
              "sibling sharing a prefix is not known")

        let other = LocalSource(kind: .port, value: "8118")
        store.dismiss(other)
        check(store.isKnown(other), "dismissed port is known")

        let saved = try! JSONSerialization.jsonObject(
            with: FileManager.default.contents(atPath: path)!) as! [String: Any]
        check(saved["_comment"] as? String == "keep me", "unrelated keys survive")
        check(saved["work_localhost_ports"] as? [Int] == [3000, 3001, 3002, 3003, 3004, 3005, 9110],
              "ports written, got \(String(describing: saved["work_localhost_ports"]))")

        var yes = 0, no = 0
        var panel: WorkSourcePanel? = WorkSourcePanel(
            source: port!, present: false, onYes: { yes += 1 }, onNo: { no += 1 })
        check(panel!.titleText == "Track localhost:9110 as work?", "title names the port, got \(panel!.titleText)")
        RunLoop.main.run(until: Date().addingTimeInterval(2))
        check(yes == 0 && no == 0, "nothing fires on its own")
        panel!.yes.performClick(nil)
        check(yes == 1 && no == 0, "Track fires yes only")
        panel = WorkSourcePanel(source: dir!, present: false, onYes: { yes += 1 }, onNo: { no += 1 })
        check(panel!.titleText == "Track this folder as work?", "title names a folder, got \(panel!.titleText)")
        panel!.no.performClick(nil)
        check(yes == 1 && no == 1, "Not work fires no only")

        // The prompter writes the answer through to the profile.
        let path2 = NSTemporaryDirectory() + "worksource2-\(getpid()).json"
        try! "{}".write(toFile: path2, atomically: true, encoding: .utf8)
        let store2 = WorkSourceStore(path: path2)
        let prompter = WorkSourcePrompter(store: store2, present: false)
        prompter.consider(url: "http://localhost:9999/")
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        prompter.consider(url: "http://localhost:9999/") // second ask while open: ignored
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        check(!store2.isKnown(LocalSource(kind: .port, value: "9999")), "unanswered stays unruled")

        exit(failures == 0 ? 0 : 1)
    }
}
