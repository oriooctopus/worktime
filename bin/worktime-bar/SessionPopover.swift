import AppKit

// The events one session collapsed, in a panel beside the menu.
//
// This was a real NSMenu submenu first, which is the obvious way to hang more
// rows off a menu row and gets hover-to-open, placement and dismissal for
// free. It was given up for one reason: AppKit places a submenu flush against
// its parent and exposes no way to offset it, so the two windows met at a seam
// and read as one wide menu whose halves obeyed different rules. A panel we
// place ourselves can stand off by POPOVER_GAP, which is what makes it read as
// a note about the row rather than as more menu.
//
// What that costs, and why it is affordable here: a submenu can be walked with
// the arrow keys and this cannot. Nothing in it is a target -- there is no row
// to choose, no action to fire, it is a list to read -- so what is lost is a
// way of moving a selection through items that were never selectable.
//
// One panel for the app's lifetime, refilled as the pointer moves between
// rows. Rebuilding a window per row flashed one closed and another open for
// every row crossed on the way down the list.
final class SessionPopover {
    private let panel: NSPanel
    private let stack = NSStackView()
    // Only a test passes false: building a panel is most of what there is to
    // test here, and a suite that put a real one on screen would drop a
    // floating window over whatever the machine was doing, exactly as the
    // countdown panel's tests once did.
    private let present: Bool

    init(present: Bool = true) {
        self.present = present
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 460, height: 100),
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        // Above the menu it sits next to. A menu runs at the pop-up level, so
        // anything at that same level is drawn behind it -- the panel would
        // open correctly and be invisible under the very window it is beside.
        panel.level = NSWindow.Level(
            rawValue: Int(CGWindowLevelForKey(.popUpMenuWindow)) + 1)
        // A menu is up whenever this is, and menus join every Space.
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        // The panel must never take the keyboard: the menu owns it while it is
        // open, and a panel that stole it would close the menu that opened it.
        panel.ignoresMouseEvents = true

        let blur = NSVisualEffectView()
        blur.material = .menu
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.wantsLayer = true
        blur.layer?.cornerRadius = 8
        blur.layer?.masksToBounds = true
        blur.layer?.borderWidth = 0.5
        blur.layer?.borderColor = NSColor.separatorColor.cgColor

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 0
        stack.edgeInsets = NSEdgeInsets(top: 6, left: 0, bottom: 6, right: 0)
        stack.translatesAutoresizingMaskIntoConstraints = false
        blur.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: blur.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: blur.trailingAnchor),
            stack.topAnchor.constraint(equalTo: blur.topAnchor),
            stack.bottomAnchor.constraint(equalTo: blur.bottomAnchor),
        ])
        panel.contentView = blur
    }

    /// Fill with one session's events and put it beside `menu`, level with
    /// `row`. Both rectangles are in screen coordinates.
    func show(_ s: ActSession, row: NSRect, menu: NSRect, screen: NSRect) {
        for v in stack.arrangedSubviews { stack.removeArrangedSubview(v); v.removeFromSuperview() }

        // Measured over this session's own rows, so the two fixed columns line
        // up within the panel -- the same rule the menu's list follows, and
        // the reason it is measured per panel rather than once for the day is
        // that a session of nothing but focus rows should not be padded out to
        // the width of the longest kind anywhere.
        let timeWidth = columnWidth(s.rows.map(\.t))
        let kindWidth = columnWidth(s.rows.map(\.kind))
        let width: CGFloat = 460
        for r in s.rows {
            let view = ActivityRowView(Activity(t: r.t, at: 0, kind: r.kind,
                                                what: r.what, n: r.n),
                                       width: width, age: r.t,
                                       ageWidth: timeWidth, kindWidth: kindWidth)
            view.translatesAutoresizingMaskIntoConstraints = false
            stack.addArrangedSubview(view)
            NSLayoutConstraint.activate([
                view.widthAnchor.constraint(equalToConstant: width),
                view.heightAnchor.constraint(equalToConstant: 17),
            ])
        }
        if let line = sessionMoreLine(s) {
            let more = NSTextField(labelWithString: line)
            more.font = ACTIVITY_FONT
            more.textColor = .tertiaryLabelColor
            more.translatesAutoresizingMaskIntoConstraints = false
            let holder = NSView()
            holder.translatesAutoresizingMaskIntoConstraints = false
            holder.addSubview(more)
            stack.addArrangedSubview(holder)
            NSLayoutConstraint.activate([
                holder.widthAnchor.constraint(equalToConstant: width),
                holder.heightAnchor.constraint(equalToConstant: 19),
                more.leadingAnchor.constraint(equalTo: holder.leadingAnchor, constant: 24),
                more.centerYAnchor.constraint(equalTo: holder.centerYAnchor),
            ])
        }

        let height = CGFloat(s.rows.count) * 17
            + (sessionMoreLine(s) == nil ? 0 : 19) + 12
        let frame = popoverFrame(row: row, menu: menu,
                                 size: NSSize(width: width, height: height),
                                 screen: screen)
        panel.setFrame(frame, display: false)
        if present { panel.orderFrontRegardless() }
    }

    func hide() {
        panel.orderOut(nil)
    }

    /// The panel's frame, for a test to check placement without a screen.
    var frame: NSRect { panel.frame }
    var isShowing: Bool { panel.isVisible }
}
