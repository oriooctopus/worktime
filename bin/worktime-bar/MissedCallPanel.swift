import AppKit

// MARK: - "Were you on this call?"

struct MissedCall: Equatable {
    let blockStart: String
    let blockEnd: String
    let start: String
    let end: String
    let app: String
}

// Raised after a busy block ends with a meeting app in front during it and no
// meeting recorded: a listen-only or muted call the microphone never saw. The
// times are only a guess -- the app coming up and the block ending -- so both
// are editable, and a Yes records whatever the fields say.
//
// No timer and no default, for the reason MeetingPromptPanel gives.
final class MissedCallPanel {
    let call: MissedCall
    private let panel: NSPanel
    private let onYes: (String, String) -> Void
    private let onNo: () -> Void

    private(set) var startField: NSTextField!
    private(set) var endField: NSTextField!
    private(set) var yes: NSButton!
    private(set) var no: NSButton!
    private let error = NSTextField(labelWithString: "")
    var errorText: String { error.stringValue }

    init(call: MissedCall, present: Bool = true,
         onYes: @escaping (String, String) -> Void, onNo: @escaping () -> Void) {
        self.call = call
        self.onYes = onYes
        self.onNo = onNo

        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 140),
                        styleMask: [.titled, .nonactivatingPanel, .fullSizeContentView],
                        backing: .buffered, defer: false)
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isMovableByWindowBackground = true
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let title = NSTextField(labelWithString: "Were you on this call?")
        title.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        let message = NSTextField(
            labelWithString: "\(call.app) was open during your \(call.blockStart)–\(call.blockEnd) block")
        message.font = NSFont.systemFont(ofSize: 12)
        message.textColor = .secondaryLabelColor
        message.lineBreakMode = .byTruncatingTail

        let from = NSTextField(labelWithString: "From")
        let to = NSTextField(labelWithString: "to")
        let startField = NSTextField(string: call.start)
        let endField = NSTextField(string: call.end)
        for f in [startField, endField] {
            f.alignment = .center
            f.widthAnchor.constraint(equalToConstant: 56).isActive = true
        }
        error.font = NSFont.systemFont(ofSize: 11)
        error.textColor = .systemRed

        let yes = NSButton(title: "Yes", target: nil, action: nil)
        yes.bezelStyle = .rounded
        let no = NSButton(title: "No", target: nil, action: nil)
        no.bezelStyle = .rounded

        let content = panel.contentView!
        for v in [title, message, from, startField, to, endField, error, yes, no] {
            v.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(v)
        }
        NSLayoutConstraint.activate([
            title.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
            title.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),
            message.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            message.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -18),
            message.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 4),

            from.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            from.centerYAnchor.constraint(equalTo: startField.centerYAnchor),
            startField.leadingAnchor.constraint(equalTo: from.trailingAnchor, constant: 6),
            startField.topAnchor.constraint(equalTo: message.bottomAnchor, constant: 10),
            to.leadingAnchor.constraint(equalTo: startField.trailingAnchor, constant: 6),
            to.centerYAnchor.constraint(equalTo: startField.centerYAnchor),
            endField.leadingAnchor.constraint(equalTo: to.trailingAnchor, constant: 6),
            endField.centerYAnchor.constraint(equalTo: startField.centerYAnchor),

            error.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            error.centerYAnchor.constraint(equalTo: yes.centerYAnchor),
            yes.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            yes.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -14),
            no.trailingAnchor.constraint(equalTo: yes.leadingAnchor, constant: -8),
            no.bottomAnchor.constraint(equalTo: yes.bottomAnchor),
        ])
        self.startField = startField
        self.endField = endField
        self.yes = yes
        self.no = no
        yes.target = self
        yes.action = #selector(yesTapped)
        no.target = self
        no.action = #selector(noTapped)

        if let screen = NSScreen.main {
            let visible = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: visible.maxX - panel.frame.width - 16,
                                         y: visible.maxY - panel.frame.height - 16))
        }
        if present {
            panel.orderFrontRegardless()
        }
    }

    /// Minutes of day for a typed "H:MM" or "HH:MM", nil for anything else.
    static func minutes(_ s: String) -> Int? {
        let parts = s.trimmingCharacters(in: .whitespaces).split(separator: ":")
        guard parts.count == 2, parts[1].count == 2,
              let h = Int(parts[0]), let m = Int(parts[1]),
              (0..<24).contains(h), (0..<60).contains(m) else { return nil }
        return h * 60 + m
    }

    @objc private func yesTapped() {
        guard let s = Self.minutes(startField.stringValue),
              let e = Self.minutes(endField.stringValue) else {
            error.stringValue = "Times must be HH:MM"
            return
        }
        guard s < e else {
            error.stringValue = "End must be after start"
            return
        }
        close()
        onYes(String(format: "%02d:%02d", s / 60, s % 60),
              String(format: "%02d:%02d", e / 60, e % 60))
    }

    @objc private func noTapped() {
        close()
        onNo()
    }

    func close() {
        panel.orderOut(nil)
    }
}
