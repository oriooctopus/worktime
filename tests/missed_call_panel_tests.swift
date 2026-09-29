// Drive a real MissedCallPanel in-process: Yes reports the times in the
// fields (edited or not), bad times are refused without firing, No fires no.
import AppKit

@main
enum MissedCallPanelTests {
    static var failures = 0

    static func check(_ ok: Bool, _ what: String) {
        print(ok ? "ok   \(what)" : "FAIL \(what)")
        if !ok { failures += 1 }
    }

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let call = MissedCall(blockStart: "12:00", blockEnd: "13:15",
                              start: "12:03", end: "13:15")

        var got: [(String, String)] = []
        var no = 0
        var panel: MissedCallPanel? = MissedCallPanel(
            call: call, present: false,
            onYes: { got.append(($0, $1)) }, onNo: { no += 1 })
        check(panel!.startField.stringValue == "12:03" && panel!.endField.stringValue == "13:15",
              "fields start at the guessed times")

        RunLoop.main.run(until: Date().addingTimeInterval(2))
        check(got.isEmpty && no == 0, "nothing fires on its own")

        panel!.startField.stringValue = "12:04"
        panel!.endField.stringValue = "13:8"
        panel!.yes.performClick(nil)
        check(got.isEmpty && panel!.errorText == "Times must be HH:MM",
              "malformed time refused, got \(panel!.errorText)")

        panel!.endField.stringValue = "12:00"
        panel!.yes.performClick(nil)
        check(got.isEmpty && panel!.errorText == "End must be after start",
              "backwards span refused, got \(panel!.errorText)")

        panel!.endField.stringValue = "13:08"
        panel!.yes.performClick(nil)
        check(got.count == 1 && got[0] == ("12:04", "13:08") && no == 0,
              "Yes reports the edited times, got \(got)")
        panel = nil

        got = []
        panel = MissedCallPanel(call: call, present: false,
                                onYes: { got.append(($0, $1)) }, onNo: { no += 1 })
        panel!.startField.stringValue = "9:05"
        panel!.yes.performClick(nil)
        check(got.count == 1 && got[0] == ("09:05", "13:15"),
              "single-digit hour normalised, got \(got)")
        panel = nil

        no = 0; got = []
        panel = MissedCallPanel(call: call, present: false,
                                onYes: { got.append(($0, $1)) }, onNo: { no += 1 })
        panel!.no.performClick(nil)
        check(no == 1 && got.isEmpty, "No fires no only")
        panel = nil

        print(failures == 0 ? "all passed" : "\(failures) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
