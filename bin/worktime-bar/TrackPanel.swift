import AppKit

// MARK: - The track-back panel

// ⌥W. A phone call that just ended, a conversation at somebody's desk: the
// length is known and the minutes are behind you. Claiming those needs a
// number, and a number needs somewhere to type it. The same panel logs main
// time or special time, and for special either as an extra block or by taking
// the minutes out of main.
//
// This is the only window this app has. Everything else the
// tracker offers is a menu row or a keystroke, which is the right shape for a
// choice between fixed options; this asks for a quantity, and a quantity
// cannot be a menu without pre-deciding which quantities a person is allowed
// to have.
//
// Unlike CountdownPanel this one DOES take the keyboard, and has to: it exists
// to be typed into, which means it interrupts.
//
// It lives in its own file for the same reason the countdown does: a test can
// build one, set its fields and press its button without also standing up the
// menu bar app around it.
final class TrackPanel: NSObject, NSTextFieldDelegate, NSWindowDelegate {
    private let panel: NSPanel
    private let onTrack: (Int, String) -> Void
    private let onClose: () -> Void

    /// The minute count and the overlap rule, held rather than looked up so a
    /// test that mis-wires them fails instead of quietly finding nothing.
    private(set) var minutesField: NSTextField!
    private(set) var modePicker: NSPopUpButton!
    private(set) var trackButton: NSButton!

    /// The probe's names for the rules, in the order they are offered. Each
    /// one is a bucket AND a rule about minutes already counted, which is why
    /// they share one picker: "Special, taking from main" is a different claim
    /// from "Special, extra", not the same claim with a flag.
    ///
    /// Parallel to the titles below -- the picker reports an index, and
    /// mapping it back through this is what keeps the row somebody read from
    /// naming a different rule than the one that runs.
    static let modes = ["clip", "split", "special_extra", "special_take"]
    static let modeTitles = [
        "Main — stop at the last tracked session",
        "Main — split the rest before it",
        "Special — extra, only untracked minutes",
        "Special — take the minutes out of main",
    ]
    // A negative count under either special row hands that many special
    // minutes back to main, so the same two rows serve as the undo.

    /// Where the minute count starts, and what a previous answer does to it.
    ///
    /// Remembered across presses because the number is usually the same one:
    /// somebody who tracks ten-minute calls is going to track another
    /// ten-minute call, and retyping it every time is the kind of friction
    /// that ends with the feature unused. Five is the opening default only
    /// until the first real answer replaces it.
    static let minutesKey = "trackBackMinutes"
    static let modeKey = "trackBackMode"
    static let defaultMinutes = 5

    /// `present: false` is for the tests, and is the same bargain
    /// CountdownPanel strikes: the behaviour under test is the parsing, the
    /// mode mapping and the button, none of which needs a window on screen --
    /// and a suite that puts a keyboard-taking panel in front of whatever the
    /// machine is doing, four times a run, is worse than no suite at all.
    init(present: Bool = true,
         onClose: @escaping () -> Void = {},
         onTrack: @escaping (Int, String) -> Void) {
        self.onTrack = onTrack
        self.onClose = onClose

        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 360, height: 148),
                        styleMask: [.titled, .closable, .utilityWindow],
                        backing: .buffered, defer: false)
        panel.title = "Track time"
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        super.init()
        // Set so the titlebar's close button is not a way of withdrawing the
        // panel that nothing hears about. orderOut() does not send this --
        // cancelTapped calls onClose itself -- so the two paths are covered
        // once each rather than twice or not at all.
        panel.delegate = self

        let prompt = NSTextField(labelWithString: "Minutes to track:")
        prompt.font = NSFont.systemFont(ofSize: 12)

        let saved = UserDefaults.standard.integer(forKey: TrackPanel.minutesKey)
        minutesField = NSTextField(string: String(saved > 0 ? saved
                                                            : TrackPanel.defaultMinutes))
        minutesField.alignment = .right
        minutesField.delegate = self

        modePicker = NSPopUpButton(frame: .zero, pullsDown: false)
        modePicker.addItems(withTitles: TrackPanel.modeTitles)
        // Restored by NAME, not by index. A stored index would silently point
        // at a different rule the day a third one is added in the middle.
        let savedMode = UserDefaults.standard.string(forKey: TrackPanel.modeKey) ?? ""
        modePicker.selectItem(at: TrackPanel.modes.firstIndex(of: savedMode) ?? 0)
        modePicker.target = self
        modePicker.action = #selector(modeChanged)

        trackButton = NSButton(title: "Track", target: self,
                               action: #selector(trackTapped))
        trackButton.bezelStyle = .rounded
        trackButton.keyEquivalent = "\r"

        let cancel = NSButton(title: "Cancel", target: self,
                              action: #selector(cancelTapped))
        cancel.bezelStyle = .rounded
        cancel.keyEquivalent = "\u{1b}"

        let content = panel.contentView!
        for v in [prompt, minutesField, modePicker, trackButton, cancel] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(v)
        }
        NSLayoutConstraint.activate([
            prompt.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 18),
            prompt.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),
            prompt.centerYAnchor.constraint(equalTo: minutesField.centerYAnchor),

            minutesField.leadingAnchor.constraint(equalTo: prompt.trailingAnchor, constant: 8),
            minutesField.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -18),
            minutesField.topAnchor.constraint(equalTo: content.topAnchor, constant: 16),

            modePicker.leadingAnchor.constraint(equalTo: prompt.leadingAnchor),
            modePicker.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -18),
            modePicker.topAnchor.constraint(equalTo: minutesField.bottomAnchor, constant: 12),

            trackButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            trackButton.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -14),
            cancel.trailingAnchor.constraint(equalTo: trackButton.leadingAnchor, constant: -8),
            cancel.centerYAnchor.constraint(equalTo: trackButton.centerYAnchor),
        ])

        place()
        if present {
            // Activating, unlike every other surface this app puts up. The
            // panel is a text field and a keystroke away from being useless if
            // the keyboard stays with the app behind it -- and the app is an
            // accessory, so ordering the window front is not enough on its own
            // to make it key.
            NSApp.activate(ignoringOtherApps: true)
            panel.makeKeyAndOrderFront(nil)
            panel.makeFirstResponder(minutesField)
            minutesField.currentEditor()?.selectAll(nil)
        }
    }

    private func place() {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        // Under the menu bar item rather than centred: the panel belongs to
        // the dot, and it is where the eye already is at the moment ⌥W is
        // pressed twice.
        panel.setFrameOrigin(NSPoint(x: visible.maxX - panel.frame.width - 16,
                                     y: visible.maxY - panel.frame.height - 16))
    }

    /// The typed count, or nil if it is not a number of minutes the picked
    /// rule can act on.
    ///
    /// nil rather than a default, and the button is disabled on it rather than
    /// falling back to five: a panel that banks a number the person did not
    /// type is worse than one that refuses to act, because the minutes land in
    /// the day either way and only one of those can be noticed.
    ///
    /// Negative is a real answer for the special rules -- "-2" hands two
    /// special minutes back to main -- and meaningless for the main ones, where
    /// there is nothing to hand back, so it is refused there rather than
    /// quietly read as its absolute value.
    var minutes: Int? {
        let raw = minutesField.stringValue.trimmingCharacters(in: .whitespaces)
        guard let n = Int(raw), n != 0 else { return nil }
        if n < 0 && !TrackPanel.isSpecial(mode) { return nil }
        return n
    }

    var mode: String { TrackPanel.modes[modePicker.indexOfSelectedItem] }

    static func isSpecial(_ mode: String) -> Bool { mode.hasPrefix("special_") }

    func controlTextDidChange(_: Notification) { syncButton() }

    /// Also on the picker: "-2" is valid under one rule and not the next, so
    /// changing the rule can enable or disable the button with no typing.
    @objc func modeChanged() { syncButton() }

    private func syncButton() { trackButton.isEnabled = minutes != nil }

    @objc func trackTapped() {
        guard let n = minutes else { return }
        let picked = mode
        // An undo is a correction, not the usual length: remembering "-2"
        // would replace the ten-minute call the next press is expecting.
        if n > 0 { UserDefaults.standard.set(n, forKey: TrackPanel.minutesKey) }
        UserDefaults.standard.set(picked, forKey: TrackPanel.modeKey)
        close()
        onTrack(n, picked)
    }

    @objc func cancelTapped() {
        close()
        onClose()
    }

    /// Whether the panel is actually in front of somebody.
    ///
    /// The question the caller has to ask before opening another one, and it
    /// is deliberately not "have I ever made one". A withdrawn panel is still
    /// a live object, so a caller holding one and testing for its existence
    /// concludes the panel is up when the screen is empty -- which is exactly
    /// how the shortcut died the first time: cancelling it once left the
    /// reference standing and every later double press silently did nothing.
    var isOnScreen: Bool { panel.isVisible }

    func close() { panel.orderOut(nil) }

    func windowWillClose(_: Notification) { onClose() }
}
