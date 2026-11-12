import AppKit

// MARK: - "Track this as work?"

// Raised once per unruled localhost port or local directory (see
// WorkSources.swift). No timer and no default, for the reasons
// MeetingPromptPanel gives: an unanswered question is neither a yes nor a no,
// and a panel rather than a notification because Focus swallows those.
final class WorkSourcePanel {
    private let panel: NSPanel
    private let source: LocalSource
    private let onYes: (LocalSource) -> Void
    private let onNo: () -> Void

    private(set) var yes: NSButton!
    private(set) var no: NSButton!
    /// The Track pull-down, for a folder only.
    private(set) var trackMenu: NSPopUpButton?
    let titleText: String
    let messageText: String

    init(source: LocalSource, present: Bool = true,
         onYes: @escaping (LocalSource) -> Void, onNo: @escaping () -> Void) {
        self.source = source
        self.onYes = onYes
        self.onNo = onNo
        switch source.kind {
        case .port:
            titleText = "Track \(source.label) as work?"
            messageText = "Chrome is on a localhost port Worktime has not seen."
        case .directory:
            titleText = "Track this folder as work?"
            messageText = source.label
        case .slides:
            titleText = "Track this Google Slides deck as work?"
            messageText = source.label
        }

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

        // A folder gets a pull-down so the answer can be its parent or any
        // other folder; a port or a deck has nothing wider to offer.
        let yes: NSButton
        if source.kind == .directory {
            let menu = NSPopUpButton(frame: .zero, pullsDown: true)
            menu.addItem(withTitle: "Track")
            addChoice(to: menu, title: "This folder", action: #selector(trackThisTapped))
            if let parent = Self.parent(of: source) {
                addChoice(to: menu, title: "Parent: \(parent.label)", action: #selector(trackParentTapped))
            }
            addChoice(to: menu, title: "Choose folder\u{2026}", action: #selector(trackCustomTapped))
            trackMenu = menu
            yes = menu
        } else {
            yes = NSButton(title: "Track", target: nil, action: nil)
            yes.bezelStyle = .rounded
        }
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
        if source.kind != .directory {
            yes.target = self
            yes.action = #selector(yesTapped)
        }
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

    private func addChoice(to menu: NSPopUpButton, title: String, action: Selector) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        menu.menu?.addItem(item)
    }

    /// The folder one level up, or nil at the filesystem root.
    static func parent(of source: LocalSource) -> LocalSource? {
        let up = URL(string: source.value)!.deletingLastPathComponent()
        guard up.path != "/" else { return nil }
        return LocalSource(kind: .directory, value: up.absoluteString)
    }

    @objc private func yesTapped() {
        close()
        onYes(source)
    }

    @objc private func trackThisTapped() { yesTapped() }

    @objc private func trackParentTapped() {
        close()
        onYes(Self.parent(of: source)!)
    }

    @objc private func trackCustomTapped() {
        let chooser = NSOpenPanel()
        chooser.canChooseFiles = false
        chooser.canChooseDirectories = true
        chooser.allowsMultipleSelection = false
        chooser.directoryURL = URL(string: source.value)
        NSApp.activate(ignoringOtherApps: true)
        guard chooser.runModal() == .OK, let url = chooser.url else { return }
        var value = url.absoluteString
        if !value.hasSuffix("/") { value += "/" }
        close()
        onYes(LocalSource(kind: .directory, value: value))
    }

    @objc private func noTapped() {
        close()
        onNo()
    }

    func close() {
        panel.orderOut(nil)
    }
}
