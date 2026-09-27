// Drive a real MeetingPromptPanel in-process: each button fires its own
// outcome exactly once, and leaving it alone fires nothing.
import AppKit

@main
enum MeetingPromptTests {
    static var failures = 0

    static func check(_ ok: Bool, _ what: String) {
        print(ok ? "ok   \(what)" : "FAIL \(what)")
        if !ok { failures += 1 }
    }

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        let began = f.date(from: "13:50")!

        var yes = 0, no = 0
        var panel: MeetingPromptPanel? = MeetingPromptPanel(
            began: began, present: false,
            onYes: { yes += 1 }, onNo: { no += 1 })
        check(panel!.messageText == "Mic in use since 13:50",
              "names the start, got \(panel!.messageText)")

        // No timer: an unanswered question is never a yes or a no.
        RunLoop.main.run(until: Date().addingTimeInterval(3))
        check(yes == 0 && no == 0, "nothing fires on its own (yes=\(yes) no=\(no))")

        panel!.update(began: began, ended: f.date(from: "14:25")!)
        check(panel!.messageText == "Mic was in use 13:50–14:25",
              "names the finished stretch, got \(panel!.messageText)")

        panel!.yes.performClick(nil)
        check(yes == 1 && no == 0, "Yes fires yes only (yes=\(yes) no=\(no))")
        panel = nil

        yes = 0; no = 0
        panel = MeetingPromptPanel(began: began, present: false,
                                   onYes: { yes += 1 }, onNo: { no += 1 })
        panel!.no.performClick(nil)
        check(no == 1 && yes == 0, "No fires no only (yes=\(yes) no=\(no))")
        panel = nil

        print(failures == 0 ? "all passed" : "\(failures) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
