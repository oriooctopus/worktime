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
import Foundation

let PROBE = ("~/.claude/bin/worktime-probe.py" as NSString).expandingTildeInPath
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
// Slack message sent, a permission approval, or a GitHub page read. `n` is
// how many identical consecutive events the probe folded into this one.
struct Activity {
    var t = ""
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

final class PeriodRowView: NSView {
    // `status` is only passed for the current (most recent) period -- it
    // folds "Nm since last activity" / "quiet Nm" into this same row group
    // instead of a separate head line above the whole menu, which said the
    // same thing about the same span twice. It's its own line directly above
    // the period's time range, not appended onto that line, so only the
    // little dot carries color -- the rest reads as plain text, same as
    // everything else in the row.
    init(_ p: Period, width: CGFloat, status: (symbol: String, text: String, color: NSColor)? = nil) {
        let statusHeight: CGFloat = status != nil ? 17 : 0
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 34 + statusHeight))

        var statusField: NSTextField?
        if let s = status {
            let attr = NSMutableAttributedString(string: s.symbol + " ", attributes: [
                .font: NSFont.systemFont(ofSize: 12, weight: .medium),
                .foregroundColor: s.color,
            ])
            attr.append(NSAttributedString(string: s.text, attributes: [
                .font: NSFont.systemFont(ofSize: 12, weight: .medium),
                .foregroundColor: NSColor.labelColor,
            ]))
            statusField = singleLineLabel(attr)
        }

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
        var fields = [topField, whatField]
        if let sf = statusField { fields.insert(sf, at: 0) }
        for f in fields {
            f.translatesAutoresizingMaskIntoConstraints = false
            addSubview(f)
        }

        var constraints: [NSLayoutConstraint] = [
            topField.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            topField.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            whatField.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            whatField.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            whatField.topAnchor.constraint(equalTo: topField.bottomAnchor, constant: 1),
        ]
        if let sf = statusField {
            constraints += [
                sf.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
                sf.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
                sf.topAnchor.constraint(equalTo: topAnchor, constant: 4),
                topField.topAnchor.constraint(equalTo: sf.bottomAnchor, constant: 2),
            ]
        } else {
            constraints.append(topField.topAnchor.constraint(equalTo: topAnchor, constant: 4))
        }
        NSLayoutConstraint.activate(constraints)
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
final class ActivityRowView: NSView {
    init(_ a: Activity, width: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 17))

        let line = NSMutableAttributedString(string: a.t + "  ", attributes: [
            // Monospaced digits so the times form a straight column. The
            // proportional face renders 1 narrower than 8, which is enough to
            // visibly ragged the left edge of a ten-row list.
            .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular),
            .foregroundColor: NSColor.secondaryLabelColor,
        ])
        line.append(NSAttributedString(string: a.kind + "  ", attributes: [
            .font: NSFont.systemFont(ofSize: 11),
            .foregroundColor: NSColor.tertiaryLabelColor,
        ]))
        line.append(NSAttributedString(string: a.what, attributes: [
            .font: NSFont.systemFont(ofSize: 11),
            .foregroundColor: NSColor.labelColor,
        ]))
        // Only when it collapsed something. A "×1" on every other row would be
        // noise standing in for the ordinary case.
        if a.n > 1 {
            line.append(NSAttributedString(string: "  ×\(a.n)", attributes: [
                .font: NSFont.systemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]))
        }

        // Truncated here rather than by the probe: the widget is the only
        // party that knows its own width, and the payload is capped already.
        let f = singleLineLabel(line)
        f.translatesAutoresizingMaskIntoConstraints = false
        addSubview(f)
        NSLayoutConstraint.activate([
            f.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            f.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            f.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }
}

func addPeriodItem(_ p: Period, to menu: NSMenu,
                   status: (symbol: String, text: String, color: NSColor)? = nil) {
    let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    item.view = PeriodRowView(p, width: 300, status: status)
    menu.addItem(item)
}

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
        registerHotKey()
        timer = Timer.scheduledTimer(withTimeInterval: POLL_SEC, repeats: true) { _ in
            self.refresh()
        }
        blinkTimer = Timer.scheduledTimer(withTimeInterval: BLINK_INTERVAL, repeats: true) { _ in
            self.tickBlink()
        }
        // Both timers must run in .common, not the .default mode
        // scheduledTimer gives them. An open menu spins the run loop in
        // .eventTracking, where a .default-mode timer simply does not fire: for
        // as long as the dropdown was up, nothing polled, nothing rebuilt, and
        // the menu could not have updated even once. That is the other half of
        // why the dot looked live and the menu looked frozen -- the dot's
        // updates all landed in the moments the menu was closed.
        for t in [timer, blinkTimer] {
            if let t { RunLoop.main.add(t, forMode: .common) }
        }
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

        // Last 3 periods, richest state a menu-bar dropdown can show without a
        // custom NSView: native title/subtitle rows carrying the time range,
        // duration, activity counts, and the AI summary underneath. Older
        // periods move to a submenu rather than growing this list unbounded --
        // past ~5 rows the dropdown stops being a glance and starts being a log.
        let recent = Array(status.periods.prefix(3))
        let earlier = Array(status.periods.dropFirst(3))

        // Keyed on exactly the strings that get rendered, so a poll that
        // changes nothing visible costs nothing and cannot flicker an open
        // menu. quiet_sec ticking every 5s is deliberately not in here -- the
        // menu shows it rounded to minutes, so it earns a rebuild once a
        // minute, not twelve times.
        let key = ([worked, symbol, status.why, status.state, status.mode]
                   + (recent + earlier).map { p in
                       let s = periodStrings(p)
                       return s.top + "\u{1}" + s.what
                   }
                   + status.activities.map { "\($0.t)\u{1}\($0.kind)\u{1}\($0.what)\u{1}\($0.n)" }
                  ).joined(separator: "\u{2}")
        if key == lastMenuKey { return }
        lastMenuKey = key

        let m = menu
        m.removeAllItems()
        let w = NSMenuItem(title: worked, action: nil, keyEquivalent: "")
        w.isEnabled = false
        m.addItem(w)
        m.addItem(.separator())

        for (i, p) in recent.enumerated() {
            let s = i == 0 ? (symbol, status.why, color) : nil
            addPeriodItem(p, to: m, status: s)
        }
        if !recent.isEmpty { m.addItem(.separator()) }

        // The evidence behind the periods above, above the actions rather than
        // in a submenu. The period rows say how the day was divided; without
        // this, the only answer to "why is it green right now" was the one
        // rounded phrase in the status line, and the only way to check what the
        // tracker had actually seen was to read the dashboard.
        //
        // Ten single-line rows is the one place this menu spends real height,
        // which is why they are single-line and why the older periods stay in
        // their submenu: the list is the log, so the periods don't need to be.
        if !status.activities.isEmpty {
            let head = NSMenuItem(title: "Recent activity", action: nil, keyEquivalent: "")
            head.isEnabled = false
            m.addItem(head)
            for a in status.activities {
                let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
                item.view = ActivityRowView(a, width: 300)
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

        if !earlier.isEmpty {
            let sub = NSMenu()
            for p in earlier { addPeriodItem(p, to: sub) }
            let host = NSMenuItem(title: "Earlier today (\(earlier.count) period\(earlier.count == 1 ? "" : "s"))",
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
