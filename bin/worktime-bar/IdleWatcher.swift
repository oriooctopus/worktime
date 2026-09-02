import AppKit

// MARK: - The idle prompt

// How long the machine goes untouched before it is worth asking about. The
// same threshold the probe excludes on (FOCUS_IDLE_SEC); it lives in both
// places because they are answering the same question from opposite ends --
// the app asks it live, the probe re-derives it from the log -- and a prompt
// that fired on a different boundary than the exclusion would ask about a
// stretch that was never going to be cut.
let IDLE_PROMPT_SEC = 120.0

// How long the panel waits for an answer. Nobody at the machine cannot reply,
// which is the point: silence IS the answer, and the exclusion it produces
// needs no record written for it.
let IDLE_CLAIM_SEC = 10

// How long a claim keeps counting after the click. Matches IDLE_GRACE_SEC in
// the probe, which is what actually applies it.
let IDLE_GRACE_SEC = 20 * 60

// Where a claim is recorded. Only claims are written: exclusion is the
// default and the focus log already says when the machine was untouched, so a
// second file listing the absences would be a copy that could disagree.
let IDLE_CLAIMS = ("~/.claude/stats/worktime/idle-claims.jsonl" as NSString)
    .expandingTildeInPath

// Whether the absence is put on screen. OFF, and the panel is the reason the
// whole feature was switched off: the question interrupted whatever was
// actually being done -- over a full-screen app, on every Space, mid-sentence
// -- to ask about a silence that HID idle had misread anyway, since it counts
// only keys and mouse and cannot tell reading, a call or a long build from an
// empty room. With the probe's IDLE_SUBTRACTS off there is nothing left for an
// answer to change, so a panel demanding one within ten seconds is pure
// interruption.
//
// The absence is still NOTICED, which is what "keep it for debugging" needs:
// every one is logged to stderr here, and the probe still finds them in the
// focus log and prints them on the timeline. Debugging reads a log; it does
// not need a window in front of the work being debugged.
let IDLE_PROMPT_VISIBLE = false

// Notices an absence, and -- when the prompt is on -- asks whether it counted.
final class IdleWatcher {
    /// Injectable so a test can write claims somewhere other than the real
    /// log, and so the suite never appends to the running app's file.
    private let claimsPath: String
    /// Injectable for the same reason, plus one more: a test that raised the
    /// real panel put a window on the user's screen every time the suite ran.
    private let visible: Bool
    private var panel: CountdownPanel?
    // One notice per absence. Without this it would fire every poll for as
    // long as somebody stayed away -- forty over a lunch, all about the same
    // silence.
    private var asked = false
    private var graceUntil = Date.distantPast

    /// Absences seen, so a test can prove the watcher still notices them with
    /// the panel off -- the half of the feature that is deliberately kept.
    private(set) var noticed = 0

    init(claimsPath: String = IDLE_CLAIMS, visible: Bool = IDLE_PROMPT_VISIBLE) {
        self.claimsPath = claimsPath
        self.visible = visible
    }

    private static let stamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f
    }()

    private static let dayfmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// `idle` is passed in rather than read here: FocusLog owns the reading,
    /// and a test cannot make the real machine go untouched for two minutes.
    func tick(idle: Double, now: Date = Date()) {
        // Back at the machine: re-arm for the next absence. This is the only
        // thing that clears `asked`, so one absence can only ever ask once.
        if idle <= IDLE_PROMPT_SEC {
            asked = false
            return
        }
        guard !asked, panel == nil, now >= graceUntil else { return }
        asked = true
        noticed += 1
        let since = now.addingTimeInterval(-idle)
        let clock = Self.stamp.string(from: since)
        // The record the absence leaves when nothing is shown. stderr is where
        // the bar's other diagnostics go, and the probe's timeline row is the
        // durable copy -- see IDLE_PROMPT_VISIBLE.
        FileHandle.standardError.write(
            "idle since \(clock) noticed; still counted\n".data(using: .utf8)!)
        guard visible else { return }
        panel = CountdownPanel(
            meeting: "Away since \(clock)",
            seconds: IDLE_CLAIM_SEC,
            buttonTitle: "I am here",
            messageFor: { "Still counting — noting this only — \($0)s" },
            onExpire: { [weak self] in
                self?.panel = nil
                FileHandle.standardError.write(
                    "idle since \(clock) unclaimed; still counted\n".data(using: .utf8)!)
            },
            onCancel: { [weak self] in
                guard let self else { return }
                self.panel = nil
                self.graceUntil = Date().addingTimeInterval(Double(IDLE_GRACE_SEC))
                self.claim(from: since, at: Date())
            })
    }

    /// The panel currently on screen, so a test can press its real button
    /// rather than assert that the wiring looks right.
    var prompt: CountdownPanel? { panel }

    /// Record that a person answered for this silence, covering the stretch
    /// asked about and the grace window after it as one span -- so the probe
    /// applies the window by reading the claim rather than by re-deriving a
    /// rule of its own. Still written while IDLE_SUBTRACTS is off, because a
    /// claim is now the only labelled example of "away by the threshold, but
    /// actually working" -- which is what a better threshold gets fitted to.
    private func claim(from since: Date, at now: Date) {
        let cal = Calendar.current
        func secOfDay(_ d: Date) -> Int {
            let c = cal.dateComponents([.hour, .minute, .second], from: d)
            return (c.hour ?? 0) * 3600 + (c.minute ?? 0) * 60 + (c.second ?? 0)
        }
        let row: [String: Any] = [
            "day": Self.dayfmt.string(from: now),
            "from": secOfDay(since),
            "until": secOfDay(now) + IDLE_GRACE_SEC,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: row),
              var line = String(data: data, encoding: .utf8)
        else { return }
        line += "\n"
        // O_APPEND so a second instance cannot interleave a half-written line
        // into this one. The focus log holds its handle open and does not do
        // this; claims are rare enough to pay for the open every time.
        let fd = open(claimsPath, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        guard fd >= 0 else {
            FileHandle.standardError.write(
                "idle claim could not be written to \(claimsPath)\n".data(using: .utf8)!)
            return
        }
        _ = line.withCString { write(fd, $0, strlen($0)) }
        close(fd)
        FileHandle.standardError.write("idle since \(Self.stamp.string(from: since)) claimed\n"
            .data(using: .utf8)!)
    }
}
