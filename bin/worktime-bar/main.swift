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
    var periods: [Period] = []
    var activities: [Activity] = []
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

// A row is written when the frontmost app changes, and otherwise every
// FOCUS_HEARTBEAT_SEC. The heartbeat is what makes an interrupted span
// truncate honestly: the probe credits the stretch between consecutive rows
// only while they stay close together, so a machine that sleeps for two hours
// leaves a two-hour hole between rows and earns nothing for it. Without the
// heartbeat a single row would sit there claiming the whole absence -- the
// same trap `visit_duration` falls into in the Chrome exporter.
let FOCUS_HEARTBEAT_SEC = 30.0


final class FocusLog {
    private var lastBundle: String?
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
        let changed = bundle != lastBundle
        let due = now.timeIntervalSince(lastWrite) >= FOCUS_HEARTBEAT_SEC
        guard changed || due else { return }

        let day = Self.dayfmt.string(from: now)
        let row: [String: Any] = [
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
        guard let data = try? JSONSerialization.data(withJSONObject: row),
              var line = String(data: data, encoding: .utf8)
        else { return }
        line += "\n"
        write(line, day: day)
        lastBundle = bundle
        lastWrite = now
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

func human(_ m: Int) -> String {
    m >= 60 ? "\(m / 60)h \(m % 60)m" : "\(m)m"
}

// Periods carry minute-of-day integers, not wall-clock strings -- the probe
// publishes them that way so the widget can do arithmetic on them too.
func hhmm(_ m: Int) -> String {
    String(format: "%02d:%02d", m / 60, m % 60)
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

final class ActivityRowView: NSView {
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
        // was rounded from stays reachable.
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
            ageField.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
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

    required init?(coder: NSCoder) { fatalError("not used") }
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

final class Bar: NSObject, NSApplicationDelegate, NSMenuDelegate {
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

    // The Carbon handler is a bare C function pointer and cannot capture, so
    // it reaches the app through the `bar` global rather than through self.
    // It fires on the event thread; the toggle reads status and rebuilds the
    // menu, so it hops to main first.
    func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ -> OSStatus in
            DispatchQueue.main.async { bar.toggleShift() }
            return noErr
        }, 1, &spec, nil, nil)
        // Logged because a hot key that fails to register fails silently and
        // invisibly: the shortcut simply does nothing, which is indistinguishable
        // from the app being down or the probe erroring.
        let err = RegisterEventHotKey(HOTKEY_CODE, HOTKEY_MODS,
                                      EventHotKeyID(signature: OSType(0x574B_5453), id: 1),
                                      GetApplicationEventTarget(), 0, &hotKeyRef)
        FileHandle.standardError.write("hotkey cmd+opt+S register -> \(err)\n".data(using: .utf8)!)
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
        let key = ([worked, symbol, status.why, status.state, status.mode]
                   + periods.map { p in
                       let s = periodStrings(p)
                       return s.top + "\u{1}" + s.what
                   }
                   + zip(status.activities, ages).map { a, age in
                       "\(age)\u{1}\(a.kind)\u{1}\(a.what)\u{1}\(a.n)"
                   }
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
                                text: status.why, color: color)
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
        if !status.activities.isEmpty {
            let head = NSMenuItem(title: "Recent activity", action: nil, keyEquivalent: "")
            head.isEnabled = false
            m.addItem(head)
            let ageWidth = columnWidth(ages)
            let kindWidth = columnWidth(status.activities.map(\.kind))
            for (a, age) in zip(status.activities, ages) {
                let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
                item.view = ActivityRowView(a, width: 300, age: age,
                                            ageWidth: ageWidth, kindWidth: kindWidth)
                m.addItem(item)
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
        // The stop here is deliberately not the one ⌘⌥S performs. Clicking it
        // is a decision made now, so it ends the shift now; the hotkey is
        // pressed while standing up and ends it at the last event instead.
        if status.state == "marked" {
            m.addItem(NSMenuItem(title: "Stop working (end now)",
                                 action: #selector(unmark), keyEquivalent: "m"))
        } else {
            m.addItem(NSMenuItem(title: "Start working",
                                 action: #selector(mark), keyEquivalent: "m"))
        }
        if status.why.hasPrefix("in ") {
            m.addItem(NSMenuItem(title: "Meeting ended early",
                                 action: #selector(endMeetingEarly), keyEquivalent: ""))
        }
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
                         n: a["n"] as? Int ?? 1)
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

    @objc func mark() {
        DispatchQueue.global(qos: .utility).async {
            _ = runProbe(["mark"])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    @objc func unmark() {
        DispatchQueue.global(qos: .utility).async {
            _ = runProbe(["unmark"])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // ⌘⌥S. Starts a shift, or ends the running one at the last event the probe
    // saw -- not at this keypress. Reading the state on main before dispatching
    // keeps the decision and the menu's rendering of it from disagreeing.
    @objc func toggleShift() {
        let running = status.state == "marked"
        DispatchQueue.global(qos: .utility).async {
            _ = runProbe(running ? ["unmark", "last"] : ["mark"])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // The clicked item carries the mode name, so both rows share one action and
    // neither can drift from the title beside its own checkmark.
    @objc func pickMode(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        DispatchQueue.global(qos: .utility).async {
            _ = runProbe(["mode", name])
            DispatchQueue.main.async { self.refresh() }
        }
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
