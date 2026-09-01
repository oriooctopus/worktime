import AppKit

// MARK: - The countdown panel

// A panel rather than a real notification, for three reasons that all showed up
// before anything was built. A notification banner dismisses itself after about
// five seconds, which is half the countdown -- the button would leave the
// screen while the clock it belongs to was still running. Notification delivery
// is also suppressed by Focus, and a meeting is exactly when Focus is on, so
// the one prompt that must not be swallowed is the one most likely to be. And
// UNUserNotificationCenter would put a permission prompt in front of a tracker
// that currently asks for nothing at all.
//
// Non-activating and accessory-level, so it never takes focus or interrupts
// typing: it appears where a notification appears, and the app behind it keeps
// the keyboard.
//
// It lives in its own file so a test can build one, press its button and watch
// the clock without also building the menu bar app around it. The button is the
// only part of this feature a person ever touches, and a synthetic click cannot
// reach it from a shell the system has not granted Accessibility to, so the
// press has to happen in-process or not at all.
final class CountdownPanel {
    private let panel: NSPanel
    private let message = NSTextField(labelWithString: "")
    private var remaining: Int
    private var timer: Timer?
    private let onExpire: () -> Void
    private let onCancel: () -> Void

    init(meeting: String, seconds: Int,
         onExpire: @escaping () -> Void, onCancel: @escaping () -> Void) {
        self.remaining = seconds
        self.onExpire = onExpire
        self.onCancel = onCancel

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
        // Follows the user across desktops and sits over full-screen apps --
        // a meeting is often the full-screen app, and the countdown that
        // belongs to it must not be stranded on another Space.
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let title = NSTextField(labelWithString: meeting.isEmpty ? "Meeting" : meeting)
        title.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        title.lineBreakMode = .byTruncatingTail

        message.font = NSFont.systemFont(ofSize: 12)
        message.textColor = .secondaryLabelColor
        message.lineBreakMode = .byTruncatingTail

        let keep = NSButton(title: "Keep tracking", target: self,
                            action: #selector(cancelTapped))
        keep.bezelStyle = .rounded
        keep.keyEquivalent = "\r"

        let content = panel.contentView!
        for v in [title, message, keep] {
            v.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(v)
        }
        NSLayoutConstraint.activate([
            title.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
            title.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -18),
            title.topAnchor.constraint(equalTo: content.topAnchor, constant: 14),

            message.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            message.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -18),
            message.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 4),

            keep.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            keep.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -14),
        ])
        self.keep = keep

        redraw()
        place()
        // Regardless, not makeKeyAndOrderFront: the app is an accessory and
        // must not steal the keyboard from whatever the meeting was about.
        panel.orderFrontRegardless()

        let t = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
        // .common for the same reason the poll timers use it: an open menu
        // spins the run loop in .eventTracking, where a .default-mode timer
        // does not fire, and a countdown that silently stops counting while
        // the dropdown is open would sit at "3s" and never act.
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    /// The button and the line of text, for a test to press and read. Held
    /// rather than looked up because a test that goes hunting through the view
    /// hierarchy passes when the wiring is wrong and the hunt finds nothing.
    private(set) var keep: NSButton!
    var messageText: String { message.stringValue }

    private func place() {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        panel.setFrameOrigin(NSPoint(x: visible.maxX - panel.frame.width - 16,
                                     y: visible.maxY - panel.frame.height - 16))
    }

    private func redraw() {
        message.stringValue = "Ended — stopping tracking in \(remaining)s"
    }

    private func tick() {
        remaining -= 1
        if remaining > 0 { redraw(); return }
        let expire = onExpire
        close()
        expire()
    }

    @objc private func cancelTapped() {
        let cancel = onCancel
        close()
        cancel()
    }

    /// Withdraw the panel without either outcome firing. Used when the call
    /// comes back while the countdown is still running.
    func close() {
        timer?.invalidate()
        timer = nil
        panel.orderOut(nil)
    }
}
