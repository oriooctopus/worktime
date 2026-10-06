import AppKit

// MARK: - "Track this as work?"

// Raised once per unruled localhost port or local directory (see
// WorkSources.swift). No timer and no default, for the reasons
// MeetingPromptPanel gives: an unanswered question is neither a yes nor a no,
// and a panel rather than a notification because Focus swallows those.
final class WorkSourcePanel {
    private let panel: NSPanel
    private let onYes: () -> Void
    private let onNo: () -> Void

    private(set) var yes: NSButton!
    private(set) var no: NSButton!
    let titleText: String
    let messageText: String

    init(source: LocalSource, present: Bool = true,
         onYes: @escaping () -> Void, onNo: @escaping () -> Void) {
        self.onYes = onYes
        self.onNo = onNo
        titleText = "Track \(source.kind == .port ? source.label : "this folder") as work?"
        messageText = source.kind == .port
            ? "Chrome is on a localhost port Worktime has not seen."
            : source.label

        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 400, height: 112),
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

        let title = NSTextField(labelWithString: titleText)
        title.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        let message = NSTextField(labelWithString: messageText)
        message.font = NSFont.systemFont(ofSize: 12)
        message.textColor = .secondaryLabelColor
        message.lineBreakMode = .byTruncatingMiddle

        let yes = NSButton(title: "Track", target: nil, action: nil)
        yes.bezelStyle = .rounded
        let no = NSButton(title: "Not work", target: nil, action: nil)
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

        if let screen = NSScreen.main {
            let visible = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: visible.maxX - panel.frame.width - 16,
                                         y: visible.maxY - panel.frame.height - 16))
        }
        if present {
            panel.orderFrontRegardless()
        }
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
