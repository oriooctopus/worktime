import AppKit

// MARK: - Work buckets: what the menu and the panels know about them

// The probe owns every figure here (see "Work buckets" in worktime-probe.py);
// this file only draws them. Nothing below adds, discounts or compares times
// beyond what is needed to lay a bar out.

let SPECIAL = NSColor(srgbRed: 0.639, green: 0.443, blue: 0.969, alpha: 1)  // #a371f7
// The same green as WORKING in main.swift, repeated here because a file other
// than main.swift cannot rely on main.swift's top-level values being
// initialised yet.
let MAIN_GREEN = NSColor(srgbRed: 0.098, green: 0.620, blue: 0.439, alpha: 1)

struct SpecialTarget {
    var targetSec = 0
    var days = 1
    var endDay = ""
    var doneSec = 0
    var remainingSec = 0
}

struct SpecialInfo {
    var on = false
    var sec = 0
    var target: SpecialTarget?
    // The probe's rule for showing the Special bar at all: a target, or some
    // special time. A day with neither has nothing to say about it.
    var visible = false
}

struct BucketInfo {
    var mainCreditedSec = 0
    var mainTargetSec = mainBaseTargetSec
    var meetingRawSec = 0
    var meetingCreditedSec = 0
    var special = SpecialInfo()
    var weekCreditedSec = 0
    var weekTargetSec = 0
    // The goals of the week's days up to today: where the bar would be had
    // every goal so far been met.
    var weekPaceSec = 0
}

func parseBuckets(_ j: [String: Any]) -> BucketInfo {
    var b = BucketInfo()
    let main = j["main"] as! [String: Any]
    b.mainCreditedSec = main["credited_sec"] as! Int
    b.mainTargetSec = main["target_sec"] as! Int
    let week = j["week"] as! [String: Any]
    b.weekCreditedSec = week["credited_sec"] as! Int
    b.weekTargetSec = week["target_sec"] as! Int
    b.weekPaceSec = week["pace_sec"] as! Int
    let meetings = j["meetings"] as! [String: Any]
    b.meetingRawSec = meetings["raw_sec"] as! Int
    b.meetingCreditedSec = meetings["credited_sec"] as! Int
    let sp = j["special"] as! [String: Any]
    b.special.on = sp["on"] as! Bool
    b.special.sec = sp["sec"] as! Int
    b.special.visible = sp["visible"] as! Bool
    if let t = sp["target"] as? [String: Any] {
        b.special.target = SpecialTarget(
            targetSec: t["target_sec"] as! Int, days: t["days"] as! Int,
            endDay: t["end_day"] as! String, doneSec: t["done_sec"] as! Int,
            remainingSec: t["remaining_sec"] as! Int)
    }
    return b
}

/// Main's standing daily target, before any of a special target is set
/// against it. Mirrors MAIN_TARGET_SEC in the probe.
let mainBaseTargetSec = 4 * 3600

// "1h24m", "36m" -- the same shape the rest of the menu uses for a duration.
func hm(_ sec: Int) -> String {
    let m = sec / 60
    return m >= 60 ? "\(m / 60)h\(String(format: "%02d", m % 60))m" : "\(m)m"
}

// "1:24:10", for a clock that is running.
func hms(_ sec: Int) -> String {
    String(format: "%d:%02d:%02d", sec / 3600, sec / 60 % 60, sec % 60)
}

// MARK: - The menu's bucket rows

// One row per bucket, drawn as a bar. A custom view for the reason every rich
// row in this menu is one: AppKit dims a menu item's own title through the
// vibrancy pass, and a bar cannot be a title at all.
//
// Main's bar is solid for time credited in full and hatched for the tail that
// is meeting time at its discounted rate, and stops at the target tick. Past
// the target the bar is full and the right-hand figure says how far over.
final class BucketRowView: NSView {
    private let fraction: CGFloat
    private let hatchedShare: CGFloat
    private let color: NSColor
    private let hasBar: Bool

    /// `fraction` is credited / target, unclamped; `hatchedShare` is the part
    /// of the filled bar that is discounted meeting time, 0...1.
    init(width: CGFloat, title: String, titleColor: NSColor, value: String,
         dim: String, fraction: CGFloat?, hatchedShare: CGFloat, color: NSColor,
         captionLeft: String?, captionRight: String?) {
        self.fraction = min(max(fraction ?? 0, 0), 1)
        self.hatchedShare = hatchedShare
        self.color = color
        self.hasBar = fraction != nil
        let height: CGFloat = (fraction != nil ? 36 : 22) + (captionLeft != nil ? 16 : 0)
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height))

        let head = NSMutableAttributedString(string: title, attributes: [
            .font: NSFont.systemFont(ofSize: 13, weight: .semibold),
            .foregroundColor: titleColor,
        ])
        let headField = singleLineLabel(head)
        let tail = NSMutableAttributedString(string: value, attributes: [
            .font: NSFont.systemFont(ofSize: 13),
            .foregroundColor: NSColor.labelColor,
        ])
        if !dim.isEmpty {
            tail.append(NSAttributedString(string: " " + dim, attributes: [
                .font: NSFont.systemFont(ofSize: 13),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]))
        }
        let tailField = singleLineLabel(tail)
        tailField.alignment = .right
        for v in [headField, tailField] {
            v.translatesAutoresizingMaskIntoConstraints = false
            addSubview(v)
        }
        NSLayoutConstraint.activate([
            headField.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            headField.topAnchor.constraint(equalTo: topAnchor, constant: 3),
            tailField.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            tailField.leadingAnchor.constraint(greaterThanOrEqualTo: headField.trailingAnchor, constant: 8),
            tailField.centerYAnchor.constraint(equalTo: headField.centerYAnchor),
        ])
        headField.setContentCompressionResistancePriority(.required, for: .horizontal)

        if let left = captionLeft {
            let l = singleLineLabel(NSMutableAttributedString(string: left, attributes: [
                .font: NSFont.systemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]))
            let r = singleLineLabel(NSMutableAttributedString(string: captionRight ?? "", attributes: [
                .font: NSFont.systemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]))
            r.alignment = .right
            for v in [l, r] {
                v.translatesAutoresizingMaskIntoConstraints = false
                addSubview(v)
            }
            NSLayoutConstraint.activate([
                l.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14 + 13),
                l.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -3),
                r.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
                r.leadingAnchor.constraint(greaterThanOrEqualTo: l.trailingAnchor, constant: 8),
                r.centerYAnchor.constraint(equalTo: l.centerYAnchor),
            ])
        }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard hasBar else { return }
        let x: CGFloat = 14
        let w = bounds.width - 28
        let barH: CGFloat = 5
        // Below the title line, above the caption when there is one.
        let barY = bounds.height - 3 - 17 - barH - 3
        let track = NSRect(x: x, y: barY, width: w, height: barH)
        NSColor.labelColor.withAlphaComponent(0.14).setFill()
        NSBezierPath(roundedRect: track, xRadius: 2.5, yRadius: 2.5).fill()

        let filled = NSRect(x: x, y: barY, width: w * fraction, height: barH)
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(roundedRect: filled, xRadius: 2.5, yRadius: 2.5).addClip()
        let solidW = filled.width * (1 - hatchedShare)
        color.setFill()
        NSRect(x: x, y: barY, width: solidW, height: barH).fill()
        if hatchedShare > 0 {
            let hatch = NSRect(x: x + solidW, y: barY, width: filled.width - solidW, height: barH)
            color.withAlphaComponent(0.35).setFill()
            hatch.fill()
            // Diagonal strokes over the dimmer fill: the discount reads as
            // texture rather than as a second colour to learn.
            color.setStroke()
            let stripe = NSBezierPath()
            stripe.lineWidth = 1.5
            var sx = hatch.minX - barH
            while sx < hatch.maxX {
                stripe.move(to: NSPoint(x: sx, y: hatch.minY))
                stripe.line(to: NSPoint(x: sx + barH, y: hatch.maxY))
                sx += 6
            }
            NSBezierPath(rect: hatch).setClip()
            stripe.stroke()
        }
        NSGraphicsContext.restoreGraphicsState()

        // The target tick, at the right end of the track.
        NSColor.labelColor.withAlphaComponent(0.6).setFill()
        NSRect(x: x + w - 1, y: barY - 2, width: 2, height: barH + 4).fill()
    }
}

// The week as one line: label, a thin bar and "done / goal". The dashed tick is
// where the bar would be had every goal so far this week been met; it is left
// off when no day has had a goal yet.
final class WeekRowView: NSView {
    private let fraction: CGFloat
    private let paceFraction: CGFloat?

    init(width: CGFloat, credited: Int, target: Int, pace: Int) {
        self.fraction = min(max(CGFloat(credited) / CGFloat(target), 0), 1)
        self.paceFraction = pace > 0 ? min(CGFloat(pace) / CGFloat(target), 1) : nil
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 20))

        let font = NSFont.systemFont(ofSize: 11)
        let label = singleLineLabel(NSMutableAttributedString(string: "Week", attributes: [
            .font: font, .foregroundColor: NSColor.secondaryLabelColor]))
        let tail = NSMutableAttributedString(string: hm(credited), attributes: [
            .font: font, .foregroundColor: NSColor.labelColor])
        tail.append(NSAttributedString(string: " / \(hm(target))", attributes: [
            .font: font, .foregroundColor: NSColor.secondaryLabelColor]))
        let value = singleLineLabel(tail)
        value.alignment = .right
        for v in [label, value] {
            v.translatesAutoresizingMaskIntoConstraints = false
            addSubview(v)
        }
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 14),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            value.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            value.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private static let barX: CGFloat = 14 + 38
    private static let valueW: CGFloat = 92

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let x = Self.barX
        let w = bounds.width - x - 14 - Self.valueW - 8
        let barH: CGFloat = 4
        let barY = (bounds.height - barH) / 2
        NSColor.labelColor.withAlphaComponent(0.14).setFill()
        NSBezierPath(roundedRect: NSRect(x: x, y: barY, width: w, height: barH),
                     xRadius: 2, yRadius: 2).fill()
        MAIN_GREEN.setFill()
        NSBezierPath(roundedRect: NSRect(x: x, y: barY, width: w * fraction, height: barH),
                     xRadius: 2, yRadius: 2).fill()
        if let p = paceFraction {
            let tick = NSBezierPath()
            let tx = min(x + w * p, x + w - 0.75)
            tick.move(to: NSPoint(x: tx, y: barY - 3))
            tick.line(to: NSPoint(x: tx, y: barY + barH + 3))
            tick.lineWidth = 1.5
            tick.setLineDash([2, 1.5], count: 2, phase: 0)
            NSColor.labelColor.withAlphaComponent(0.55).setStroke()
            tick.stroke()
        }
    }
}

// MARK: - Special reminder panel

// Every five minutes while special time is on. A panel and not a notification
// for the reasons CountdownPanel gives: Focus suppresses notifications, and
// special time is exactly when somebody has it on. It shows a live clock and
// the target's progress, offers End special, and withdraws itself after
// AUTOHIDE_SEC with a hairline along the bottom counting that down.
final class SpecialReminderPanel {
    static let AUTOHIDE_SEC = 5

    private let panel: NSPanel
    private let clockLabel = NSTextField(labelWithString: "")
    private let barFill = NSView()
    private let hairline = NSView()
    private let baseSec: Int
    private let shownAt = Date()
    private var timer: Timer?
    private let onEnd: () -> Void
    private(set) var endButton: NSButton!
    var clockText: String { clockLabel.stringValue }

    init(special: SpecialInfo, present: Bool = true, onEnd: @escaping () -> Void) {
        baseSec = special.sec
        self.onEnd = onEnd
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 118),
                        styleMask: [.titled, .nonactivatingPanel, .fullSizeContentView],
                        backing: .buffered, defer: false)
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isMovableByWindowBackground = true
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let content = panel.contentView!
        content.wantsLayer = true

        let title = NSTextField(labelWithString: "SPECIAL TIME")
        title.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        title.textColor = SPECIAL
        clockLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 20, weight: .semibold)
        clockLabel.alignment = .right

        let accent = NSView()
        accent.wantsLayer = true
        accent.layer?.backgroundColor = SPECIAL.cgColor

        let track = NSView()
        track.wantsLayer = true
        track.layer?.backgroundColor = NSColor.labelColor.withAlphaComponent(0.14).cgColor
        track.layer?.cornerRadius = 2
        barFill.wantsLayer = true
        barFill.layer?.backgroundColor = SPECIAL.cgColor
        barFill.layer?.cornerRadius = 2
        track.addSubview(barFill)

        var left = "no target set"
        var right = ""
        var share: CGFloat = 0
        if let t = special.target {
            share = min(CGFloat(t.doneSec) / CGFloat(t.targetSec), 1)
            left = "\(Int((share * 100).rounded()))% of \(hm(t.targetSec)) target"
            right = "\(hm(t.remainingSec)) left"
        }
        let leftLabel = NSTextField(labelWithString: left)
        let rightLabel = NSTextField(labelWithString: right)
        for l in [leftLabel, rightLabel] {
            l.font = NSFont.systemFont(ofSize: 12)
            l.textColor = .secondaryLabelColor
        }
        rightLabel.alignment = .right

        let end = NSButton(title: "End special", target: self, action: #selector(endTapped))
        end.bezelStyle = .rounded
        end.controlSize = .small
        endButton = end

        hairline.wantsLayer = true
        hairline.layer?.backgroundColor = NSColor.labelColor.withAlphaComponent(0.35).cgColor

        for v in [accent, title, clockLabel, track, leftLabel, rightLabel, end, hairline] {
            v.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(v)
        }
        NSLayoutConstraint.activate([
            accent.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            accent.topAnchor.constraint(equalTo: content.topAnchor),
            accent.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            accent.widthAnchor.constraint(equalToConstant: 4),

            title.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
            title.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),
            clockLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            clockLabel.centerYAnchor.constraint(equalTo: title.centerYAnchor),

            track.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            track.trailingAnchor.constraint(equalTo: clockLabel.trailingAnchor),
            track.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 12),
            track.heightAnchor.constraint(equalToConstant: 4),

            leftLabel.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            leftLabel.topAnchor.constraint(equalTo: track.bottomAnchor, constant: 4),
            rightLabel.trailingAnchor.constraint(equalTo: clockLabel.trailingAnchor),
            rightLabel.centerYAnchor.constraint(equalTo: leftLabel.centerYAnchor),

            end.trailingAnchor.constraint(equalTo: clockLabel.trailingAnchor),
            end.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -10),

            hairline.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            hairline.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            hairline.heightAnchor.constraint(equalToConstant: 2),
        ])
        let hairWidth = hairline.widthAnchor.constraint(equalTo: content.widthAnchor, multiplier: 1)
        hairWidth.isActive = true
        // The fill's width is a fixed share of the track; laid out after the
        // track has a size.
        barFill.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            barFill.leadingAnchor.constraint(equalTo: track.leadingAnchor),
            barFill.topAnchor.constraint(equalTo: track.topAnchor),
            barFill.bottomAnchor.constraint(equalTo: track.bottomAnchor),
            barFill.widthAnchor.constraint(equalTo: track.widthAnchor, multiplier: max(share, 0.001)),
        ])

        redraw()
        if let screen = NSScreen.main {
            let v = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: v.maxX - panel.frame.width - 16,
                                         y: v.maxY - panel.frame.height - 16))
        }
        if present { panel.orderFrontRegardless() }

        let t = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
        // .common: an open menu spins the run loop in .eventTracking.
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private func redraw() {
        let elapsed = Int(Date().timeIntervalSince(shownAt))
        clockLabel.stringValue = hms(baseSec + elapsed)
    }

    private func tick() {
        let elapsed = Int(Date().timeIntervalSince(shownAt))
        if elapsed >= SpecialReminderPanel.AUTOHIDE_SEC { close(); return }
        redraw()
        let remaining = 1 - CGFloat(elapsed) / CGFloat(SpecialReminderPanel.AUTOHIDE_SEC)
        if let w = hairline.superview?.bounds.width {
            hairline.layer?.frame.size.width = w * remaining
        }
    }

    @objc private func endTapped() {
        close()
        onEnd()
    }

    func close() {
        timer?.invalidate()
        timer = nil
        panel.orderOut(nil)
    }
}

// MARK: - "Main or Special?"

// Raised when a call starts while special time is on. Two cards, each saying
// what choosing it does. Special is the default and is what unanswered means
// -- all wall-clock time between on and off is special, and Main is the
// exception that has to be asked for -- so the panel picks it itself after
// TIMEOUT_SEC rather than waiting forever behind a call.
final class MeetingRoutePanel {
    static let TIMEOUT_SEC = 60

    private let panel: NSPanel
    private let footer = NSTextField(labelWithString: "")
    private var remaining = MeetingRoutePanel.TIMEOUT_SEC
    private var timer: Timer?
    private let onChoose: (String) -> Void
    private var done = false
    private(set) var mainButton: NSButton!
    private(set) var specialButton: NSButton!

    init(meeting: String, mainNote: String, present: Bool = true,
         onChoose: @escaping (String) -> Void) {
        self.onChoose = onChoose
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 150),
                        styleMask: [.titled, .nonactivatingPanel, .fullSizeContentView],
                        backing: .buffered, defer: false)
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isMovableByWindowBackground = true
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let title = NSTextField(labelWithString: "\(meeting) started")
        title.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        title.lineBreakMode = .byTruncatingTail
        let sub = NSTextField(labelWithString: "Special is on. Where should this meeting count?")
        sub.font = NSFont.systemFont(ofSize: 12)
        sub.textColor = .secondaryLabelColor

        let main = NSButton(title: "Main  (M)", target: self, action: #selector(mainTapped))
        main.bezelStyle = .rounded
        main.keyEquivalent = "m"
        let special = NSButton(title: "Special  (S)", target: self, action: #selector(specialTapped))
        special.bezelStyle = .rounded
        special.keyEquivalent = "s"
        special.contentTintColor = SPECIAL
        mainButton = main
        specialButton = special

        let mainCard = MeetingRoutePanel.card(button: main, color: MAIN_GREEN, note: mainNote)
        let specialCard = MeetingRoutePanel.card(button: special, color: SPECIAL,
                                                 note: "Stays in special, full time. Default.")

        footer.font = NSFont.systemFont(ofSize: 11)
        footer.textColor = .secondaryLabelColor

        let cards = NSStackView(views: [mainCard, specialCard])
        cards.orientation = .horizontal
        cards.distribution = .fillEqually
        cards.spacing = 10

        let content = panel.contentView!
        for v in [title, sub, cards, footer] {
            v.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(v)
        }
        NSLayoutConstraint.activate([
            title.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
            title.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -18),
            title.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),
            sub.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            sub.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 3),
            cards.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            cards.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            cards.topAnchor.constraint(equalTo: sub.bottomAnchor, constant: 10),
            footer.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            footer.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -10),
        ])

        redraw()
        if let screen = NSScreen.main {
            let v = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: v.maxX - panel.frame.width - 16,
                                         y: v.maxY - panel.frame.height - 16))
        }
        if present { panel.orderFrontRegardless() }

        let t = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private static func card(button: NSButton, color: NSColor, note: String) -> NSView {
        let box = NSBox()
        box.boxType = .custom
        box.borderColor = color.withAlphaComponent(0.6)
        box.borderWidth = 1
        box.cornerRadius = 9
        box.fillColor = color.withAlphaComponent(0.10)
        box.contentViewMargins = NSSize(width: 8, height: 6)
        let label = NSTextField(wrappingLabelWithString: note)
        label.font = NSFont.systemFont(ofSize: 11)
        label.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [button, label])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        box.contentView = stack
        return box
    }

    private func redraw() {
        footer.stringValue = "Picks Special in \(remaining)s if you do nothing."
    }

    private func tick() {
        remaining -= 1
        if remaining > 0 { redraw(); return }
        choose("special")
    }

    @objc private func mainTapped() { choose("main") }
    @objc private func specialTapped() { choose("special") }

    private func choose(_ to: String) {
        guard !done else { return }
        done = true
        close()
        onChoose(to)
    }

    func close() {
        timer?.invalidate()
        timer = nil
        panel.orderOut(nil)
    }
}
