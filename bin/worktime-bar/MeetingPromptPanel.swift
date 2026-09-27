import AppKit

// MARK: - "Are you in a meeting?"

// Raised when the microphone has been in use for a minute, and the only way a
// meeting gets recorded. The microphone cannot tell a work call from a personal
// one, a dictation session, or whatever held it at 22:36 on 09-24 with nothing
// open, so it only ever raises the question.
//
// No timer and no default: it stays up until one of the buttons is pressed. An
// unanswered question is not a yes, and a panel that disappeared on its own
// would record nothing while someone who meant to press Yes was still on the
// call. A panel rather than a notification for the reasons CountdownPanel gives:
// Focus swallows notifications and is usually on during a meeting.
final class MeetingPromptPanel {
    private let panel: NSPanel
    private let message = NSTextField(labelWithString: "")
    private let onYes: () -> Void
    private let onNo: () -> Void

    private(set) var yes: NSButton!
    private(set) var no: NSButton!
    var messageText: String { message.stringValue }

    init(began: Date, present: Bool = true,
         onYes: @escaping () -> Void, onNo: @escaping () -> Void) {
        self.onYes = onYes
        self.onNo = onNo

        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 112),
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

        let title = NSTextField(labelWithString: "Are you in a meeting?")
        title.font = NSFont.systemFont(ofSize: 13, weight: .semibold)

        message.font = NSFont.systemFont(ofSize: 12)
        message.textColor = .secondaryLabelColor
        message.lineBreakMode = .byTruncatingTail

        let yes = NSButton(title: "Yes", target: nil, action: nil)
        yes.bezelStyle = .rounded
        let no = NSButton(title: "No", target: nil, action: nil)
        no.bezelStyle = .rounded

        let content = panel.contentView!
        for v in [title, message, yes, no] {
            v.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(v)
        }
        NSLayoutConstraint.activate([
            title.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
            title.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),

            message.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            message.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -18),
            message.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 4),

            yes.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            yes.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -14),
            no.trailingAnchor.constraint(equalTo: yes.leadingAnchor, constant: -8),
            no.bottomAnchor.constraint(equalTo: yes.bottomAnchor),
        ])
        self.yes = yes
        self.no = no
        yes.target = self
        yes.action = #selector(yesTapped)
        no.target = self
        no.action = #selector(noTapped)

        update(began: began, ended: nil)
        if let screen = NSScreen.main {
            let visible = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: visible.maxX - panel.frame.width - 16,
                                         y: visible.maxY - panel.frame.height - 16))
        }
        if present {
            panel.orderFrontRegardless()
        }
    }

    /// Say which stretch a Yes would record: still running, or already over.
    func update(began: Date, ended: Date?) {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        message.stringValue = ended.map {
            "Mic was in use \(f.string(from: began))–\(f.string(from: $0))"
        } ?? "Mic in use since \(f.string(from: began))"
    }

    @objc private func yesTapped() {
        close()
        onYes()
    }

    @objc private func noTapped() {
        close()
        onNo()
    }

    func close() {
        panel.orderOut(nil)
    }
}
