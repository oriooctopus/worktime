// Menu bar indicator for the worktime tracker.
//
// Native AppKit rather than SwiftBar/xbar/rumps: none of those are installed,
// and this needs no dependency beyond the swiftc already on the machine.
//
// It never computes anything. The probe owns the model; this polls
// `worktime-probe.py status` and draws a dot. Re-deriving state here would
// give a second opinion that could disagree with the dashboard, and two
// disagreeing answers are worse than one.

import AppKit
import Carbon.HIToolbox
import CoreAudio
import Foundation

// Overridable so the app can be run against a probe that reports a chosen
// state. The audio watcher only acts while the probe says a meeting is live,
// and there is no way to make that true on demand with the real probe short of
// putting a fake meeting in the real calendar and waiting for a real call --
// which is why this path went unexercised until it could be pointed somewhere.
let PROBE = ProcessInfo.processInfo.environment["WORKTIME_PROBE"]
    ?? ("~/.claude/bin/worktime-probe.py" as NSString).expandingTildeInPath
// 5s is affordable only because the probe's status path is memoised: the
// transcripts are parsed per-file against size+mtime, and a full re-derivation
// is floored at MIN_RECOMPUTE_SEC. Raising this back to 60 without those would
// be the safe move; lowering it below 5 would not buy anything, since the
// underlying data cannot be fresher than that floor.
let POLL_SEC = 5.0

// How long before a working period lapses into a gap (GAP_AFTER on the probe
// side) the dot starts blinking, and how fast it blinks. A full period is
// several minutes; a 60s warning is enough time to notice and act without
// making every ordinary period spend most of its life blinking.
let BLINK_WARNING_SEC = 60.0
let BLINK_INTERVAL = 0.5

// ⌘⌥S starts or ends a shift from anywhere, without opening the menu.
//
// Registered through Carbon's RegisterEventHotKey rather than
// NSEvent.addGlobalMonitorForEvents. The NSEvent path needs Accessibility
// permission granted to this exact binary, and the grant is keyed to the
// binary's signature -- recompiling revokes it silently, so the shortcut would
// die on every rebuild with no error surfacing anywhere. A Carbon hot key
// needs no permission and survives rebuilds.
let HOTKEY_CODE = UInt32(kVK_ANSI_S)
let HOTKEY_MODS = UInt32(cmdKey | optionKey)

// ⌥W logs an entry: one keypress saying this minute was worked, for work this
// machine has no way to see. Nothing opens, nothing is asked.
//
// Registered the same way, and deliberately without ⌘: the shift toggle is a
// decision about the whole day and wants the harder chord, while this is meant
// to be cheap enough to press mid-thought. ⌥W types "∑" in a text field, so
// the two-key form is only safe because a Carbon hot key consumes the event
// before the frontmost app sees it.
let ENTRY_HOTKEY_CODE = UInt32(kVK_ANSI_W)
let ENTRY_HOTKEY_MODS = UInt32(optionKey)

// Which hot key fired. The Carbon handler is installed once and shared, so it
// has to tell them apart by id rather than by which registration it came from.
let HOTKEY_ID_SHIFT = UInt32(1)
let HOTKEY_ID_ENTRY = UInt32(2)

// Absolute, not `/usr/bin/env python3`. launchd hands this process a PATH of
// /usr/bin:/bin:/usr/sbin:/sbin, so `env` resolves to Apple's /usr/bin/python3
// (3.9), which cannot even import the probe -- `str | None` in an annotation
// is a TypeError at def time there. Every poll then exited non-zero, refresh()
// returned early, and the dot sat on its launch placeholder: a stuck hollow
// amber that is indistinguishable from a genuine idle reading.
let PYTHON = "/opt/homebrew/bin/python3"

// How often to ask CoreAudio whether anything is capturing. Cheaper than the
// probe poll -- it is a couple of HAL property reads and touches no
// subprocess, no file and no network -- so it can run faster than POLL_SEC
// without costing anything, and a 2s grid keeps the countdown from appearing
// up to five seconds after the call actually stopped.
let AUDIO_POLL_SEC = 2.0

// A run of capture shorter than this was not a meeting; see CallDetector.
let MIN_CALL_SEC = 60.0
let SETTLE_SEC = 5.0

// Long enough to read the panel, notice it, and stop it; short enough that
// waiting it out is not itself an interruption.
let COUNTDOWN_SEC = 10

// Match the dashboard exactly. A different green here would read as a
// different state rather than the same state in another place.
let WORKING = NSColor(srgbRed: 0.098, green: 0.620, blue: 0.439, alpha: 1)  // #199e70
let AWAY    = NSColor(srgbRed: 0.788, green: 0.522, blue: 0.000, alpha: 1)  // #c98500
let MARKED  = NSColor(srgbRed: 0.380, green: 0.647, blue: 0.980, alpha: 1)
let BROKEN  = NSColor(srgbRed: 0.850, green: 0.200, blue: 0.200, alpha: 1)

struct Period {
    var start = 0
    var end = 0
    var len = 0
    var nPrompts = 0
    var nSlack = 0
    var what = ""
    var current = false
}

// One piece of evidence the probe's verdict was derived from: a prompt, a
// permission approval, a GitHub page read, or a stretch of attended time in an
// app that nothing else explains. `n` is how many identical consecutive events
// the probe folded into this one.
//
// A Slack row is the exception and is not evidence in its own right -- sends
// stopped counting as presence when focus started counting the reading too. It
// rides along to say what an already-counted minute was about.
struct Activity {
    // `t` is the clock time it happened, `at` the same instant as an absolute
    // one. The row shows an age derived from `at`; `t` is what its tooltip
    // says, and what the exact minute is recoverable from.
    var t = ""
    var at: TimeInterval = 0
    var kind = ""
    var what = ""
    var n = 1
    // Which session this event fell into, as the probe grouped them. Carried
    // so the raw list can mark the rest of a stretch when one of its rows is
    // hovered -- the grouping is the sessions view's answer, and this is that
    // answer without leaving the list. Nil when the payload had no sessions to
    // group by, which marks nothing rather than lumping the unlabelled
    // together as though they were one.
    var session: Int?
}

struct Status {
    var state = "unknown"
    var why = "not yet polled"
    var workedMinutes = 0
    var at = ""
    var quietSince: String?
    var quietSec: Int?
    var gapAfterSec: Int?
    var mode = "focused"
    var focusPct: Int?
    // HH:MM the link item would claim from, nil when there is nothing to link
    // to. Decided by the probe off the same periods the list is drawn from --
    // the menu must not work out for itself which period counts as the last
    // one, or it can name a different minute than the action then claims.
    var linkFrom: String?
    var periods: [Period] = []
    var activities: [Activity] = []
    var sessions: [ActSession] = []
}

func runProbe(_ args: [String]) -> String? {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: PYTHON)
    p.arguments = [PROBE] + args
    let out = Pipe()
    let err = Pipe()
    p.standardOutput = out
    p.standardError = err
    do { try p.run() } catch {
        FileHandle.standardError.write("probe launch failed: \(error)\n".data(using: .utf8)!)
        return nil
    }
    let data = out.fileHandleForReading.readDataToEndOfFile()
    let edata = err.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    // Logged, not swallowed. A silent non-zero exit is what let a blind poll
    // sit on screen looking like a confident idle reading.
    guard p.terminationStatus == 0 else {
        let msg = String(data: edata, encoding: .utf8) ?? ""
        FileHandle.standardError.write(
            "probe \(args) exit \(p.terminationStatus): \(msg)\n".data(using: .utf8)!)
        return nil
    }
    return String(data: data, encoding: .utf8)
}

// Confirmation for ⌥W. The press is silent by design -- nothing opens, no
// window takes the caret -- and the dot only moves if the minute was not
// already counted, so a working shortcut and a dead one look identical from
// the outside. The banner is the only thing that tells them apart.
//
// Posted with osascript rather than UNUserNotificationCenter because this app
// carries an ad-hoc signature, and macOS refuses an ad-hoc identity notification
// authorization outright: requestAuthorization returns "Notifications are not
// allowed for this application", after which a post is *accepted* at the call
// site and then silently dropped. A path that reports success and shows nothing
// is the worst possible shape for the one thing standing in for feedback, so
// this goes through an identity the system already trusts. Registering the
// bundle with lsregister and launching through LaunchServices were both tried;
// neither lifts the refusal.
//
// The cost of that choice is that macOS owns how long the banner stays up
// (roughly five seconds). A notification withdrawn on our own schedule needs
// the native API, which needs a real signing identity.
func notifyEntryLogged() {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = ["-e", "display notification \"Logged this minute as work.\" "
        + "with title \"Worktime\""]
    // Logged rather than swallowed, same as the probe: a banner that stops
    // appearing is indistinguishable from a hot key that stopped registering.
    do { try p.run() } catch {
        FileHandle.standardError.write(
            "notify launch failed: \(error)\n".data(using: .utf8)!)
        return
    }
    p.waitUntilExit()
    if p.terminationStatus != 0 {
        FileHandle.standardError.write(
            "notify exit \(p.terminationStatus)\n".data(using: .utf8)!)
    }
}

// ---------------------------------------------------------------------------
// Focus sampling
//
// Which app is frontmost, and how long since the last mouse or key event.
// Written here rather than derived by the probe because neither fact survives:
// nothing on disk records that Slack was frontmost at 14:03, so a probe that
// only ever reads files cannot see a stretch spent reading Slack. Sends were
// the previous stand-in and they are a poor one -- reading half an hour of
// #ruby-dev and answering nothing produces no evidence at all.
//
// Sampled on the poll tick rather than driven by
// NSWorkspace.didActivateApplicationNotification. Sampling makes the machine
// going away -- sleep, lock, this app crashing -- indistinguishable from
// samples simply stopping, which is exactly how it should read; the
// notification path would leave the last activation standing forever and
// credit the whole absence to whatever happened to be frontmost when the lid
// closed. It also declines to notice a two-second glance at Slack, which is
// the right call for time accounting.
//
// Neither API needs a permission grant. Accessibility is required only for
// window TITLES -- which Slack channel, which document -- and this
// deliberately stays at app granularity to avoid asking for that.
let FOCUS_DIR = ("~/.claude/stats/worktime/focus" as NSString).expandingTildeInPath

// A row is written when the front thing changes, and at no other time. The
// log is a record of switches.
//
// It used to also tick every thirty seconds, because the probe credited the
// span between consecutive rows and needed the span to stay short: without a
// heartbeat one row would sit there claiming a two-hour sleep. That made
// being in front a subscription -- an app left in front billed at the same
// rate all night, and on 2026-09-02 forty-one minutes of untouched Slack
// became forty-one minutes of work. The probe now credits the ACTIVATION and
// nothing else, so there is no span to truncate and no reason to tick.
//
// What the heartbeat also carried was the live idle reading, which is a
// genuinely continuous thing and now goes to PRESENCE_PATH: one file,
// overwritten, no history. Two signals that were sharing a channel because
// they happened to be sampled together.
let PRESENCE_PATH = ("~/.claude/stats/worktime/presence.json" as NSString)
    .expandingTildeInPath

// Chrome's active tab, as (title, url).
//
// The frontmost app alone says a browser is open, not what is in it, which is
// why Chrome earned no foreground credit at all: half of it is the job and
// half is shopping, and the app name cannot tell the two apart. The tab can,
// and nothing else can. History records navigations, so a doc opened yesterday
// and read all morning leaves no row anywhere -- that case, a parked tab with
// the day's work in it, is the whole reason this exists.
//
// Asked only on the samples where Chrome is already frontmost, so it costs one
// osascript per heartbeat at most and nothing at all while the browser sits
// behind something else.
//
// Title as well as URL because the classifier needs both and neither is
// sufficient: a Google Doc URL is an opaque id with no hint of the employer in
// it, and the title -- "Rubrik AI / RAC Policy -- OTEL Integration Test Cases"
// -- is the only place the work keyword appears. The reverse holds for a PR
// page whose title is somebody else's branch name.
//
// Requires the Automation permission for Chrome, which macOS prompts for once.
// Refusing it returns nil, and a sample with no tab on it earns nothing --
// precisely what every Chrome sample earned before this existed, so the
// declined case degrades to the old behaviour rather than to a wrong one.
let CHROME_BUNDLE = "com.google.Chrome"

// Chrome answers in single-digit milliseconds when it is healthy. This is not
// tuned for the healthy case: it is the wall against a browser wedged behind a
// modal, where the script never returns and would otherwise hang the poll
// timer -- and with it the menu, the countdown and the dot -- indefinitely.
let CHROME_TAB_TIMEOUT_SEC = 2.0

func chromeActiveTab() -> (title: String, url: String)? {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = CHROME_TAB_SCRIPT
    let out = Pipe()
    p.standardOutput = out
    // Both streams down one pipe. Two pipes would need two readers to avoid
    // deadlocking on a full buffer, and there is nothing to tell apart:
    // osascript writes nothing to stderr when it succeeds, so anything here
    // on a non-zero exit is the error text -- which is the only evidence that
    // separates a denied permission from a browser with no windows.
    p.standardError = out
    do { try p.run() } catch { return nil }

    let killer = DispatchWorkItem { if p.isRunning { p.terminate() } }
    DispatchQueue.global().asyncAfter(deadline: .now() + CHROME_TAB_TIMEOUT_SEC,
                                      execute: killer)
    // Read before waiting: readDataToEndOfFile returns at EOF, which is the
    // child exiting, so this is the wait. Terminating the child closes the
    // pipe, so the timeout unblocks it too.
    let data = out.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    killer.cancel()

    guard let text = String(data: data, encoding: .utf8) else { return nil }
    // A non-zero exit is the ordinary answer to "what is the front window?"
    // when Chrome has no windows open, and it is also what a refused Apple
    // Event looks like. Whatever it is, say it once per launch rather than
    // folding it into the no-windows case: a silent nil here is
    // indistinguishable in the focus log from a machine whose owner never
    // opened a browser, which is exactly how a delimiter bug survived every
    // day it ran.
    //
    // Once, because none of it can be fixed from here -- a refusal is granted
    // in System Settings, not by retrying -- and the poll would otherwise
    // repeat the line every few seconds.
    if p.terminationStatus != 0 {
        reportChromeTabFailure(text)
        return nil
    }
    // A reply with no page in it came back exit 0 -- so silence here is what
    // let both of these bugs run unnoticed. The reply says what it was.
    guard let tab = parseChromeTabReply(text) else {
        reportChromeTabFailure(text)
        return nil
    }
    return tab
}


final class FocusLog {
    private var lastKey: String?
    private var lastWrite = Date.distantPast
    private var handle: FileHandle?
    private var handleDay = ""

    private static let stamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    private static let dayfmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    // Seconds since the last input event of any kind, across the whole login
    // session. `.combinedSessionState` rather than `.hidSystemState` so that
    // input synthesised into the session counts the same as a real mouse --
    // the question is whether a person is driving the machine, not which
    // device the events came from.
    static func idleSeconds() -> Double {
        guard let anyInput = CGEventType(rawValue: ~0) else { return 0 }
        return CGEventSource.secondsSinceLastEventType(.combinedSessionState,
                                                       eventType: anyInput)
    }

    func sample(now: Date = Date()) {
        let app = NSWorkspace.shared.frontmostApplication
        let bundle = app?.bundleIdentifier ?? ""
        let day = Self.dayfmt.string(from: now)
        var row: [String: Any] = [
            "day": day,
            "t": Self.stamp.string(from: now),
            "app": app?.localizedName ?? "",
            "bundle": bundle,
            // Rounded, not thresholded. The threshold is the probe's to choose
            // and lives beside its other thresholds; duplicating it here would
            // give the two halves separate definitions of "away" that could
            // drift apart without either one looking wrong.
            "idle": Int(Self.idleSeconds().rounded()),
        ]
        // Only Chrome carries these, and only when the tab could be read. The
        // probe treats their absence as "not a page worth counting", which is
        // the same verdict it reaches for a tab that is genuinely not work --
        // so a sample written before this field existed, or by a machine that
        // declined the permission, needs no special handling anywhere.
        if bundle == CHROME_BUNDLE, let tab = chromeActiveTab() {
            row["tab"] = tab.title
            row["url"] = tab.url
        }

        // The live reading goes out on every poll, switch or not. It is what
        // the dot reads, and it is the one thing here that really is
        // continuous.
        writePresence(row)

        // Chrome is keyed by its tab as well as by itself, because Chrome
        // earns per PAGE: leaving a document for a PR inside the same window
        // is a switch by every measure the probe cares about, and comparing
        // bundles alone would file it as no event at all.
        let key = bundle == CHROME_BUNDLE
            ? bundle + "\u{1}" + ((row["tab"] as? String) ?? "")
            : bundle
        guard key != lastKey else { return }

        guard let data = try? JSONSerialization.data(withJSONObject: row),
              var line = String(data: data, encoding: .utf8)
        else { return }
        line += "\n"
        write(line, day: day)
        lastKey = key
        lastWrite = now
    }

    /// The current front app and idle reading, overwritten in place.
    ///
    /// Not appended and not history: the only question it answers is "as of
    /// right now, when did somebody last touch this machine with something
    /// work-shaped in front of it", which the focus log answered as a side
    /// effect of ticking. Written whole to a temp path and moved into place,
    /// so a reader mid-write sees the previous file rather than half of this
    /// one.
    private func writePresence(_ row: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: row)
        else { return }
        let tmp = PRESENCE_PATH + ".tmp"
        guard (try? data.write(to: URL(fileURLWithPath: tmp))) != nil
        else { return }
        try? FileManager.default.removeItem(atPath: PRESENCE_PATH)
        try? FileManager.default.moveItem(atPath: tmp, toPath: PRESENCE_PATH)
    }

    // One file per day, opened once and held. Appending through a fresh
    // FileHandle every 30s would reopen the file ~2,900 times a day for no
    // benefit, and a single ever-growing file would have to be re-read in full
    // to answer a question that only ever concerns today.
    private func write(_ line: String, day: String) {
        if handleDay != day {
            handle?.closeFile()
            handle = nil
        }
        if handle == nil {
            try? FileManager.default.createDirectory(
                atPath: FOCUS_DIR, withIntermediateDirectories: true)
            let path = (FOCUS_DIR as NSString).appendingPathComponent("\(day).jsonl")
            // O_APPEND, so every write lands at the file's real end as one
            // atomic step. seekToEndOfFile() instead caches an offset for the
            // life of the process, which is only correct while this process is
            // the sole writer -- and it is not: running a second copy by hand
            // (a build under test, a verification run) alongside the launchd
            // one gives both a stale offset, and the later write lands a few
            // bytes inside the earlier one. That truncates a line's opening
            // brace, and the probe parses the whole log strictly, so one torn
            // line takes down every reading the dot depends on.
            let fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
            guard fd >= 0 else {
                FileHandle.standardError.write(
                    "focus log open failed: \(String(cString: strerror(errno)))\n"
                        .data(using: .utf8)!)
                return
            }
            handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
            handleDay = day
        }
        guard let data = line.data(using: .utf8) else { return }
        // Logged rather than swallowed: a focus log that silently stopped
        // writing would read downstream as a day spent away from the machine,
        // which is a plausible-looking answer and therefore the dangerous kind
        // of failure.
        do { try handle?.write(contentsOf: data) } catch {
            FileHandle.standardError.write("focus log write failed: \(error)\n"
                .data(using: .utf8)!)
            handle?.closeFile()
            handle = nil
            handleDay = ""
        }
    }
}

// A coloured glyph as the button's attributed title, not an NSImage.
// An NSImage built with lockFocus() drew nothing at all when the process was
// started by launchd: there is no window context to focus in that environment,
// so the status item existed at zero visible width and the menu bar looked
// unchanged. A title needs no drawing context and renders identically whether
// the app was launched by hand or by launchd.
// Drawn as a bitmap, not a text glyph -- SF's circle glyphs sit off-center
// within a button's text baseline, and no attributedTitle tweak fixes that
// reliably. A custom-drawn image centers exactly in its frame every time.
func dotImage(_ color: NSColor, hollow: Bool) -> NSImage {
    let size = NSSize(width: 18, height: 18)
    let image = NSImage(size: size)
    image.lockFocus()
    let diameter: CGFloat = 8
    let rect = NSRect(x: (size.width - diameter) / 2, y: (size.height - diameter) / 2,
                       width: diameter, height: diameter)
    let path = NSBezierPath(ovalIn: rect)
    if hollow {
        color.setStroke()
        path.lineWidth = 1.5
        path.stroke()
    } else {
        color.setFill()
        path.fill()
    }
    image.unlockFocus()
    return image
}

// Same canvas size as dotImage, nothing drawn. Blinking swaps between the two
// rather than setting item.button?.image = nil -- nil drops the button's
// intrinsic size to zero and the whole status item collapses for each
// off-frame, visibly shoving neighboring menu bar icons sideways twice a
// second instead of just blinking the dot in place.
func emptyDotImage() -> NSImage {
    NSImage(size: NSSize(width: 18, height: 18))
}

// A period's summary line, matching the mockup's three fallback states: a
// real one-line summary, "in progress" for the still-growing current span,
// and "no summary" for one that closed too recently to have been summarized.
func periodStrings(_ p: Period) -> (top: String, what: String) {
    var top = "\(hhmm(p.start))–\(hhmm(p.end)) · \(human(p.len))   \(p.nPrompts)p"
    if p.nSlack > 0 { top += " · \(p.nSlack) slack" }
    var what = !p.what.isEmpty
        ? p.what
        : (p.current ? "in progress — still being summarized…" : "(no summary)")
    if what.count > 46 { what = String(what.prefix(45)) + "…" }
    return (top, what)
}

// Both the hand-built attributedTitle and the native title/subtitle pair got
// visibly dimmed by the menu's vibrancy blend -- the OS applies its own
// low-contrast rendering to any menu item text that isn't the plain-title
// clickable style, and no color or alpha set on that path survives it. A
// custom NSView is the one part of a menu item AppKit doesn't touch: it draws
// exactly the pixels its NSTextFields are told to, with no vibrancy pass in
// between. This is what it takes to get real full-contrast text in a row
// that isn't itself a clickable action.
// One row of mixed-styling text that truncates instead of wrapping.
//
// Every label in this menu is one line inside a fixed-height row, so a wrap is
// always a bug: the second line draws outside the row and straight over its
// neighbour. `lineBreakMode` on the field is not enough to prevent it -- an
// attributed value carries its own paragraph style, which wins, and the
// default one word-wraps. The style has to go on the string. Going through
// this helper is what keeps the next mixed-styling row from rediscovering
// that.
func singleLineLabel(_ text: NSMutableAttributedString) -> NSTextField {
    let style = NSMutableParagraphStyle()
    style.lineBreakMode = .byTruncatingTail
    text.addAttribute(.paragraphStyle, value: style,
                      range: NSRange(location: 0, length: text.length))

    let f = NSTextField(labelWithString: "")
    f.attributedStringValue = text
    f.maximumNumberOfLines = 1
    f.lineBreakMode = .byTruncatingTail
    return f
}

// "Nm since last activity" / "quiet Nm", with the dot in the state's colour.
//
// A line of its own since the period rows moved into their submenu. It used to
// ride on top of the first of them, which is why only the little dot carries
// colour: it sat directly above that period's time range and had to read as
// part of the same block rather than as a coloured banner.
final class StatusRowView: NSView {
    init(width: CGFloat, symbol: String, text: String, color: NSColor) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 21))

        let attr = NSMutableAttributedString(string: symbol + " ", attributes: [
            .font: NSFont.systemFont(ofSize: 12, weight: .medium),
            .foregroundColor: color,
        ])
        attr.append(NSAttributedString(string: text, attributes: [
            .font: NSFont.systemFont(ofSize: 12, weight: .medium),
            .foregroundColor: NSColor.labelColor,
        ]))
        let field = singleLineLabel(attr)
        field.translatesAutoresizingMaskIntoConstraints = false
        addSubview(field)

        NSLayoutConstraint.activate([
            field.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            field.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            field.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }
}

final class PeriodRowView: NSView {
    init(_ p: Period, width: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 34))

        let (top, what) = periodStrings(p)
        let topField = NSTextField(labelWithString: top)
        topField.font = NSFont.systemFont(ofSize: 13, weight: p.current ? .semibold : .regular)
        topField.textColor = .labelColor
        topField.lineBreakMode = .byTruncatingTail

        let whatField = NSTextField(labelWithString: what)
        whatField.font = NSFont.systemFont(ofSize: 11)
        whatField.textColor = .secondaryLabelColor
        whatField.lineBreakMode = .byTruncatingTail

        // A custom view's own NSTextFields are never touched by the menu's
        // vibrancy dimming, so these colors render exactly as set -- unlike
        // NSMenuItem.title/attributedTitle, which AppKit recolors regardless
        // of what's specified there.
        for f in [topField, whatField] {
            f.translatesAutoresizingMaskIntoConstraints = false
            addSubview(f)
        }

        NSLayoutConstraint.activate([
            topField.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            topField.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            topField.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            whatField.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            whatField.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            whatField.topAnchor.constraint(equalTo: topField.bottomAnchor, constant: 1),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }
}

// A single line, unlike the two-line period rows: ten of these hang under one
// header, and giving each a second line would turn the dropdown into a page.
// The three parts are separately colored so the eye can skip the columns it
// isn't looking for -- the time and the kind recede, the text reads.
//
// Same custom-NSView reason as PeriodRowView: an NSMenuItem's own title is
// recolored by the menu's vibrancy pass no matter what is set on it, and these
// rows would come out dimmed to the point of being hard to read.
let ACTIVITY_FONT = NSFont.systemFont(ofSize: 11)

// How long ago, not when. A list of ten things read in one glance is answering
// "how recently was I working", and "4m" answers that directly where "12:36"
// makes the reader subtract. Computed here rather than in the probe because
// the probe's answer is memoised until the underlying files change: an age
// baked in there would be frozen at whatever it was when it was written.
//
// Whole minutes below an hour and whole hours above it. The rows are recorded
// to the minute, so there is no finer truth to show, and a second unit ("2h
// 40m") would widen the column for a list whose top rows are the point.
//
// Rounded to the nearest minute rather than floored, because the header above
// this list rounds ("2m since last activity" is `f"{quiet:.0f}m"` in the
// probe) and the newest row is usually describing that very same event. Two
// numbers for one instant is what the reader notices first, and flooring made
// them disagree for the 30 seconds either side of every minute boundary.
func activityAge(_ a: Activity, now: Date = Date()) -> String {
    let mins = Int(((now.timeIntervalSince1970 - a.at) / 60).rounded(.toNearestOrEven))
    if mins < 1 { return "now" }
    if mins < 60 { return "\(mins)m" }
    return "\(mins / 60)h"
}

// Widest of the strings actually being shown, measured rather than guessed at.
// Taken over the whole list once and handed to every row, which is what makes
// the columns line up -- a per-row width just reproduces the ragged edge that
// a proportional face gives "focus" against "approval", or "9m" against "45m".
// Measuring beats a constant because both sets grow: a new stream with a
// longer name, or a day long enough to reach three digits of minutes, would
// silently clip against a hardcoded width.
func columnWidth(_ labels: [String]) -> CGFloat {
    ceil(labels.map {
        ($0 as NSString).size(withAttributes: [.font: ACTIVITY_FONT]).width
    }.max() ?? 0)
}

// Which session the pointer is currently over, shared by every row of one
// built menu.
//
// It lives outside the rows because the answer the raw list gives on hover is
// about the OTHER rows: pointing at one event marks every event of the same
// stretch, which is the sessions view's grouping shown without leaving the
// list. A row cannot know that on its own -- it needs to hear that a sibling
// was entered -- so the rows share one of these and redraw when it changes.
final class SessionHover {
    private(set) var session: Int?
    // Every row that can respond, session rows included. Held strongly: the
    // menu owns the views, this object is rebuilt with them, and both are
    // discarded together on the next build.
    var rows: [NSView] = []
    // Called with the row that was entered, so the events panel can follow the
    // pointer. Nil for the raw list, which answers a hover by marking its own
    // rows and opens nothing.
    var onEnter: ((Int?, NSView?) -> Void)?

    func enter(_ id: Int?, from view: NSView? = nil) {
        guard id != session else { return }
        session = id
        for r in rows { r.needsDisplay = true }
        onEnter?(id, view)
    }
}

// The tint a hovered row takes. Deliberately not the menu's own blue selection
// fill: that is the OS's way of saying "this is what a click will act on", and
// these rows are not clickable -- a full selection bar on them would promise a
// press that does nothing. A wash of the accent colour reads as "this is what
// you are pointing at" without making that promise.
func hoverFill() -> NSColor {
    NSColor.controlAccentColor.withAlphaComponent(0.13)
}

final class ActivityRowView: NSView {
    // Set when this row belongs to a session the list also knows about; nil
    // when the payload carried no grouping, in which case hovering marks
    // nothing rather than pretending every unlabelled row is one group.
    var session: Int?
    weak var hover: SessionHover?
    // Whether this row is the top or the bottom of its session's run, which is
    // what the bracket's two arms are drawn from.
    var groupFirst = false
    var groupLast = false

    init(_ a: Activity, width: CGFloat, age: String,
         ageWidth: CGFloat, kindWidth: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 17))

        // Right-aligned, so the unit letters line up under each other and the
        // number grows leftward into the column's own slack instead of pushing
        // the kind beside it out of true.
        let ageField = NSTextField(labelWithString: age)
        ageField.font = ACTIVITY_FONT
        ageField.textColor = .secondaryLabelColor
        ageField.alignment = .right
        // The age is the rounded version; this is where the exact minute it
        // was rounded from stays reachable. It is a tooltip on the row itself
        // rather than anything drawn, so it does not compete with the bracket
        // the hover draws -- the two say different things about the same row.
        toolTip = a.t

        let kindField = NSTextField(labelWithString: a.kind)
        kindField.font = ACTIVITY_FONT
        kindField.textColor = .tertiaryLabelColor

        let line = NSMutableAttributedString(string: a.what, attributes: [
            .font: ACTIVITY_FONT,
            .foregroundColor: NSColor.labelColor,
        ])
        // Only when it collapsed something. A "×1" on every other row would be
        // noise standing in for the ordinary case.
        if a.n > 1 {
            line.append(NSAttributedString(string: "  ×\(a.n)", attributes: [
                .font: ACTIVITY_FONT,
                .foregroundColor: NSColor.secondaryLabelColor,
            ]))
        }
        // Truncated here rather than by the probe: the widget is the only
        // party that knows its own width, and the payload is capped already.
        let whatField = singleLineLabel(line)

        for f in [ageField, kindField, whatField] {
            f.translatesAutoresizingMaskIntoConstraints = false
            addSubview(f)
        }
        // Only the text column gives way when the menu is too narrow for the
        // row; the two fixed columns are what the alignment is made of, so
        // they must never be the ones that shrink or truncate.
        for f in [ageField, kindField] {
            f.setContentCompressionResistancePriority(.required, for: .horizontal)
        }
        whatField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        NSLayoutConstraint.activate([
            // Indented past the bracket's gutter, so the columns do not shift
            // sideways when a group lights up -- the mark has to be drawn in
            // space the text never occupies or the whole list jumps on hover.
            ageField.leadingAnchor.constraint(equalTo: leadingAnchor,
                                              constant: 14 + GROUP_GUTTER),
            ageField.widthAnchor.constraint(equalToConstant: ageWidth),
            kindField.leadingAnchor.constraint(equalTo: ageField.trailingAnchor, constant: 8),
            kindField.widthAnchor.constraint(equalToConstant: kindWidth),
            whatField.leadingAnchor.constraint(equalTo: kindField.trailingAnchor, constant: 8),
            whatField.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            ageField.centerYAnchor.constraint(equalTo: centerYAnchor),
            kindField.firstBaselineAnchor.constraint(equalTo: ageField.firstBaselineAnchor),
            whatField.firstBaselineAnchor.constraint(equalTo: ageField.firstBaselineAnchor),
        ])
    }

    // Rebuilt on every layout pass rather than added once: menu rows are laid
    // out after they are made, and a tracking area added against the initial
    // zero-ish frame covers the wrong rectangle for the row's whole life.
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for a in trackingAreas { removeTrackingArea(a) }
        addTrackingArea(NSTrackingArea(
            rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self, userInfo: nil))
    }

    override func mouseEntered(with _: NSEvent) { hover?.enter(session) }
    // Cleared rather than left standing: the pointer leaving the list has to
    // put it back the way it was, and the next row entered replaces this
    // anyway, so the two together mean the mark follows the pointer exactly.
    override func mouseExited(with _: NSEvent) { hover?.enter(nil) }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let session, hover?.session == session else { return }
        hoverFill().setFill()
        bounds.fill()
        drawGroupBracket(in: bounds, first: groupFirst, last: groupLast)
    }
}

// How much room the group bracket gets at the leading edge of a raw row.
let GROUP_GUTTER: CGFloat = 10

// One row's slice of the bracket that marks a hovered session: a vertical
// stroke down the gutter, with a short arm at whichever end is the end of the
// run.
//
// Drawn per row rather than as one shape over the group because a menu has no
// surface spanning several items -- each item is its own view and cannot paint
// outside itself. The arms are what makes the stack of strokes read as a
// single bracket rather than as a stripe on each row.
func drawGroupBracket(in bounds: NSRect, first: Bool, last: Bool) {
    let x = bounds.minX + GROUP_GUTTER / 2
    let arm: CGFloat = 4
    let path = NSBezierPath()
    path.lineWidth = 1.5
    path.lineCapStyle = .round
    // The stroke runs the full height of every row, so consecutive rows join
    // into one unbroken line down the group.
    path.move(to: NSPoint(x: x, y: bounds.minY))
    path.line(to: NSPoint(x: x, y: bounds.maxY))
    if first {
        path.move(to: NSPoint(x: x, y: bounds.maxY - 0.75))
        path.line(to: NSPoint(x: x + arm, y: bounds.maxY - 0.75))
    }
    if last {
        path.move(to: NSPoint(x: x, y: bounds.minY + 0.75))
        path.line(to: NSPoint(x: x + arm, y: bounds.minY + 0.75))
    }
    NSColor.controlAccentColor.setStroke()
    path.stroke()
}

// Two lines, unlike the raw rows' one. A session stands for a dozen of them,
// so it can afford the height the thing it replaced would have spent anyway --
// and the same custom-NSView reason as PeriodRowView applies: an NSMenuItem's
// own title is dimmed by the menu's vibrancy pass no matter what is set on it.
final class SessionRowView: NSView {
    // Its own index, so entering it lights this row alone. It shares the
    // hover object with the raw rows because only one list is ever on screen,
    // and one mechanism for "what is the pointer on" is one fewer to keep in
    // step than two.
    var session: Int?
    weak var hover: SessionHover?

    init(_ s: ActSession, width: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 34))
        let (top, what) = sessionStrings(s)

        let topField = NSTextField(labelWithString: top)
        topField.font = NSFont.systemFont(ofSize: 12,
                                          weight: s.current ? .semibold : .regular)
        topField.textColor = s.counted ? .labelColor : .secondaryLabelColor
        topField.lineBreakMode = .byTruncatingTail

        let whatField = NSTextField(labelWithString: what)
        whatField.font = ACTIVITY_FONT
        whatField.textColor = .secondaryLabelColor
        whatField.lineBreakMode = .byTruncatingTail

        // AppKit draws the submenu arrow for an ordinary menu item and draws
        // nothing at all for one with a custom view, so a row whose events are
        // one hover away would look exactly like a row that has none.
        //
        // On the leading edge rather than the trailing one, where a submenu
        // arrow normally sits. The trailing edge of these rows is a ragged one
        // -- the two lines are different lengths and both truncate -- so an
        // arrow parked out there floated away from the row it belongs to,
        // while the leading edge is the one straight line the block already
        // has. It also puts the mark in the same gutter the raw list's bracket
        // uses, so both lists say "there is more here" in the same column.
        //
        // Pointing back out of the menu, not into it. On the trailing edge an
        // arrow points the way the submenu will open and that is what makes it
        // legible; moved to this side, the same glyph pointed at the row's own
        // text, which reads as "the rest is that way" aimed at the words right
        // next to it.
        let arrow = NSTextField(labelWithString: "\u{2039}")
        arrow.font = NSFont.systemFont(ofSize: 13)
        arrow.textColor = .tertiaryLabelColor

        for f in [topField, whatField, arrow] {
            f.translatesAutoresizingMaskIntoConstraints = false
            addSubview(f)
        }
        NSLayoutConstraint.activate([
            arrow.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            arrow.centerYAnchor.constraint(equalTo: centerYAnchor),
            topField.leadingAnchor.constraint(equalTo: arrow.trailingAnchor, constant: 6),
            topField.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            topField.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            whatField.leadingAnchor.constraint(equalTo: topField.leadingAnchor),
            whatField.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            whatField.topAnchor.constraint(equalTo: topField.bottomAnchor, constant: 1),
        ])
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for a in trackingAreas { removeTrackingArea(a) }
        addTrackingArea(NSTrackingArea(
            rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self, userInfo: nil))
    }

    // The fill and the panel both land on mouseEntered, the moment the
    // pointer arrives. The submenu this replaced sat behind the OS's hover
    // delay, so a row gave no sign at all for the half second before its
    // events appeared.
    //
    // `self` goes along so whoever opens the panel knows which row to stand
    // beside; the view is the only thing that knows where on screen it ended
    // up.
    override func mouseEntered(with _: NSEvent) { hover?.enter(session, from: self) }
    override func mouseExited(with _: NSEvent) { hover?.enter(nil) }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let session, hover?.session == session else { return }
        hoverFill().setFill()
        bounds.fill()
    }
}

func addPeriodItem(_ p: Period, to menu: NSMenu) {
    let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    item.view = PeriodRowView(p, width: 300)
    menu.addItem(item)
}

// MARK: - Is anything capturing audio right now

// Reads the HAL's own view of which devices are live. It never opens a stream,
// so it needs no microphone permission, prompts for none, and does not light
// the orange recording indicator -- this app must be able to notice a meeting
// without looking like a participant in it.
//
// Every input-capable device is checked rather than just the default one: the
// default input changes when AirPods connect, and a meeting can hold a device
// that is not the default at all.

func audioDeviceIDs() -> [AudioObjectID] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr
    else { return [] }
    var ids = [AudioObjectID](repeating: 0,
                              count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr
    else { return [] }
    return ids
}

// The filter that makes this a microphone test rather than an audio test.
// DeviceIsRunningSomewhere is true of the speakers whenever anything can play
// through them -- on this machine "MacBook Pro Speakers" reads 1 at rest, with
// nothing playing -- so without restricting to devices that actually carry an
// input stream, every reading would say a call was in progress forever.
func deviceHasInput(_ id: AudioObjectID) -> Bool {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreams,
        mScope: kAudioObjectPropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr else { return false }
    return size > 0
}

func deviceIsRunning(_ id: AudioObjectID) -> Bool {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr else { return false }
    return value != 0
}

/// True while any process anywhere is capturing audio input. App-agnostic by
/// construction: it is a property of the device, so a call in a browser tab, a
/// native client, or something the calendar has never heard of all read the
/// same.
func anythingIsCapturing() -> Bool {
    audioDeviceIDs().contains { deviceHasInput($0) && deviceIsRunning($0) }
}

// MARK: - The menu bar item

final class Bar: NSObject, NSApplicationDelegate, NSMenuDelegate, NSMenuItemValidation {
    // Created in applicationDidFinishLaunching, NOT as a stored-property
    // initializer. Built at property-init time the item came back with
    // isVisible == true and a live button, yet never appeared in the menu bar:
    // the status bar is not ready to place items before the app has launched.
    var item: NSStatusItem!
    var status = Status()
    var timer: Timer?
    var blinkTimer: Timer?
    var blinkOn = true
    // The moment activity was last seen, derived from quiet_sec at the last
    // poll. Recomputing "how close to lapsing" from this anchor every blink
    // tick (instead of only at POLL_SEC) is what lets the blink count down
    // smoothly between polls rather than jumping every 5s.
    var lastActivityAt: Date?
    var hotKeyRef: EventHotKeyRef?
    var entryHotKeyRef: EventHotKeyRef?
    // Which of the two activity views is showing, remembered across launches.
    // The choice is about how the reader wants to read the day rather than
    // about anything happening in it, so having it reset every time the app is
    // rebuilt would make it feel like a mode that keeps slipping back.
    var grouped = UserDefaults.standard.bool(forKey: "activityGrouped")
    // What the pointer is on inside the activity list, shared by that list's
    // rows. Held here only so it outlives build() and is replaced by the next
    // one, alongside the views it drives.
    var hover: SessionHover?
    // One panel for the app's lifetime -- see SessionPopover for why it is a
    // panel and not the submenu it replaced.
    let popover = SessionPopover()
    let focusLog = FocusLog()
    var audioTimer: Timer?
    var detector = CallDetector(minCallSec: MIN_CALL_SEC, settleSec: SETTLE_SEC)
    // Non-nil only while a countdown is on screen.
    var countdown: CountdownPanel?
    let idleWatcher = IdleWatcher()
    // One menu for the app's lifetime, mutated in place rather than replaced.
    // Assigning a freshly built NSMenu to item.menu does nothing to a menu that
    // is already on screen: AppKit goes on displaying the instance it was handed
    // when the menu opened. So every poll used to build a new menu into an
    // object nobody could see while the stale one stayed up, and the dropdown
    // sat minutes behind reality. The dot never had this problem -- it is the
    // status item's button image, one object written in place every poll --
    // which is exactly why the dot tracked the truth and the menu did not.
    let menu = NSMenu()
    // What the menu currently renders. Rebuilding relays the menu out, which
    // flickers while it is open, and most polls change nothing that shows.
    var lastMenuKey = ""
    // Whether the dropdown is on screen. ⌘⌥S now exists twice -- as the Carbon
    // hot key and as the toggle row's key equivalent -- and an open menu
    // matches its own key equivalents while the hot key manager goes on
    // dispatching the chord regardless. Both would fire, so a shift would be
    // started and immediately ended by one press. While the menu is up it owns
    // the chord and the hot key stands down.
    var menuIsOpen = false

    // The Carbon handler is a bare C function pointer and cannot capture, so
    // it reaches the app through the `bar` global rather than through self.
    // It fires on the event thread; the toggle reads status and rebuilds the
    // menu, so it hops to main first.
    func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            // Read out of the event rather than assumed: one handler now
            // serves both hot keys, so acting without asking which fired
            // would make ⌥W end the shift.
            var id = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &id)
            DispatchQueue.main.async {
                switch id.id {
                case HOTKEY_ID_ENTRY: bar.logEntry()
                default:              if !bar.menuIsOpen { bar.toggleShift() }
                }
            }
            return noErr
        }, 1, &spec, nil, nil)
        // Logged because a hot key that fails to register fails silently and
        // invisibly: the shortcut simply does nothing, which is indistinguishable
        // from the app being down or the probe erroring.
        let err = RegisterEventHotKey(HOTKEY_CODE, HOTKEY_MODS,
                                      EventHotKeyID(signature: OSType(0x574B_5453),
                                                    id: HOTKEY_ID_SHIFT),
                                      GetApplicationEventTarget(), 0, &hotKeyRef)
        FileHandle.standardError.write("hotkey cmd+opt+S register -> \(err)\n".data(using: .utf8)!)
        let entryErr = RegisterEventHotKey(ENTRY_HOTKEY_CODE, ENTRY_HOTKEY_MODS,
                                           EventHotKeyID(signature: OSType(0x574B_5453),
                                                         id: HOTKEY_ID_ENTRY),
                                           GetApplicationEventTarget(), 0, &entryHotKeyRef)
        FileHandle.standardError.write("hotkey opt+W register -> \(entryErr)\n".data(using: .utf8)!)
    }

    func applicationDidFinishLaunching(_: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = dotImage(AWAY, hollow: true)
        item.button?.imagePosition = .imageOnly
        menu.delegate = self
        item.menu = menu
        build()
        refresh()
        focusLog.sample()
        registerHotKey()
        timer = Timer.scheduledTimer(withTimeInterval: POLL_SEC, repeats: true) { _ in
            // Sampled here and not inside refresh(): menuNeedsUpdate also calls
            // refresh(), so opening the menu would otherwise log an extra
            // sample and make "how often was this app frontmost" partly a
            // measure of how often the dropdown was opened.
            self.focusLog.sample()
            self.idleWatcher.tick(idle: FocusLog.idleSeconds())
            self.refresh()
        }
        blinkTimer = Timer.scheduledTimer(withTimeInterval: BLINK_INTERVAL, repeats: true) { _ in
            self.tickBlink()
        }
        audioTimer = Timer.scheduledTimer(withTimeInterval: AUDIO_POLL_SEC, repeats: true) { _ in
            self.tickAudio()
        }
        // Both timers must run in .common, not the .default mode
        // scheduledTimer gives them. An open menu spins the run loop in
        // .eventTracking, where a .default-mode timer simply does not fire: for
        // as long as the dropdown was up, nothing polled, nothing rebuilt, and
        // the menu could not have updated even once. That is the other half of
        // why the dot looked live and the menu looked frozen -- the dot's
        // updates all landed in the moments the menu was closed.
        for t in [timer, blinkTimer, audioTimer] {
            if let t { RunLoop.main.add(t, forMode: .common) }
        }
    }

    // The calendar says when a meeting was scheduled to end; the microphone
    // says when the talking actually stopped. Only the second one knows that a
    // half-hour slot finished in twelve minutes, and it knows it for calls the
    // calendar has never heard of too.
    func tickAudio() {
        let capturing = anythingIsCapturing()

        // The call came back while the countdown was still running -- someone
        // rejoined, or a device handoff outlasted the settle. Either way this
        // is not a meeting that ended, so the countdown is withdrawn and
        // nothing is written.
        if capturing, let panel = countdown {
            panel.close()
            countdown = nil
            FileHandle.standardError.write("call resumed; countdown withdrawn\n".data(using: .utf8)!)
        }

        guard detector.update(capturing: capturing, now: Date()) else { return }

        // Only a calendar meeting can be ended early, because only a calendar
        // meeting holds the dot green on a schedule that can outlive the call.
        // A manual mark is a human declaration and is not something a
        // microphone reading gets to revoke; ordinary prompt activity lapses on
        // its own. So when the probe is not currently leaning on a meeting,
        // there is nothing for this to stop and no reason to interrupt.
        guard status.why.hasPrefix("in ") else {
            FileHandle.standardError.write(
                "call ended, no meeting to close (\(status.why))\n".data(using: .utf8)!)
            return
        }
        guard countdown == nil else { return }

        let meeting = String(status.why.dropFirst("in ".count))
        FileHandle.standardError.write("call ended during \(meeting); counting down\n".data(using: .utf8)!)
        countdown = CountdownPanel(
            meeting: meeting, seconds: COUNTDOWN_SEC,
            onExpire: { [weak self] in
                self?.countdown = nil
                self?.endMeetingEarly()
            },
            onCancel: { [weak self] in
                self?.countdown = nil
                FileHandle.standardError.write("countdown cancelled by hand\n".data(using: .utf8)!)
            })
    }

    // Only a working period can lapse, so only "working" ever blinks -- marked
    // spans are ended by hand, and idle/broken have nothing to warn about.
    func tickBlink() {
        guard status.state == "working",
              let anchor = lastActivityAt,
              let gapAfterSec = status.gapAfterSec
        else {
            if !blinkOn { blinkOn = true; item.button?.image = dotImage(WORKING, hollow: false) }
            return
        }
        // A minute of warning on a five-minute period is a warning; a minute of
        // warning on the one-minute cutoff an unfocused bout opens with is a dot
        // that blinks for its entire life and never signals anything. Capped at
        // a third of the cutoff so the warning stays proportional to whatever
        // the current run has actually earned.
        let warn = min(BLINK_WARNING_SEC, Double(gapAfterSec) / 3)
        let remaining = Double(gapAfterSec) - Date().timeIntervalSince(anchor)
        guard remaining > 0, remaining <= warn else {
            if !blinkOn { blinkOn = true; item.button?.image = dotImage(WORKING, hollow: false) }
            return
        }
        blinkOn.toggle()
        item.button?.image = blinkOn ? dotImage(WORKING, hollow: false) : emptyDotImage()
    }

    // Called on every poll and again from menuNeedsUpdate just before the
    // dropdown is shown, so what opens is never older than the last reading.
    func build() {
        // "worked today" stands alone -- it's the day total, a different
        // metric from anything a period row shows, so it keeps its own row.
        var worked = "worked today: \(human(status.workedMinutes))"
        if let f = status.focusPct { worked += "   \(f)% focus" }

        // "Nm since last activity" / "quiet Nm" used to be its own head row
        // above this list, describing the exact same current period a second
        // time. Folding it into that period's own row said it once instead
        // of twice, and reads as one glance instead of two rows to reconcile.
        let symbol = (status.state == "working" || status.state == "marked") ? "●" : "○"
        let color: NSColor = status.state == "marked" ? MARKED
            : (status.state == "working" ? WORKING : AWAY)

        // In focused mode the cutoff is always five minutes, so "2m since last
        // activity" already tells you how much silence the run has left. In
        // unfocused mode it does not: the cutoff is whatever the bout has
        // earned on the ramp so far, anywhere between one and five minutes, and
        // nothing on screen said which. That is exactly the reading that looked
        // wrong -- a bout switched to unfocused mid-run carries the width it
        // already earned, so it goes on counting through silences that the "1m,
        // widening to 5m" label implies would have ended it. Naming both halves
        // -- what is left, and of what -- makes the rule in force visible while
        // it is still in force.
        var why = status.why
        if status.mode == "unfocused", status.state == "working",
           let quiet = status.quietSec, let cutoff = status.gapAfterSec {
            // Ceil, so the countdown reads "1m left" for the whole final minute
            // rather than dropping to a 0m that never corresponds to anything:
            // the moment it truly reaches zero the state is no longer working
            // and this line is gone.
            let left = Int(ceil(Double(cutoff - quiet) / 60))
            if left > 0 {
                why += "  ·  \(left)m left of \(Int((Double(cutoff) / 60).rounded()))m"
            }
        }

        // Every period lives in the submenu, none of them at the top level.
        // They used to lead the menu -- three two-line rows of time ranges,
        // counts and summaries -- but the activity list now says what the day
        // was made of in more detail and in less space, and the two together
        // read as the same day divided twice. Kept rather than dropped: the
        // periods are the only place the AI summaries surface, and one
        // collapsed row is a cheap place to keep them.
        let periods = status.periods

        // Keyed on exactly the strings that get rendered, so a poll that
        // changes nothing visible costs nothing and cannot flicker an open
        // menu. quiet_sec ticking every 5s is deliberately not in here -- the
        // menu shows it rounded to minutes, so it earns a rebuild once a
        // minute, not twelve times.
        // Ages go in the key, not just the rows. They are the one part of the
        // menu that changes without the probe's answer changing at all, so
        // keying on the activity alone would hold "1m" on screen indefinitely.
        let ages = status.activities.map { activityAge($0) }
        // Both views' strings go in the key, not just the one on screen: the
        // toggle changes which is drawn without changing anything the probe
        // said, so a key built from the visible view alone would match on the
        // click that flips it and leave the old list up.
        let key = ([worked, symbol, why, status.state, status.mode,
                    grouped ? "grouped" : "raw"]
                   + periods.map { p in
                       let s = periodStrings(p)
                       return s.top + "\u{1}" + s.what
                   }
                   + zip(status.activities, ages).map { a, age in
                       "\(age)\u{1}\(a.kind)\u{1}\(a.what)\u{1}\(a.n)"
                   }
                   // sessionKey covers the submenu's rows as well as the two
                   // visible lines -- see its own comment for why what is
                   // hidden behind a hover still has to be in here.
                   + status.sessions.map(sessionKey)
                  ).joined(separator: "\u{2}")
        if key == lastMenuKey { return }
        lastMenuKey = key

        let m = menu
        m.removeAllItems()
        let w = NSMenuItem(title: worked, action: nil, keyEquivalent: "")
        w.isEnabled = false
        m.addItem(w)
        m.addItem(.separator())

        let st = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        st.view = StatusRowView(width: 300, symbol: symbol,
                                text: why, color: color)
        m.addItem(st)
        m.addItem(.separator())

        // The evidence the verdict was derived from, at the top level rather
        // than in a submenu: without it, the only answer to "why is it green
        // right now" was the one rounded phrase in the status line, and the
        // only way to check what the tracker had actually seen was to read the
        // dashboard.
        //
        // Ten single-line rows is the one place this menu spends real height,
        // which is why they are single-line and why the periods sit in a
        // submenu: this list is the log, so they don't need to be.
        //
        // The same evidence can be read two ways, and which one is wanted
        // depends on the question. "Why is the dot green right now" is answered
        // by the newest few events; "what did this morning consist of" is
        // answered by the day gathered into its periods, where ten raw rows
        // reach back twenty minutes and eight sessions reach back hours. A
        // checked row under the header switches between them, and the header
        // says which is showing so the list is never ambiguous about it.
        if !status.activities.isEmpty {
            let head = NSMenuItem(title: grouped ? "Recent activity — sessions"
                                                 : "Recent activity",
                                  action: nil, keyEquivalent: "")
            head.isEnabled = false
            m.addItem(head)
            let toggle = NSMenuItem(title: "Group into sessions",
                                    action: #selector(toggleGrouped), keyEquivalent: "g")
            toggle.state = grouped ? .on : .off
            m.addItem(toggle)

            // One per build, and replaced with the rows it belongs to: the old
            // views go when the menu is emptied, so a hover object kept across
            // builds would be holding rows that are no longer on screen.
            let hover = SessionHover()
            self.hover = hover
            if grouped {
                let sessions = status.sessions
                // Opening the panel is the menu's job, not the row's: only
                // this level knows the menu's own window, and the panel has to
                // be placed against that rather than against the row alone.
                hover.onEnter = { [weak self] id, view in
                    guard let self else { return }
                    guard let id, id < sessions.count, let view,
                          let window = view.window, let screen = window.screen
                    else {
                        self.popover.hide()
                        return
                    }
                    self.popover.show(sessions[id],
                                      row: window.convertToScreen(
                                          view.convert(view.bounds, to: nil)),
                                      menu: window.frame,
                                      screen: screen.visibleFrame)
                }
                for (i, s) in sessions.enumerated() {
                    let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
                    let view = SessionRowView(s, width: 300)
                    view.session = i
                    view.hover = hover
                    hover.rows.append(view)
                    item.view = view
                    m.addItem(item)
                }
            } else {
                let ageWidth = columnWidth(ages)
                let kindWidth = columnWidth(status.activities.map(\.kind))
                let sessions = status.activities.map(\.session)
                for (i, (a, age)) in zip(status.activities, ages).enumerated() {
                    let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
                    let view = ActivityRowView(a, width: 300, age: age,
                                               ageWidth: ageWidth, kindWidth: kindWidth)
                    view.session = a.session
                    view.hover = hover
                    // The list is in time order and a session is a contiguous
                    // stretch of it, so a run's ends are simply where the
                    // neighbour's session differs. The last row of the list
                    // counts as an end even when its session continues past
                    // it: the bracket has to close where the list stops,
                    // because nothing below is there to close it.
                    view.groupFirst = i == 0 || sessions[i - 1] != a.session
                    view.groupLast = i == sessions.count - 1
                        || sessions[i + 1] != a.session
                    hover.rows.append(view)
                    item.view = view
                    m.addItem(item)
                }
            }
            m.addItem(.separator())
        }

        // One toggle, never both. A second "Mark as working" while a mark is
        // already running just appends an identical open mark -- which is
        // exactly what happened on the first day this shipped: two clicks ten
        // seconds apart, two rows, because nothing in the menu said the first
        // one had taken. Showing only the move that applies makes the state
        // legible and the duplicate unreachable.
        //
        // One action, too: this row and ⌘⌥S are the same decision, so they run
        // the same code and the row advertises the chord. They used to differ
        // -- the row ended the shift now, the chord at the last event -- which
        // made the shortcut impossible to describe in the interface without
        // also explaining that it did something slightly different. The two
        // endings are still both reachable, by name, under End Session below.
        let toggle = NSMenuItem(title: status.state == "marked" ? "Stop working"
                                                                : "Start working",
                                action: #selector(toggleShift), keyEquivalent: "s")
        toggle.keyEquivalentModifierMask = [.command, .option]
        m.addItem(toggle)

        // Directly under the toggle because it is the toggle's other half.
        // "Start working" claims time from the click forward, which is the
        // wrong end of a stretch you have already walked back from: the
        // corridor conversation is over, and the minutes worth claiming are
        // the ones behind you. This claims those -- from where the last
        // session ended up to now -- so the gap closes and the two sessions
        // read as the one stretch they were.
        //
        // The minute is in the title rather than left to be discovered in the
        // period list afterwards. It is a claim on time that cannot be seen
        // being made, so it says how far back it reaches before it reaches.
        //
        // Greyed rather than hidden when there is nothing to link to. Hiding
        // it would make the menu's shape depend on how the morning happened to
        // go, and an item that comes and goes is harder to learn than one that
        // is always in the same place and sometimes dim.
        let link = NSMenuItem(title: status.linkFrom.map { "Link with Last Session (from \($0))" }
                                  ?? "Link with Last Session",
                              action: #selector(linkLastSession), keyEquivalent: "")
        m.addItem(link)

        // Always present, unlike the toggle's stop, which only appears once a
        // mark is running. A mark is not the only thing that keeps the day
        // open -- a meeting runs on the calendar's schedule and prompting
        // lights the dot with nothing marked at all -- so on most afternoons
        // there was no menu item at all that meant "that was the day". This
        // is that item, and it says which minute it means rather than picking
        // for you.
        let endSub = NSMenu()
        for (title, atLast) in [("End Now", false), ("End After Last Entry", true)] {
            let mi = NSMenuItem(title: title, action: #selector(endSession(_:)),
                                keyEquivalent: "")
            mi.representedObject = atLast
            // Set here, not by the sweep at the bottom of build(): that walks
            // m.items, which is the top level only. A submenu item left with a
            // nil target falls back to the responder chain, finds nothing that
            // implements the selector, and AppKit draws the row permanently
            // greyed out -- the menu looks broken rather than acting broken.
            mi.target = self
            endSub.addItem(mi)
        }
        let endHost = NSMenuItem(title: "End Session", action: nil, keyEquivalent: "")
        endHost.submenu = endSub
        m.addItem(endHost)

        if status.why.hasPrefix("in ") {
            m.addItem(NSMenuItem(title: "Meeting ended early",
                                 action: #selector(endMeetingEarly), keyEquivalent: ""))
        }
        // Here as well as on ⌥W, because a shortcut nothing in the interface
        // mentions is a shortcut that is forgotten by the week after it ships.
        m.addItem(NSMenuItem(title: "Log an entry",
                             action: #selector(logEntry), keyEquivalent: "w"))
        m.addItem(NSMenuItem(title: "Refresh now",
                             action: #selector(refreshNow), keyEquivalent: "r"))
        m.addItem(.separator())

        // Two checked rows rather than one "Unfocused ✓" toggle. The mode
        // changes how the whole day is read, so which rule is running has to be
        // legible at a glance and not inferable only from the absence of a
        // mark -- and naming both makes the pair read as a choice between two
        // ways of working instead of a setting that is on or off.
        let modes = NSMenuItem(title: "Tracking", action: nil, keyEquivalent: "")
        modes.isEnabled = false
        m.addItem(modes)
        for (title, name) in [("Focused — 5m cutoff", "focused"),
                              ("Unfocused — 1m, widening to 5m", "unfocused")] {
            let mi = NSMenuItem(title: title, action: #selector(pickMode(_:)),
                                keyEquivalent: "")
            mi.representedObject = name
            mi.state = status.mode == name ? .on : .off
            m.addItem(mi)
        }
        m.addItem(.separator())

        if !periods.isEmpty {
            let sub = NSMenu()
            for p in periods { addPeriodItem(p, to: sub) }
            let host = NSMenuItem(title: "Today's periods (\(periods.count))",
                                  action: nil, keyEquivalent: "")
            host.submenu = sub
            m.addItem(host)
            m.addItem(.separator())
        }

        m.addItem(NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q"))
        for i in m.items where i.action != nil { i.target = self }
    }

    // Fires immediately before the dropdown is drawn. build() is cheap and
    // works off the last poll, so this closes the gap between whenever that
    // poll ran and this click; the refresh lands the genuinely current reading
    // a moment later, and now it lands in a menu that is on screen and live.
    func menuNeedsUpdate(_: NSMenu) {
        build()
        refresh()
    }

    func menuWillOpen(_: NSMenu) { menuIsOpen = true }

    // The panel is a window of our own, so nothing takes it away when the menu
    // goes: without this it would be left floating over the desktop, beside a
    // menu that is no longer there. A row's mouseExited normally closes it
    // first, but a menu dismissed by a click elsewhere or by Escape never
    // delivers one.
    func menuDidClose(_: NSMenu) {
        menuIsOpen = false
        popover.hide()
        hover?.enter(nil)
    }

    // The probe shells out to prompt-count and Slack, so a poll can take a
    // second or two. On the main thread that freezes the menu bar for everyone.
    func refresh() {
        DispatchQueue.global(qos: .utility).async {
            guard let out = runProbe(["status"]),
                  let d = out.data(using: .utf8),
                  let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any]
            else {
                // A poll that could not answer is NOT a reading of "idle".
                // Returning early here left the last dot on screen, so a probe
                // that had been failing since launch showed as a confident
                // hollow amber -- the tracker looked like it was working and
                // reporting an idle day, when in fact it was blind. A broken
                // poll gets its own glyph so it cannot be mistaken for one.
                DispatchQueue.main.async {
                    var s = self.status
                    s.state = "broken"
                    s.why = "probe did not answer"
                    self.apply(s)
                }
                return
            }
            var s = Status()
            s.state = j["state"] as? String ?? "unknown"
            s.why = j["why"] as? String ?? ""
            s.workedMinutes = j["worked_minutes"] as? Int ?? 0
            s.at = j["at"] as? String ?? ""
            s.quietSince = j["quiet_since"] as? String
            s.quietSec = j["quiet_sec"] as? Int
            s.gapAfterSec = j["gap_after_sec"] as? Int
            s.mode = j["mode"] as? String ?? "focused"
            s.focusPct = j["focus_pct"] as? Int
            s.linkFrom = j["link_from"] as? String
            s.periods = (j["periods"] as? [[String: Any]] ?? []).map { p in
                Period(start: p["start"] as? Int ?? 0,
                       end: p["end"] as? Int ?? 0,
                       len: p["len"] as? Int ?? 0,
                       nPrompts: p["n_prompts"] as? Int ?? 0,
                       nSlack: p["n_slack"] as? Int ?? 0,
                       what: p["what"] as? String ?? "",
                       current: p["current"] as? Bool ?? false)
            }
            s.activities = (j["activities"] as? [[String: Any]] ?? []).map { a in
                Activity(t: a["t"] as? String ?? "",
                         at: a["at"] as? TimeInterval ?? 0,
                         kind: a["kind"] as? String ?? "",
                         what: a["what"] as? String ?? "",
                         n: a["n"] as? Int ?? 1,
                         session: a["session"] as? Int)
            }
            s.sessions = (j["sessions"] as? [[String: Any]] ?? []).map { g in
                ActSession(start: g["start"] as? Int ?? 0,
                           end: g["end"] as? Int ?? 0,
                           len: g["len"] as? Int ?? 0,
                           what: g["what"] as? String ?? "",
                           counted: g["counted"] as? Bool ?? true,
                           current: g["current"] as? Bool ?? false,
                           n: g["n"] as? Int ?? 0,
                           // JSON pairs, not a dictionary, because the order is
                           // the payload's: the probe sorts biggest first so
                           // the smallest contributor is what truncates off the
                           // end of the line, and a dictionary would lose that.
                           kinds: (g["kinds"] as? [[Any]] ?? []).compactMap {
                               guard let k = $0.first as? String,
                                     let n = $0.last as? Int else { return nil }
                               return (k, n)
                           },
                           rows: (g["rows"] as? [[String: Any]] ?? []).map { r in
                               SessionRow(t: r["t"] as? String ?? "",
                                          kind: r["kind"] as? String ?? "",
                                          what: r["what"] as? String ?? "",
                                          n: r["n"] as? Int ?? 1)
                           })
            }
            DispatchQueue.main.async { self.apply(s) }
        }
    }

    func apply(_ s: Status) {
        status = s
        lastActivityAt = s.quietSec.map { Date().addingTimeInterval(-Double($0)) }
        blinkOn = true
        switch s.state {
        case "working": item.button?.image = dotImage(WORKING, hollow: false)
        case "marked":  item.button?.image = dotImage(MARKED, hollow: false)
        case "broken":  item.button?.image = dotImage(BROKEN, hollow: false)
        default:        item.button?.image = dotImage(AWAY, hollow: true)
        }
        item.button?.imagePosition = .imageOnly
        item.button?.toolTip = "\(s.state) — \(s.why) (as of \(s.at))"
        build()
    }

    // ⌘⌥S, and the toggle row that names it. Starts a shift, or ends the
    // running one at the last event the probe saw -- not at this keypress: the
    // chord gets pressed on the way out the door, and the minutes between the
    // last prompt and the press are the leaving, not the work. Ending at this
    // minute instead is End Session's "End Now", which is a thing you ask for
    // by name rather than a difference between two ways of doing one thing.
    // Reading the state on main before dispatching keeps the decision and the
    // menu's rendering of it from disagreeing.
    @objc func toggleShift() {
        let running = status.state == "marked"
        DispatchQueue.global(qos: .utility).async {
            _ = runProbe(running ? ["unmark", "last"] : ["mark"])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // ⌥W. One keypress, no window, no question: it records that this minute
    // was worked and nothing else. Deliberately not a text prompt -- the value
    // of the shortcut is that it costs nothing to press mid-thought, and a
    // dialog that takes the caret to ask what you are doing is an interruption
    // of the very work it is trying to record.
    @objc func logEntry() {
        DispatchQueue.global(qos: .utility).async {
            // Only claims what actually happened. runProbe reports its own
            // failure to stderr; a banner saying the minute was logged when
            // the write never landed would be worse than no banner at all,
            // because it is the thing being trusted instead of checking.
            guard runProbe(["note"]) != nil else { return }
            notifyEntryLogged()
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // The clicked item carries the mode name, so both rows share one action and
    // neither can drift from the title beside its own checkmark.
    // Purely a way of looking at what the last poll already returned, so this
    // asks the probe for nothing and rebuilds straight away. The menu closes on
    // the click, so the flipped view is what opens next time.
    @objc func toggleGrouped() {
        grouped.toggle()
        UserDefaults.standard.set(grouped, forKey: "activityGrouped")
        build()
    }

    @objc func pickMode(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        DispatchQueue.global(qos: .utility).async {
            _ = runProbe(["mode", name])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // End Session. Closes whatever is holding the day open -- an open mark, a
    // meeting still running on the calendar's schedule -- at one minute, either
    // this one or the last entry the tracker saw. The probe decides which
    // minute "the last entry" is, using the same rule the shift stop uses, so
    // there is one answer to that question and not two.
    @objc func endSession(_ sender: NSMenuItem) {
        // Logged rather than defaulted: the two rows mean different minutes, so
        // guessing one would silently end the day at a time nobody asked for.
        guard let atLast = sender.representedObject as? Bool else {
            FileHandle.standardError.write(
                "end session: row carried no minute choice\n".data(using: .utf8)!)
            return
        }
        DispatchQueue.global(qos: .utility).async {
            _ = runProbe(atLast ? ["end_session", "last"] : ["end_session"])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // Link with Last Session. Claims the gap between where the last session
    // ended and now as work, and leaves the claim open so the stretch it just
    // rejoined keeps running. Which minute counts as "where the last session
    // ended" is the probe's to decide -- it is the one that drew the period
    // list -- so this passes no minute and cannot name a different one than
    // the title advertised.
    @objc func linkLastSession() {
        DispatchQueue.global(qos: .utility).async {
            _ = runProbe(["link_last"])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // The menu auto-enables, which ignores isEnabled on any item that has an
    // action -- so the link row has to be greyed from here. Everything else
    // keeps the default: items with no action grey themselves, and every other
    // action row applies whenever the menu is open.
    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        item.action == #selector(linkLastSession) ? status.linkFrom != nil : true
    }

    @objc func endMeetingEarly() {
        DispatchQueue.global(qos: .utility).async {
            _ = runProbe(["meeting_end"])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    @objc func refreshNow() { refresh() }
    @objc func quit() { NSApplication.shared.terminate(nil) }
}

let app = NSApplication.shared
// Accessory, so it lives only in the menu bar: no Dock icon, no app switcher
// entry, and it never steals focus.
app.setActivationPolicy(.accessory)
let bar = Bar()
app.delegate = bar
app.run()
