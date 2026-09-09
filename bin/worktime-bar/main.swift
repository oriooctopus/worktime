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
// Where launchd sends this app's stderr, which is where every probe failure is
// already written. The menu's diagnostic rows can only afford three lines of a
// traceback, so the row that opens the whole file has to name it -- and the
// name has to be the one deploy/launchd/com.oliver.worktime-bar.plist uses,
// which a test pins rather than trusting the two to stay in step.
let BAR_LOG = "/tmp/worktime-bar.err"

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

// ⌥W logs an entry: one keypress saying this minute was worked, for work this
// machine has no way to see. Nothing opens, nothing is asked.
//
// Registered the same way, and deliberately without ⌘: the shift toggle is a
// decision about the whole day and wants the harder chord, while this is meant
// to be cheap enough to press mid-thought. ⌥W types "∑" in a text field, so
// the two-key form is only safe because a Carbon hot key consumes the event
// before the frontmost app sees it.
let ENTRY_HOTKEY_CODE = UInt32(kVK_ANSI_W)
let ENTRY_HOTKEY_MODS = UInt32(optionKey)

// ⌘E ends the session, from anywhere. One press ends it at this minute; two
// end it at the last entry instead -- the same two minutes End Session's rows
// name, on the same key, told apart the way ⌥W tells its two meanings apart.
//
// This way round, and not the other, because the common case is the one that
// should cost one press: the ending you mean most of the time is the minute
// you are in. Ending at the last entry is the correction -- you are leaving
// and the last half hour was not work -- and a correction is worth a
// deliberate second press. It is also the recoverable order. A single press
// that lands End Now claims a few minutes too many, which is visible in the
// period list and can be walked back; a single press that silently ended the
// day half an hour ago deletes work nothing in the interface would show.
//
// ⌘E without ⌥, unlike the shift toggle, because the chord was asked for in
// that form. It is a common shortcut in other apps -- Finder's Eject, "Use
// Selection for Find" in several editors -- and a Carbon hot key consumes the
// event before the frontmost app sees it, so those lose it while this runs.
let END_HOTKEY_CODE = UInt32(kVK_ANSI_E)
let END_HOTKEY_MODS = UInt32(cmdKey)

// How long ⌘E waits to find out whether a second press is coming.
//
// Longer than ⌥W's 0.33: that key's single press only files a minute, while
// this one declares the day over, and the cost of the two mistakes is not the
// same. A double press read as two singles ends the day at the wrong minute
// and posts two banners saying so; the price of the extra time is that every
// single press is a beat slower to land, which is a delay before a banner and
// not before anything that could be lost.
let END_DOUBLE_PRESS_SEC = 0.5

// How long ⌥W waits to find out whether a second press is coming, before
// treating the first as a single press.
//
// The single press has to be delayed by this much, which is the price of the
// double press existing at all: acting immediately and then also opening the
// panel would file an entry every time somebody meant to open the panel, and
// those stray entries would be indistinguishable from real ones. A third of a
// second is comfortably inside a deliberate double press and short enough that
// the banner still reads as a response to the key rather than as something
// that happened later.
let DOUBLE_PRESS_SEC = 0.33

// Which hot key fired. The Carbon handler is installed once and shared, so it
// has to tell them apart by id rather than by which registration it came from.
let HOTKEY_ID_SHIFT = UInt32(1)
let HOTKEY_ID_ENTRY = UInt32(2)
let HOTKEY_ID_END = UInt32(3)

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
    // Which session this event fell into, as the probe grouped them. Carried
    // so the raw list can mark the rest of a stretch when one of its rows is
    // hovered -- the grouping is the sessions view's answer, and this is that
    // answer without leaving the list. Nil when the payload had no sessions to
    // group by, which marks nothing rather than lumping the unlabelled
    // together as though they were one.
    var session: Int?
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
    // HH:MM the link item would claim from, nil when there is nothing to link
    // to. Decided by the probe off the same periods the list is drawn from --
    // the menu must not work out for itself which period counts as the last
    // one, or it can name a different minute than the action then claims.
    var linkFrom: String?
    var periods: [Period] = []
    var activities: [Activity] = []
    var sessions: [ActSession] = []
}

// Every probe run goes through here, one at a time. The probe is not a cheap
// pure reader: it recomputes the day from every transcript on disk and writes
// the status cache, so two running together do the same expensive work twice
// and race on the same file. Worse, they compound -- on 2026-09-04 this app
// was found with fifteen `probe status` children alive at once, the oldest 65
// seconds old, because every 5s poll launched another regardless of whether
// the previous one had answered. Past a certain point each probe is slow
// BECAUSE of the others, so the pile never drains on its own: the menu goes
// minutes stale and a clicked row waits behind the whole queue. That is
// exactly what "the menu is slow to open and clicking an option does nothing"
// looks like from outside.
//
// Serial rather than merely capped, because the order is part of the meaning:
// a "mark" followed by a status read has to see the mark.
//
// .userInitiated rather than .utility, because a dot on screen refreshing
// every five seconds is user-initiated work by definition. The hazard being
// avoided is real but was NOT the cause of the red afternoon that prompted
// this: a child process inherits the spawning thread's disk policy, and the
// probe's fingerprint walk -- every transcript under both profile roots --
// measured 140s under an explicitly throttled policy against 2.3s without
// one, which is five times over PROBE_TIMEOUT_SEC. What the throttle would
// cost is therefore known. Whether QoS alone imposes it is not: the tier is
// not readable back, and raising this changed nothing about the timeouts
// being chased at the time. Those turned out to be every open() under
// ~/Documents stalling for the whole 30s after a rebuild -- see the
// permissions note in README.
let probeQueue = DispatchQueue(label: "worktime.probe", qos: .userInitiated)

// A one-shot flag set from the watchdog's queue and read from the caller's, so
// the kill is reported rather than inferred: exit 15 alone cannot tell this
// app's own SIGTERM from anybody else's.
final class Fired {
    private let lock = NSLock()
    private var value = false
    func set() { lock.lock(); value = true; lock.unlock() }
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

func runProbe(_ args: [String]) -> ProbeRun {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: PYTHON)
    p.arguments = [PROBE] + args
    let out = Pipe()
    let err = Pipe()
    p.standardOutput = out
    p.standardError = err
    let started = Date()
    do { try p.run() } catch {
        FileHandle.standardError.write("probe launch failed: \(error)\n".data(using: .utf8)!)
        return .failed(ProbeFailure(args: args, path: PROBE,
                                    launchError: "\(error)"))
    }
    let fired = Fired()
    let killer = DispatchWorkItem {
        if p.isRunning { fired.set(); p.terminate() }
    }
    DispatchQueue.global().asyncAfter(deadline: .now() + PROBE_TIMEOUT_SEC,
                                      execute: killer)
    let data = out.fileHandleForReading.readDataToEndOfFile()
    let edata = err.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    killer.cancel()
    // Logged, not swallowed. A silent non-zero exit is what let a blind poll
    // sit on screen looking like a confident idle reading. Returned as well as
    // logged, so the menu can say which failure this was instead of leaving
    // the answer in a file in /tmp.
    guard p.terminationStatus == 0 else {
        let msg = String(data: edata, encoding: .utf8) ?? ""
        FileHandle.standardError.write(
            "probe \(args) exit \(p.terminationStatus): \(msg)\n".data(using: .utf8)!)
        return .failed(ProbeFailure(args: args, path: PROBE,
                                    status: p.terminationStatus, stderr: msg,
                                    elapsed: Date().timeIntervalSince(started),
                                    killed: fired.isSet))
    }
    return .ok(String(data: data, encoding: .utf8) ?? "")
}

// Confirmation for ⌥W. The press is silent by design -- nothing opens, no
// window takes the caret -- and the dot only moves if the minute was not
// already counted, so a working shortcut and a dead one look identical from
// the outside. The banner is the only thing that tells them apart.
//
// Posted with osascript rather than UNUserNotificationCenter because this app
// carries an ad-hoc signature, and macOS refuses an ad-hoc identity notification
// authorization outright: requestAuthorization returns "Notifications are not
// allowed for this application", after which a post is *accepted* at the call
// site and then silently dropped. A path that reports success and shows nothing
// is the worst possible shape for the one thing standing in for feedback, so
// this goes through an identity the system already trusts. Registering the
// bundle with lsregister and launching through LaunchServices were both tried;
// neither lifts the refusal.
//
// The cost of that choice is that macOS owns how long the banner stays up
// (roughly five seconds). A notification withdrawn on our own schedule needs
// the native API, which needs a real signing identity.
func notifyEntryLogged() {
    notify("Logged this minute as work.")
}

// The same banner for the double press. Names the minutes that actually
// landed, and the ones asked for when the two differ -- which is the ordinary
// outcome of the default rule, not an error, and reads as one unless it is
// spelled out.
func notifyTracked(claimed: Int, asked: Int) {
    if claimed == 0 {
        notify("Nothing to track — those minutes are already counted.")
    } else if claimed < asked {
        notify("Tracked \(claimed)m of \(asked)m — the rest was already counted.")
    } else {
        notify("Tracked \(claimed)m as work.")
    }
}

// Confirmation for ⌘E, and for the two rows that do the same thing.
//
// It names the minute because that is the whole question the two endings
// differ on, and a press that ended the day thirty minutes ago looks identical
// from the outside to one that ended it now -- the dot goes out either way.
// Naming which rule ran as well as the minute is what lets a mistaken double
// press be recognised as one: "at 13:05 — the last entry" after a single press
// meant for now is the only thing that would say so.
func notifySessionEnded(at: String, atLast: Bool) {
    notify(atLast ? "Session ended at \(at) — the last entry."
                  : "Session ended at \(at).")
}

func notify(_ body: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = ["-e", "display notification \"\(body)\" "
        + "with title \"Worktime\""]
    // Logged rather than swallowed, same as the probe: a banner that stops
    // appearing is indistinguishable from a hot key that stopped registering.
    do { try p.run() } catch {
        FileHandle.standardError.write(
            "notify launch failed: \(error)\n".data(using: .utf8)!)
        return
    }
    p.waitUntilExit()
    if p.terminationStatus != 0 {
        FileHandle.standardError.write(
            "notify exit \(p.terminationStatus)\n".data(using: .utf8)!)
    }
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
// Driven by NSWorkspace.didActivateApplicationNotification, with the tick
// still running underneath it. The notification is what an app switch
// actually is, so it fires on the switch instead of up to POLL_SEC later, and
// it fires however the switch was made -- rcmd, Cmd-Tab, the Dock, a click on
// a window. There is nothing to listen to in rcmd itself: it activates apps
// through the ordinary API, so the ordinary notification already covers it,
// and hooking the app would only see the switches that one launcher made.
//
// The tick stays because two of the three things sampled here are not
// switches. Chrome's active tab changes with no activation at all -- a new
// page in the same window is a switch by every measure the probe cares about
// and by none that AppKit reports -- and the live idle reading in
// PRESENCE_PATH is continuous by definition.
//
// Sampling used to be the whole mechanism, on the argument that the machine
// going away -- sleep, lock, this app crashing -- should be indistinguishable
// from samples stopping, where a notification would leave the last activation
// standing and credit the absence to whatever was frontmost when the lid
// closed. That argument died with the span model: the probe credits the
// ACTIVATION and nothing else, so a last activation with no successor buys no
// time and there is no absence to misattribute. What is left is the accounting
// cost -- a two-second glance at Slack was invisible between ticks and is now
// a row. It is a row that says a person switched apps, which is presence; the
// idle gate still discards it if nobody was at the machine.
//
// Neither API needs a permission grant. Accessibility is required only for
// window TITLES -- which Slack channel, which document -- and this
// deliberately stays at app granularity to avoid asking for that.
let FOCUS_DIR = ("~/.claude/stats/worktime/focus" as NSString).expandingTildeInPath

// A row is written when the front thing changes, and at no other time. The
// log is a record of switches.
//
// It used to also tick every thirty seconds, because the probe credited the
// span between consecutive rows and needed the span to stay short: without a
// heartbeat one row would sit there claiming a two-hour sleep. That made
// being in front a subscription -- an app left in front billed at the same
// rate all night, and on 2026-09-02 forty-one minutes of untouched Slack
// became forty-one minutes of work. The probe now credits the ACTIVATION and
// nothing else, so there is no span to truncate and no reason to tick.
//
// What the heartbeat also carried was the live idle reading, which is a
// genuinely continuous thing and now goes to PRESENCE_PATH: one file,
// overwritten, no history. Two signals that were sharing a channel because
// they happened to be sampled together.
let PRESENCE_PATH = ("~/.claude/stats/worktime/presence.json" as NSString)
    .expandingTildeInPath

// Chrome's active tab, as (title, url).
//
// The frontmost app alone says a browser is open, not what is in it, which is
// why Chrome earned no foreground credit at all: half of it is the job and
// half is shopping, and the app name cannot tell the two apart. The tab can,
// and nothing else can. History records navigations, so a doc opened yesterday
// and read all morning leaves no row anywhere -- that case, a parked tab with
// the day's work in it, is the whole reason this exists.
//
// Asked only on the samples where Chrome is already frontmost, so it costs one
// osascript per heartbeat at most and nothing at all while the browser sits
// behind something else.
//
// Title as well as URL because the classifier needs both and neither is
// sufficient: a Google Doc URL is an opaque id with no hint of the employer in
// it, and the title -- "Rubrik AI / RAC Policy -- OTEL Integration Test Cases"
// -- is the only place the work keyword appears. The reverse holds for a PR
// page whose title is somebody else's branch name.
//
// Requires the Automation permission for Chrome, which macOS prompts for once.
// Refusing it returns nil, and a sample with no tab on it earns nothing --
// precisely what every Chrome sample earned before this existed, so the
// declined case degrades to the old behaviour rather than to a wrong one.
let CHROME_BUNDLE = "com.google.Chrome"

// Chrome answers in single-digit milliseconds when it is healthy. This is not
// tuned for the healthy case: it is the wall against a browser wedged behind a
// modal, where the script never returns and would otherwise hang the poll
// timer -- and with it the menu, the countdown and the dot -- indefinitely.
let CHROME_TAB_TIMEOUT_SEC = 2.0

func chromeActiveTab() -> (title: String, url: String)? {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = CHROME_TAB_SCRIPT
    let out = Pipe()
    p.standardOutput = out
    // Both streams down one pipe. Two pipes would need two readers to avoid
    // deadlocking on a full buffer, and there is nothing to tell apart:
    // osascript writes nothing to stderr when it succeeds, so anything here
    // on a non-zero exit is the error text -- which is the only evidence that
    // separates a denied permission from a browser with no windows.
    p.standardError = out
    do { try p.run() } catch { return nil }

    // Exit is awaited on a semaphore rather than with waitUntilExit(), which
    // does not block the thread -- it POLLS the current run loop until the
    // child is done. On the main thread that drains the main queue, so an
    // activation notification arriving while this waits used to be delivered
    // inside it: a second sample() ran to completion in the middle of the
    // first, wrote its row first, and left the outer one to land afterwards
    // carrying the earlier timestamp. The focus log went out of order, which
    // is the one thing every reader of it assumes cannot happen.
    //
    // Only ever a problem once activations could arrive between ticks. The
    // 5s timer could not re-enter itself, so the reentrancy was there all
    // along with nothing able to trigger it.
    let exited = DispatchSemaphore(value: 0)
    p.terminationHandler = { _ in exited.signal() }

    let killer = DispatchWorkItem { if p.isRunning { p.terminate() } }
    DispatchQueue.global().asyncAfter(deadline: .now() + CHROME_TAB_TIMEOUT_SEC,
                                      execute: killer)
    // Read before waiting: readDataToEndOfFile returns at EOF, which is the
    // child exiting, so this is the wait. Terminating the child closes the
    // pipe, so the timeout unblocks it too.
    let data = out.fileHandleForReading.readDataToEndOfFile()
    exited.wait()
    killer.cancel()

    guard let text = String(data: data, encoding: .utf8) else { return nil }
    // A non-zero exit is the ordinary answer to "what is the front window?"
    // when Chrome has no windows open, and it is also what a refused Apple
    // Event looks like. Whatever it is, say it once per launch rather than
    // folding it into the no-windows case: a silent nil here is
    // indistinguishable in the focus log from a machine whose owner never
    // opened a browser, which is exactly how a delimiter bug survived every
    // day it ran.
    //
    // Once, because none of it can be fixed from here -- a refusal is granted
    // in System Settings, not by retrying -- and the poll would otherwise
    // repeat the line every few seconds.
    if p.terminationStatus != 0 {
        reportChromeTabFailure(text)
        return nil
    }
    // A reply with no page in it came back exit 0 -- so silence here is what
    // let both of these bugs run unnoticed. The reply says what it was.
    guard let tab = parseChromeTabReply(text) else {
        reportChromeTabFailure(text)
        return nil
    }
    return tab
}


final class FocusLog {
    // Where the osascript round trip and both file writes happen. Serial, so
    // the log stays in sampled order and the state below needs no lock.
    //
    // .userInitiated for the same reason as probeQueue: this spawns osascript,
    // which inherits the throttled disk tier from a utility thread, and a
    // sampler that misses its window writes no row at all -- the minutes it
    // was supposed to witness come out as silence.
    private let queue = DispatchQueue(label: "worktime.focus", qos: .userInitiated)
    private var lastKey: String?
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

    // Switches arrive as NSWorkspace activations; the tick supplies the rest.
    // Both land in sample(), so a switch is written the moment it happens
    // rather than whenever the next tick catches up to it.
    //
    // The default argument is evaluated per call, so the tick's plain
    // sample() still reads whatever is in front now. The notification passes
    // the app it names instead, which is the authoritative answer to "what
    // just came forward" -- frontmostApplication is a second reading of the
    // same instant and there is no reason to prefer it.
    // Everything this reads about the running system is read here, on
    // whichever thread asked -- which is always main, because both callers are
    // an AppKit notification and a main-mode timer. Everything it then DOES
    // with those readings happens on `queue`.
    //
    // The split is not tidiness. Asking Chrome for its tab is an osascript
    // round trip, ~130ms on a healthy browser and up to CHROME_TAB_TIMEOUT_SEC
    // on a wedged one, and it used to run right here: a sample on the main
    // thread every 5s while Chrome was in front, plus one per app switch. A
    // profile of the running app on 2026-09-04 found the main thread inside
    // that one read for 30% of its wall time. A menu bar click landing in that
    // window has nowhere to go until the browser answers, which is what "slow
    // to open, slow to respond" was.
    //
    // It was also re-entrant, which is worse than slow: Process.waitUntilExit
    // spins the run loop, the run loop delivers the next activation
    // notification, and that notification called straight back into sample()
    // from inside sample(), interleaving two rows' writes.
    func sample(now: Date = Date(),
                app: NSRunningApplication? = NSWorkspace.shared.frontmostApplication)
    {
        // Read now, not on the queue. These are facts about THIS instant and
        // the queue may not reach them for a moment; an idle reading taken
        // late is a different reading, and frontmostApplication asked later is
        // a different app.
        let bundle = app?.bundleIdentifier ?? ""
        let name = app?.localizedName ?? ""
        let idle = Int(Self.idleSeconds().rounded())
        queue.async { self.record(now: now, bundle: bundle, name: name, idle: idle) }
    }

    // The serial half. Every piece of this object's mutable state -- lastKey,
    // lastWrite, the open file handle -- is touched here and nowhere else, so
    // there is no lock and no interleaving, and rows land in the order they
    // were sampled.
    private func record(now: Date, bundle: String, name: String, idle: Int) {
        let day = Self.dayfmt.string(from: now)
        var row: [String: Any] = [
            "day": day,
            "t": Self.stamp.string(from: now),
            "app": name,
            "bundle": bundle,
            // Rounded, not thresholded. The threshold is the probe's to choose
            // and lives beside its other thresholds; duplicating it here would
            // give the two halves separate definitions of "away" that could
            // drift apart without either one looking wrong.
            "idle": idle,
        ]
        // Only Chrome carries these, and only when the tab could be read. The
        // probe treats their absence as "not a page worth counting", which is
        // the same verdict it reaches for a tab that is genuinely not work --
        // so a sample written before this field existed, or by a machine that
        // declined the permission, needs no special handling anywhere.
        if bundle == CHROME_BUNDLE, let tab = chromeActiveTab() {
            row["tab"] = tab.title
            row["url"] = tab.url
        }

        // The live reading goes out on every poll, switch or not. It is what
        // the dot reads, and it is the one thing here that really is
        // continuous.
        writePresence(row)

        // Chrome is keyed by its tab as well as by itself, because Chrome
        // earns per PAGE: leaving a document for a PR inside the same window
        // is a switch by every measure the probe cares about, and comparing
        // bundles alone would file it as no event at all.
        let key = bundle == CHROME_BUNDLE
            ? bundle + "\u{1}" + ((row["tab"] as? String) ?? "")
            : bundle
        guard key != lastKey else { return }

        guard let data = try? JSONSerialization.data(withJSONObject: row),
              var line = String(data: data, encoding: .utf8)
        else { return }
        line += "\n"
        write(line, day: day)
        lastKey = key
        lastWrite = now
    }

    /// The current front app and idle reading, overwritten in place.
    ///
    /// Not appended and not history: the only question it answers is "as of
    /// right now, when did somebody last touch this machine with something
    /// work-shaped in front of it", which the focus log answered as a side
    /// effect of ticking. Written whole to a temp path and moved into place,
    /// so a reader mid-write sees the previous file rather than half of this
    /// one.
    private func writePresence(_ row: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: row)
        else { return }
        let tmp = PRESENCE_PATH + ".tmp"
        guard (try? data.write(to: URL(fileURLWithPath: tmp))) != nil
        else { return }
        try? FileManager.default.removeItem(atPath: PRESENCE_PATH)
        try? FileManager.default.moveItem(atPath: tmp, toPath: PRESENCE_PATH)
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

// One line of a failed poll's account of itself. Monospaced and small: these
// rows are a path, an exit status and a line of stderr, which are read by
// picking a detail out rather than by reading the sentence, and a proportional
// face makes a path harder to scan. Dimmer than the status row above them for
// the same reason the activity rows are -- the verdict is the thing being read,
// this is what it was derived from.
//
// A custom view rather than a disabled NSMenuItem, and for the usual reason:
// AppKit recolors a menu item's own title through the vibrancy pass regardless
// of what is set on it, and a disabled item comes out dimmed far past the point
// where a truncated traceback is legible.
// Wider than the 300 every other row in this menu is built at, and only ever
// on screen while the dot is red. A frame and a message do not fit in 300 --
// they came out middle-truncated, which reads as the block being broken rather
// than the message being long -- and twenty points of menu width is a cheap
// price for the state where the menu is the only thing to read.
let DEBUG_ROW_WIDTH: CGFloat = 330

final class DebugRowView: NSView {
    init(width: CGFloat, text: String) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 16))

        let field = NSTextField(labelWithString: text)
        field.font = NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)
        field.textColor = .secondaryLabelColor
        field.lineBreakMode = .byTruncatingMiddle
        field.translatesAutoresizingMaskIntoConstraints = false
        addSubview(field)

        NSLayoutConstraint.activate([
            field.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 28),
            field.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor,
                                            constant: -14),
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
// HH:mm in the machine's own locale, for the two moments the failure block
// names. Deliberately not seconds: the poll runs every five of them, so a
// second is a precision the number does not have.
let clockFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "HH:mm"
    return f
}()

func clock(_ d: Date) -> String { clockFormatter.string(from: d) }

func columnWidth(_ labels: [String]) -> CGFloat {
    ceil(labels.map {
        ($0 as NSString).size(withAttributes: [.font: ACTIVITY_FONT]).width
    }.max() ?? 0)
}

// Which session the pointer is currently over, shared by every row of one
// built menu.
//
// It lives outside the rows because the answer the raw list gives on hover is
// about the OTHER rows: pointing at one event marks every event of the same
// stretch, which is the sessions view's grouping shown without leaving the
// list. A row cannot know that on its own -- it needs to hear that a sibling
// was entered -- so the rows share one of these and redraw when it changes.
final class SessionHover {
    private(set) var session: Int?
    // Every row that can respond, session rows included. Held strongly: the
    // menu owns the views, this object is rebuilt with them, and both are
    // discarded together on the next build.
    var rows: [NSView] = []
    // Called with the row that was entered, so the events panel can follow the
    // pointer. Nil for the raw list, which answers a hover by marking its own
    // rows and opens nothing.
    var onEnter: ((Int?, NSView?) -> Void)?

    func enter(_ id: Int?, from view: NSView? = nil) {
        guard id != session else { return }
        session = id
        for r in rows { r.needsDisplay = true }
        onEnter?(id, view)
    }
}

// The tint a hovered row takes. Deliberately not the menu's own blue selection
// fill: that is the OS's way of saying "this is what a click will act on", and
// these rows are not clickable -- a full selection bar on them would promise a
// press that does nothing. A wash of the accent colour reads as "this is what
// you are pointing at" without making that promise.
func hoverFill() -> NSColor {
    NSColor.controlAccentColor.withAlphaComponent(0.13)
}

// A checkable row that does NOT close the menu when it is clicked.
//
// An ordinary NSMenuItem with an action dismisses the whole dropdown on the
// click, which is exactly wrong for this one: which of the two lists to read
// is a question about what is on screen right now, and answering it by taking
// the screen away means every look at the other view costs a reopen. A menu
// item with a custom view gets the mouse events itself and AppKit dismisses
// nothing, so the list flips under the pointer with the menu still up -- which
// is the only way the two views can be compared at all.
//
// It draws its own checkmark, highlight and shortcut hint, because a
// view-backed item draws none of what NSMenuItem would have drawn for it.
final class ToggleRowView: NSView {
    private let check = NSTextField(labelWithString: "\u{2713}")
    private let label = NSTextField(labelWithString: "")
    private let hint = NSTextField(labelWithString: "")
    // Full menu-selection blue, unlike the activity rows' wash: this row IS
    // clickable, so the OS's "a click acts on this" fill is the honest one
    // here, where on those rows it would have been a false promise.
    private var highlighted = false
    var onClick: (() -> Void)?

    init(title: String, on: Bool, hint hintText: String, width: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 20))

        check.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
        check.isHidden = !on
        label.stringValue = title
        label.font = NSFont.systemFont(ofSize: 13)
        hint.stringValue = hintText
        hint.font = NSFont.systemFont(ofSize: 13)

        for f in [check, label, hint] {
            f.translatesAutoresizingMaskIntoConstraints = false
            addSubview(f)
        }
        NSLayoutConstraint.activate([
            check.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 5),
            label.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 20),
            hint.leadingAnchor.constraint(greaterThanOrEqualTo: label.trailingAnchor,
                                          constant: 8),
            hint.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -14),
            check.centerYAnchor.constraint(equalTo: centerYAnchor),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            hint.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
        colorize()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    // Same reason as the activity rows: menu rows are laid out after they are
    // made, so an area added against the initial frame covers the wrong strip.
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for a in trackingAreas { removeTrackingArea(a) }
        addTrackingArea(NSTrackingArea(
            rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self, userInfo: nil))
    }

    override func mouseEntered(with _: NSEvent) { setHighlighted(true) }
    override func mouseExited(with _: NSEvent) { setHighlighted(false) }

    private func setHighlighted(_ on: Bool) {
        guard on != highlighted else { return }
        highlighted = on
        colorize()
        needsDisplay = true
    }

    // The label colours are switched by hand because a view's own NSTextFields
    // are never recoloured by AppKit: on the blue fill they would otherwise
    // stay dark on dark, which the other rows avoid only by never being
    // selected in the first place.
    private func colorize() {
        check.textColor = highlighted ? .selectedMenuItemTextColor : .labelColor
        label.textColor = highlighted ? .selectedMenuItemTextColor : .labelColor
        hint.textColor = highlighted ? .selectedMenuItemTextColor : .tertiaryLabelColor
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard highlighted else { return }
        NSColor.selectedContentBackgroundColor.setFill()
        bounds.fill()
    }

    override func mouseUp(with _: NSEvent) { onClick?() }

    func setOn(_ on: Bool) { check.isHidden = !on }
}

final class ActivityRowView: NSView {
    // Set when this row belongs to a session the list also knows about; nil
    // when the payload carried no grouping, in which case hovering marks
    // nothing rather than pretending every unlabelled row is one group.
    var session: Int?
    weak var hover: SessionHover?
    // Whether this row is the top or the bottom of its session's run, which is
    // what the bracket's two arms are drawn from.
    var groupFirst = false
    var groupLast = false

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
        // was rounded from stays reachable. It is a tooltip on the row itself
        // rather than anything drawn, so it does not compete with the bracket
        // the hover draws -- the two say different things about the same row.
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
            // Indented past the bracket's gutter, so the columns do not shift
            // sideways when a group lights up -- the mark has to be drawn in
            // space the text never occupies or the whole list jumps on hover.
            ageField.leadingAnchor.constraint(equalTo: leadingAnchor,
                                              constant: 14 + GROUP_GUTTER),
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

    // Rebuilt on every layout pass rather than added once: menu rows are laid
    // out after they are made, and a tracking area added against the initial
    // zero-ish frame covers the wrong rectangle for the row's whole life.
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for a in trackingAreas { removeTrackingArea(a) }
        addTrackingArea(NSTrackingArea(
            rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self, userInfo: nil))
    }

    override func mouseEntered(with _: NSEvent) { hover?.enter(session) }
    // Cleared rather than left standing: the pointer leaving the list has to
    // put it back the way it was, and the next row entered replaces this
    // anyway, so the two together mean the mark follows the pointer exactly.
    override func mouseExited(with _: NSEvent) { hover?.enter(nil) }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let session, hover?.session == session else { return }
        hoverFill().setFill()
        bounds.fill()
        drawGroupBracket(in: bounds, first: groupFirst, last: groupLast)
    }
}

// How much room the group bracket gets at the leading edge of a raw row.
let GROUP_GUTTER: CGFloat = 10

// One row's slice of the bracket that marks a hovered session: a vertical
// stroke down the gutter, with a short arm at whichever end is the end of the
// run.
//
// Drawn per row rather than as one shape over the group because a menu has no
// surface spanning several items -- each item is its own view and cannot paint
// outside itself. The arms are what makes the stack of strokes read as a
// single bracket rather than as a stripe on each row.
func drawGroupBracket(in bounds: NSRect, first: Bool, last: Bool) {
    let x = bounds.minX + GROUP_GUTTER / 2
    let arm: CGFloat = 4
    let path = NSBezierPath()
    path.lineWidth = 1.5
    path.lineCapStyle = .round
    // The stroke runs the full height of every row, so consecutive rows join
    // into one unbroken line down the group.
    path.move(to: NSPoint(x: x, y: bounds.minY))
    path.line(to: NSPoint(x: x, y: bounds.maxY))
    if first {
        path.move(to: NSPoint(x: x, y: bounds.maxY - 0.75))
        path.line(to: NSPoint(x: x + arm, y: bounds.maxY - 0.75))
    }
    if last {
        path.move(to: NSPoint(x: x, y: bounds.minY + 0.75))
        path.line(to: NSPoint(x: x + arm, y: bounds.minY + 0.75))
    }
    NSColor.controlAccentColor.setStroke()
    path.stroke()
}

// Two lines, unlike the raw rows' one. A session stands for a dozen of them,
// so it can afford the height the thing it replaced would have spent anyway --
// and the same custom-NSView reason as PeriodRowView applies: an NSMenuItem's
// own title is dimmed by the menu's vibrancy pass no matter what is set on it.
final class SessionRowView: NSView {
    // Its own index, so entering it lights this row alone. It shares the
    // hover object with the raw rows because only one list is ever on screen,
    // and one mechanism for "what is the pointer on" is one fewer to keep in
    // step than two.
    var session: Int?
    weak var hover: SessionHover?

    init(_ s: ActSession, width: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: 34))
        let (top, what) = sessionStrings(s)

        let topField = NSTextField(labelWithString: top)
        topField.font = NSFont.systemFont(ofSize: 12,
                                          weight: s.current ? .semibold : .regular)
        topField.textColor = s.counted ? .labelColor : .secondaryLabelColor
        topField.lineBreakMode = .byTruncatingTail

        let whatField = NSTextField(labelWithString: what)
        whatField.font = ACTIVITY_FONT
        whatField.textColor = .secondaryLabelColor
        whatField.lineBreakMode = .byTruncatingTail

        // AppKit draws the submenu arrow for an ordinary menu item and draws
        // nothing at all for one with a custom view, so a row whose events are
        // one hover away would look exactly like a row that has none.
        //
        // On the leading edge rather than the trailing one, where a submenu
        // arrow normally sits. The trailing edge of these rows is a ragged one
        // -- the two lines are different lengths and both truncate -- so an
        // arrow parked out there floated away from the row it belongs to,
        // while the leading edge is the one straight line the block already
        // has. It also puts the mark in the same gutter the raw list's bracket
        // uses, so both lists say "there is more here" in the same column.
        //
        // Pointing back out of the menu, not into it. On the trailing edge an
        // arrow points the way the submenu will open and that is what makes it
        // legible; moved to this side, the same glyph pointed at the row's own
        // text, which reads as "the rest is that way" aimed at the words right
        // next to it.
        let arrow = NSTextField(labelWithString: "\u{2039}")
        arrow.font = NSFont.systemFont(ofSize: 13)
        arrow.textColor = .tertiaryLabelColor

        for f in [topField, whatField, arrow] {
            f.translatesAutoresizingMaskIntoConstraints = false
            addSubview(f)
        }
        NSLayoutConstraint.activate([
            arrow.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            arrow.centerYAnchor.constraint(equalTo: centerYAnchor),
            topField.leadingAnchor.constraint(equalTo: arrow.trailingAnchor, constant: 6),
            topField.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            topField.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            whatField.leadingAnchor.constraint(equalTo: topField.leadingAnchor),
            whatField.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -14),
            whatField.topAnchor.constraint(equalTo: topField.bottomAnchor, constant: 1),
        ])
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        for a in trackingAreas { removeTrackingArea(a) }
        addTrackingArea(NSTrackingArea(
            rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self, userInfo: nil))
    }

    // The fill and the panel both land on mouseEntered, the moment the
    // pointer arrives. The submenu this replaced sat behind the OS's hover
    // delay, so a row gave no sign at all for the half second before its
    // events appeared.
    //
    // `self` goes along so whoever opens the panel knows which row to stand
    // beside; the view is the only thing that knows where on screen it ended
    // up.
    override func mouseEntered(with _: NSEvent) { hover?.enter(session, from: self) }
    override func mouseExited(with _: NSEvent) { hover?.enter(nil) }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let session, hover?.session == session else { return }
        hoverFill().setFill()
        bounds.fill()
    }
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

final class Bar: NSObject, NSApplicationDelegate, NSMenuDelegate, NSMenuItemValidation {
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
    // What the last failed poll was, and how long the run of them has been
    // going. Kept on the Bar rather than in Status because it is not a reading
    // of the day -- it is the reason there is no reading of the day, and the
    // menu is the only place it can be read without opening a file in /tmp.
    var probeFail: ProbeFailure?
    var probeFailures = 0
    /// When the current run of failures began, so the menu can say the minute
    /// rather than a count of polls.
    var probeFailSince: Date?
    var lastGoodPollAt: Date?
    var hotKeyRef: EventHotKeyRef?
    var entryHotKeyRef: EventHotKeyRef?
    var endHotKeyRef: EventHotKeyRef?
    // Which of the two activity views is showing, remembered across launches.
    // The choice is about how the reader wants to read the day rather than
    // about anything happening in it, so having it reset every time the app is
    // rebuilt would make it feel like a mode that keeps slipping back.
    var grouped = UserDefaults.standard.bool(forKey: "activityGrouped")
    // What the pointer is on inside the activity list, shared by that list's
    // rows. Held here only so it outlives build() and is replaced by the next
    // one, alongside the views it drives.
    var hover: SessionHover?
    // The two lists, both in the menu at once with one of them hidden, and the
    // two items that say which is which. Held so the toggle can flip the
    // visibility of rows that are already on screen instead of rebuilding the
    // menu around them -- an open menu does not survive being emptied.
    // Replaced on every build; emptied when the day has no activity at all, so
    // nothing here can outlive the items it names.
    var rawItems: [NSMenuItem] = []
    var sessionItems: [NSMenuItem] = []
    var activityHeader: NSMenuItem?
    var activityToggle: ToggleRowView?
    // One panel for the app's lifetime -- see SessionPopover for why it is a
    // panel and not the submenu it replaced.
    let popover = SessionPopover()
    let focusLog = FocusLog()
    var audioTimer: Timer?
    var detector = CallDetector(minCallSec: MIN_CALL_SEC, settleSec: SETTLE_SEC)
    // Non-nil only while a countdown is on screen.
    var countdown: CountdownPanel?
    // Non-nil only while the track-back panel is on screen. Held so a second
    // double press raises the panel already up rather than stacking a new one
    // behind it, each with its own copy of the number being typed.
    var trackPanel: TrackPanel?
    // The single-press action, scheduled but not yet run. Its existence IS the
    // "a press is pending" flag: a second press cancels it and opens the panel
    // instead. Cleared by the work item itself so a press that has already
    // fired cannot be cancelled retroactively by a much later one.
    var pendingEntry: DispatchWorkItem?
    // The same thing for ⌘E: scheduled-but-not-yet-run End Now, whose
    // existence is the "a press is pending" flag a second press cancels.
    var pendingEnd: DispatchWorkItem?
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
    // Whether the dropdown is on screen. ⌘⌥S now exists twice -- as the Carbon
    // hot key and as the toggle row's key equivalent -- and an open menu
    // matches its own key equivalents while the hot key manager goes on
    // dispatching the chord regardless. Both would fire, so a shift would be
    // started and immediately ended by one press. While the menu is up it owns
    // the chord and the hot key stands down.
    var menuIsOpen = false
    // Holds the poll to one probe at a time. See SingleFlight for what the
    // unguarded version did.
    lazy var poll = SingleFlight { [weak self] in self?.runStatusProbe() }

    // The Carbon handler is a bare C function pointer and cannot capture, so
    // it reaches the app through the `bar` global rather than through self.
    // It fires on the event thread; the toggle reads status and rebuilds the
    // menu, so it hops to main first.
    func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            // Read out of the event rather than assumed: one handler now
            // serves both hot keys, so acting without asking which fired
            // would make ⌥W end the shift.
            var id = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &id)
            DispatchQueue.main.async {
                switch id.id {
                case HOTKEY_ID_ENTRY: bar.entryHotKey()
                // Guarded like the shift toggle and for the same reason: the
                // End Now row carries ⌘E as its key equivalent, so with the
                // menu open both that row and this would fire.
                case HOTKEY_ID_END:   if !bar.menuIsOpen { bar.endHotKey() }
                default:              if !bar.menuIsOpen { bar.toggleShift() }
                }
            }
            return noErr
        }, 1, &spec, nil, nil)
        // Logged because a hot key that fails to register fails silently and
        // invisibly: the shortcut simply does nothing, which is indistinguishable
        // from the app being down or the probe erroring.
        let err = RegisterEventHotKey(HOTKEY_CODE, HOTKEY_MODS,
                                      EventHotKeyID(signature: OSType(0x574B_5453),
                                                    id: HOTKEY_ID_SHIFT),
                                      GetApplicationEventTarget(), 0, &hotKeyRef)
        FileHandle.standardError.write("hotkey cmd+opt+S register -> \(err)\n".data(using: .utf8)!)
        let entryErr = RegisterEventHotKey(ENTRY_HOTKEY_CODE, ENTRY_HOTKEY_MODS,
                                           EventHotKeyID(signature: OSType(0x574B_5453),
                                                         id: HOTKEY_ID_ENTRY),
                                           GetApplicationEventTarget(), 0, &entryHotKeyRef)
        FileHandle.standardError.write("hotkey opt+W register -> \(entryErr)\n".data(using: .utf8)!)
        let endErr = RegisterEventHotKey(END_HOTKEY_CODE, END_HOTKEY_MODS,
                                         EventHotKeyID(signature: OSType(0x574B_5453),
                                                       id: HOTKEY_ID_END),
                                         GetApplicationEventTarget(), 0, &endHotKeyRef)
        FileHandle.standardError.write("hotkey cmd+E register -> \(endErr)\n".data(using: .utf8)!)
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
        // The workspace centre, not the default one: these are posted by
        // NSWorkspace and nothing arrives on NotificationCenter.default.
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil, queue: .main
        ) { [weak self] note in
            // The key is part of the notification's contract. If it is ever
            // missing, say so rather than logging a row with no app in it --
            // an empty bundle is a switch to nothing, which the probe would
            // count as a switch all the same.
            guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey]
                as? NSRunningApplication
            else {
                FileHandle.standardError.write(
                    "activation notification without an app: \(note.userInfo ?? [:])\n"
                        .data(using: .utf8)!)
                return
            }
            self?.focusLog.sample(app: app)
        }
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
        // Broken is filled and red like the dot in the bar, not hollow amber:
        // this row is the caption on that dot, and the two disagreeing about
        // which state the app is in is worse than either colour alone.
        let symbol = status.state == "idle" || status.state == "unknown" ? "○" : "●"
        let color: NSColor = status.state == "marked" ? MARKED
            : status.state == "working" ? WORKING
            : status.state == "broken" ? BROKEN : AWAY

        // In focused mode the cutoff is always five minutes, so "2m since last
        // activity" already tells you how much silence the run has left. In
        // unfocused mode it does not: the cutoff is whatever the bout has
        // earned on the ramp so far, anywhere between one and five minutes, and
        // nothing on screen said which. That is exactly the reading that looked
        // wrong -- a bout switched to unfocused mid-run carries the width it
        // already earned, so it goes on counting through silences that the "1m,
        // widening to 5m" label implies would have ended it. Naming both halves
        // -- what is left, and of what -- makes the rule in force visible while
        // it is still in force.
        var why = status.why
        if status.mode == "unfocused", status.state == "working",
           let quiet = status.quietSec, let cutoff = status.gapAfterSec {
            // Ceil, so the countdown reads "1m left" for the whole final minute
            // rather than dropping to a 0m that never corresponds to anything:
            // the moment it truly reaches zero the state is no longer working
            // and this line is gone.
            let left = Int(ceil(Double(cutoff - quiet) / 60))
            if left > 0 {
                why += "  ·  \(left)m left of \(Int((Double(cutoff) / 60).rounded()))m"
            }
        }

        // The failing poll's own account of itself, red-state only. Three
        // lines and a count: what was run, how it ended, what it managed to
        // say, and how long this has been going on. Enough to tell a stall
        // from a traceback and to see whether it is one bad poll or the
        // afternoon -- which is the whole question a red dot asks and the one
        // thing it could not previously answer without opening a file in /tmp.
        var debug: [String] = []
        if status.state == "broken", let f = probeFail {
            debug = f.lines
            var run = probeFailures == 1 ? "1 poll failed"
                                         : "\(probeFailures) polls failed"
            // Clock times, not ages. "last good 22m ago" is a number to add to
            // the time to find out when this started, and when it started is
            // the thing to line up against what else happened -- the machine
            // going to sleep, the network dropping, a rebuild.
            if let since = probeFailSince { run += " since \(clock(since))" }
            if let good = lastGoodPollAt {
                run += ", last good \(clock(good))"
            } else {
                // Short because the row is one line: "none good since launch"
                // is six characters past what fits, and what it buys over
                // "none good yet" is a word the row above already implies.
                run += ", none good yet"
            }
            debug.append(run)
        }

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
        // Both views' strings go in the key, and which of the two is showing is
        // deliberately NOT in it: both lists are built every time and one of
        // them is hidden, so the toggle changes no item this key describes. It
        // used to be in here, back when flipping meant rebuilding -- and that
        // is exactly what a rebuild costs: emptying an open menu dismisses it
        // half a second later, which is the whole reason the flip now hides
        // rows instead.
        let key = ([worked, symbol, why, status.state, status.mode] + debug
                   + periods.map { p in
                       let s = periodStrings(p)
                       return s.top + "\u{1}" + s.what
                   }
                   + zip(status.activities, ages).map { a, age in
                       "\(age)\u{1}\(a.kind)\u{1}\(a.what)\u{1}\(a.n)"
                   }
                   // sessionKey covers the submenu's rows as well as the two
                   // visible lines -- see its own comment for why what is
                   // hidden behind a hover still has to be in here.
                   + status.sessions.map(sessionKey)
                  ).joined(separator: "\u{2}")
        if key == lastMenuKey { return }
        lastMenuKey = key

        let m = menu
        m.removeAllItems()
        // Dropped with the items they point at. A day with no activity builds
        // no list at all, and holding last build's rows would leave the toggle
        // flipping items that are no longer in any menu.
        rawItems = []
        sessionItems = []
        activityHeader = nil
        activityToggle = nil
        let w = NSMenuItem(title: worked, action: nil, keyEquivalent: "")
        w.isEnabled = false
        m.addItem(w)
        m.addItem(.separator())

        let st = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        // The wider row while the failure block is under it: the menu is
        // already that wide there, and this line is the one the block explains.
        st.view = StatusRowView(width: debug.isEmpty ? 300 : DEBUG_ROW_WIDTH,
                                symbol: symbol, text: why, color: color)
        m.addItem(st)

        // Directly under the status row it elaborates, above the day: while
        // this is showing there is no reading of the day to lead with, and
        // burying the reason under a list of yesterday's evidence would be
        // burying the only thing on screen that is actionable.
        for line in debug {
            let d = NSMenuItem(title: "", action: nil, keyEquivalent: "")
            d.view = DebugRowView(width: DEBUG_ROW_WIDTH, text: line)
            m.addItem(d)
        }
        if !debug.isEmpty {
            // Two ways out of the three-line summary, because the two failures
            // want different ones: a traceback is longer than the menu can
            // show and wants pasting somewhere, while a stall's evidence is the
            // run of them in the log rather than any single line.
            m.addItem(NSMenuItem(title: "Copy Probe Diagnostics",
                                 action: #selector(copyProbeDiagnostics),
                                 keyEquivalent: ""))
            m.addItem(NSMenuItem(title: "Open Bar Log",
                                 action: #selector(openBarLog), keyEquivalent: ""))
        }
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
        //
        // The same evidence can be read two ways, and which one is wanted
        // depends on the question. "Why is the dot green right now" is answered
        // by the newest few events; "what did this morning consist of" is
        // answered by the day gathered into its periods, where ten raw rows
        // reach back twenty minutes and eight sessions reach back hours. A
        // checked row under the header switches between them, and the header
        // says which is showing so the list is never ambiguous about it.
        if !status.activities.isEmpty {
            let head = NSMenuItem(title: "", action: nil, keyEquivalent: "")
            head.isEnabled = false
            m.addItem(head)
            activityHeader = head
            // A view row rather than a plain item, so clicking it leaves the
            // menu up -- see ToggleRowView. The key equivalent stays on the
            // item itself: ⌘G still works, and still closes the menu the way
            // every other shortcut in it does.
            let toggle = NSMenuItem(title: "Group into sessions",
                                    action: #selector(toggleGrouped), keyEquivalent: "g")
            let toggleRow = ToggleRowView(title: "Group into sessions",
                                          on: grouped, hint: "⌘G", width: 300)
            toggleRow.onClick = { [weak self] in self?.toggleGrouped() }
            toggle.view = toggleRow
            m.addItem(toggle)
            activityToggle = toggleRow

            // One per build, and replaced with the rows it belongs to: the old
            // views go when the menu is emptied, so a hover object kept across
            // builds would be holding rows that are no longer on screen.
            let hover = SessionHover()
            self.hover = hover

            // Both lists go into the menu and one of them is hidden. Building
            // only the view that is showing would mean the toggle had to add
            // and remove items from a menu that is on screen, and an open menu
            // does not survive that -- it dismisses itself a moment later.
            // Hiding costs one extra list built per rebuild, which is a dozen
            // labels off data already in hand.
            let sessions = status.sessions
            // Opening the panel is the menu's job, not the row's: only this
            // level knows the menu's own window, and the panel has to be
            // placed against that rather than against the row alone.
            //
            // Guarded on grouped because the raw rows share this hover object
            // and now exist even while the sessions list is the one showing.
            // Without the guard, pointing at a raw row would open a session
            // panel beside the list that is meant to answer hovers by marking
            // its own rows.
            hover.onEnter = { [weak self] id, view in
                guard let self else { return }
                guard self.grouped, let id, id < sessions.count, let view,
                      let window = view.window, let screen = window.screen
                else {
                    self.popover.hide()
                    return
                }
                self.popover.show(sessions[id],
                                  row: window.convertToScreen(
                                      view.convert(view.bounds, to: nil)),
                                  menu: window.frame,
                                  screen: screen.visibleFrame)
            }
            sessionItems = sessions.enumerated().map { i, s in
                let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
                let view = SessionRowView(s, width: 300)
                view.session = i
                view.hover = hover
                hover.rows.append(view)
                item.view = view
                m.addItem(item)
                return item
            }

            let ageWidth = columnWidth(ages)
            let kindWidth = columnWidth(status.activities.map(\.kind))
            let rowSessions = status.activities.map(\.session)
            rawItems = zip(status.activities, ages).enumerated().map { i, pair in
                let (a, age) = pair
                let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
                let view = ActivityRowView(a, width: 300, age: age,
                                           ageWidth: ageWidth, kindWidth: kindWidth)
                view.session = a.session
                view.hover = hover
                // The list is in time order and a session is a contiguous
                // stretch of it, so a run's ends are simply where the
                // neighbour's session differs. The last row of the list
                // counts as an end even when its session continues past
                // it: the bracket has to close where the list stops,
                // because nothing below is there to close it.
                view.groupFirst = i == 0 || rowSessions[i - 1] != a.session
                view.groupLast = i == rowSessions.count - 1
                    || rowSessions[i + 1] != a.session
                hover.rows.append(view)
                item.view = view
                m.addItem(item)
                return item
            }
            showActivityView()
            m.addItem(.separator())
        }

        // One toggle, never both. A second "Mark as working" while a mark is
        // already running just appends an identical open mark -- which is
        // exactly what happened on the first day this shipped: two clicks ten
        // seconds apart, two rows, because nothing in the menu said the first
        // one had taken. Showing only the move that applies makes the state
        // legible and the duplicate unreachable.
        //
        // One action, too: this row and ⌘⌥S are the same decision, so they run
        // the same code and the row advertises the chord. They used to differ
        // -- the row ended the shift now, the chord at the last event -- which
        // made the shortcut impossible to describe in the interface without
        // also explaining that it did something slightly different. The two
        // endings are still both reachable, by name, under End Session below.
        let toggle = NSMenuItem(title: status.state == "marked" ? "Stop working"
                                                                : "Start working",
                                action: #selector(toggleShift), keyEquivalent: "s")
        toggle.keyEquivalentModifierMask = [.command, .option]
        m.addItem(toggle)

        // Directly under the toggle because it is the toggle's other half.
        // "Start working" claims time from the click forward, which is the
        // wrong end of a stretch you have already walked back from: the
        // corridor conversation is over, and the minutes worth claiming are
        // the ones behind you. This claims those -- from where the last
        // session ended up to now -- so the gap closes and the two sessions
        // read as the one stretch they were.
        //
        // The minute is in the title rather than left to be discovered in the
        // period list afterwards. It is a claim on time that cannot be seen
        // being made, so it says how far back it reaches before it reaches.
        //
        // Greyed rather than hidden when there is nothing to link to. Hiding
        // it would make the menu's shape depend on how the morning happened to
        // go, and an item that comes and goes is harder to learn than one that
        // is always in the same place and sometimes dim.
        let link = NSMenuItem(title: status.linkFrom.map { "Link with Last Session (from \($0))" }
                                  ?? "Link with Last Session",
                              action: #selector(linkLastSession), keyEquivalent: "")
        m.addItem(link)

        // Always present, unlike the toggle's stop, which only appears once a
        // mark is running. A mark is not the only thing that keeps the day
        // open -- a meeting runs on the calendar's schedule and prompting
        // lights the dot with nothing marked at all -- so on most afternoons
        // there was no menu item at all that meant "that was the day". This
        // is that item, and it says which minute it means rather than picking
        // for you.
        // ⌘E goes on End Now and not on the host, because the host opens a
        // submenu rather than doing anything, and a chord on it would advertise
        // the choice rather than an ending. It goes on this row specifically
        // because this is what one press does; the other row is what two
        // presses do, and a menu cannot express that -- the same reason "Track
        // time…" below carries no key equivalent for ⌥W pressed twice. So the
        // second row's title is where the double press is written down.
        let endSub = NSMenu()
        for (title, atLast, key) in [("End Now", false, "e"),
                                     ("End After Last Entry (⌘E twice)", true, "")] {
            let mi = NSMenuItem(title: title, action: #selector(endSession(_:)),
                                keyEquivalent: key)
            mi.representedObject = atLast
            // Set here, not by the sweep at the bottom of build(): that walks
            // m.items, which is the top level only. A submenu item left with a
            // nil target falls back to the responder chain, finds nothing that
            // implements the selector, and AppKit draws the row permanently
            // greyed out -- the menu looks broken rather than acting broken.
            mi.target = self
            endSub.addItem(mi)
        }
        let endHost = NSMenuItem(title: "End Session", action: nil, keyEquivalent: "")
        endHost.submenu = endSub
        m.addItem(endHost)

        if status.why.hasPrefix("in ") {
            m.addItem(NSMenuItem(title: "Meeting ended early",
                                 action: #selector(endMeetingEarly), keyEquivalent: ""))
        }
        // Here as well as on ⌥W, because a shortcut nothing in the interface
        // mentions is a shortcut that is forgotten by the week after it ships.
        m.addItem(NSMenuItem(title: "Log an entry",
                             action: #selector(logEntry), keyEquivalent: "w"))
        // No key equivalent of its own: the shortcut is ⌥W pressed twice, and
        // a menu cannot express that. The row is here so the panel is
        // reachable by somebody who never learns the double press, and so the
        // double press is discoverable by somebody reading the menu.
        m.addItem(NSMenuItem(title: "Track time…",
                             action: #selector(showTrackPanel), keyEquivalent: ""))
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

    func menuWillOpen(_: NSMenu) { menuIsOpen = true }

    // The panel is a window of our own, so nothing takes it away when the menu
    // goes: without this it would be left floating over the desktop, beside a
    // menu that is no longer there. A row's mouseExited normally closes it
    // first, but a menu dismissed by a click elsewhere or by Escape never
    // delivers one.
    func menuDidClose(_: NSMenu) {
        menuIsOpen = false
        popover.hide()
        hover?.enter(nil)
    }

    // The probe shells out to prompt-count and Slack, so a poll can take a
    // second or two. On the main thread that freezes the menu bar for everyone.
    //
    // Coalesced as well as serialized -- see SingleFlight. Everything that
    // wants a fresh reading calls this; whether that becomes a subprocess is
    // the guard's decision.
    //
    // Main-thread only, which every caller already is: the poll timer,
    // menuNeedsUpdate, the Refresh row, and the completion hop of every action.
    func refresh() {
        poll.request()
    }

    private func runStatusProbe() {
        probeQueue.async {
            let run = runProbe(["status"])
            guard case .ok(let out) = run,
                  let d = out.data(using: .utf8),
                  let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any]
            else {
                // A poll that could not answer is NOT a reading of "idle".
                // Returning early here left the last dot on screen, so a probe
                // that had been failing since launch showed as a confident
                // hollow amber -- the tracker looked like it was working and
                // reporting an idle day, when in fact it was blind. A broken
                // poll gets its own glyph so it cannot be mistaken for one.
                //
                // Which failure it was goes on screen too. "probe did not
                // answer" was all this used to say, and it is the one sentence
                // that fits both a stall and a traceback -- so the afternoon
                // it mattered was spent in /tmp/worktime-bar.err working out
                // which of the two was on the dot.
                let fail: ProbeFailure
                if case .failed(let f) = run {
                    fail = f
                } else {
                    // Ran, exited 0, and what came back was not the status
                    // JSON. Nothing about the process was wrong, so it has no
                    // failure of its own to describe.
                    fail = ProbeFailure(args: ["status"], path: PROBE,
                                        stderr: "answer was not status JSON")
                }
                DispatchQueue.main.async {
                    var s = self.status
                    s.state = "broken"
                    s.why = fail.summary
                    self.probeFail = fail
                    self.probeFailures += 1
                    if self.probeFailSince == nil { self.probeFailSince = Date() }
                    self.apply(s)
                    self.poll.finish()
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
            s.linkFrom = j["link_from"] as? String
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
                         n: a["n"] as? Int ?? 1,
                         session: a["session"] as? Int)
            }
            s.sessions = (j["sessions"] as? [[String: Any]] ?? []).map { g in
                ActSession(start: g["start"] as? Int ?? 0,
                           end: g["end"] as? Int ?? 0,
                           len: g["len"] as? Int ?? 0,
                           what: g["what"] as? String ?? "",
                           counted: g["counted"] as? Bool ?? true,
                           current: g["current"] as? Bool ?? false,
                           n: g["n"] as? Int ?? 0,
                           // JSON pairs, not a dictionary, because the order is
                           // the payload's: the probe sorts biggest first so
                           // the smallest contributor is what truncates off the
                           // end of the line, and a dictionary would lose that.
                           kinds: (g["kinds"] as? [[Any]] ?? []).compactMap {
                               guard let k = $0.first as? String,
                                     let n = $0.last as? Int else { return nil }
                               return (k, n)
                           },
                           rows: (g["rows"] as? [[String: Any]] ?? []).map { r in
                               SessionRow(t: r["t"] as? String ?? "",
                                          kind: r["kind"] as? String ?? "",
                                          what: r["what"] as? String ?? "",
                                          n: r["n"] as? Int ?? 1)
                           })
            }
            DispatchQueue.main.async {
                self.probeFail = nil
                self.probeFailures = 0
                self.probeFailSince = nil
                self.lastGoodPollAt = Date()
                self.apply(s)
                self.poll.finish()
            }
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
        // A dot and only a dot, in every state including red. Words beside it
        // were tried and are not worth their room: the menu bar is shared with
        // a dozen other icons, a red dot is already the thing the eye catches,
        // and what a failure needs said about it is more than a tag's worth --
        // which is what the menu holds. The tooltip carries the whole failure
        // for a pointer that pauses on the way there.
        item.button?.imagePosition = .imageOnly
        item.button?.toolTip = s.state == "broken" ? probeFail?.report ?? s.why
            : "\(s.state) — \(s.why) (as of \(s.at))"
        build()
    }

    // ⌘⌥S, and the toggle row that names it. Starts a shift, or ends the
    // running one at the last event the probe saw -- not at this keypress: the
    // chord gets pressed on the way out the door, and the minutes between the
    // last prompt and the press are the leaving, not the work. Ending at this
    // minute instead is End Session's "End Now", which is a thing you ask for
    // by name rather than a difference between two ways of doing one thing.
    // Reading the state on main before dispatching keeps the decision and the
    // menu's rendering of it from disagreeing.
    @objc func toggleShift() {
        let running = status.state == "marked"
        probeQueue.async {
            _ = runProbe(running ? ["unmark", "last"] : ["mark"])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // ⌥W. One keypress, no window, no question: it records that this minute
    // was worked and nothing else. Deliberately not a text prompt -- the value
    // of the shortcut is that it costs nothing to press mid-thought, and a
    // dialog that takes the caret to ask what you are doing is an interruption
    // of the very work it is trying to record.
    @objc func logEntry() {
        probeQueue.async {
            // Only claims what actually happened. runProbe reports its own
            // failure to stderr; a banner saying the minute was logged when
            // the write never landed would be worse than no banner at all,
            // because it is the thing being trusted instead of checking.
            guard case .ok = runProbe(["note"]) else { return }
            // Off the probe queue: the banner is an osascript of its own, and
            // holding a serial queue that every poll and every clicked row now
            // waits behind, for the sake of a notification nothing depends on,
            // would trade the pileup for a smaller one.
            DispatchQueue.global(qos: .utility).async { notifyEntryLogged() }
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // ⌥W, before it is known which of the two things it means. One press logs
    // an entry; two open the track-back panel.
    //
    // Which means the single press cannot act until the double press has been
    // ruled out, so it is scheduled rather than run. The alternative -- log
    // immediately, and open the panel as well if a second press arrives --
    // needs no delay but leaves a stray entry behind every trip to the panel,
    // at a minute the person did not mean to claim and with nothing to
    // distinguish it from an entry they did.
    //
    // Always on main: the Carbon handler hops here before calling this, and
    // pendingEntry is read and written from nowhere else, so the cancel and
    // the fire cannot race.
    @objc func entryHotKey() {
        if let pending = pendingEntry {
            pending.cancel()
            pendingEntry = nil
            showTrackPanel()
            return
        }
        let work = DispatchWorkItem { [weak self] in
            self?.pendingEntry = nil
            self?.logEntry()
        }
        pendingEntry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + DOUBLE_PRESS_SEC, execute: work)
    }

    // ⌥W twice, and the menu row that names it. Asks for a number of minutes
    // and what to do about work already counted inside them, then hands both
    // to the probe, which owns the arithmetic -- the panel neither knows nor
    // decides where the minutes land.
    @objc func showTrackPanel() {
        // Asked of the window, not of this reference. Holding a TrackPanel is
        // not the same as one being on screen -- a cancelled panel is a live
        // object with nothing visible -- and testing the reference is how the
        // shortcut broke the first time: cancel it once and every later double
        // press took this branch, activating the app and showing nothing.
        if trackPanel?.isOnScreen == true {
            // Genuinely up. Raising it is the whole response: opening a second
            // one would put two half-typed counts on screen and bank whichever
            // got its return key first.
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        trackPanel = TrackPanel(onClose: { [weak self] in
            self?.trackPanel = nil
        }, onTrack: { [weak self] minutes, mode in
            self?.trackPanel = nil
            self?.trackBack(minutes: minutes, mode: mode)
        })
    }

    // Runs the claim and reports what actually landed, which is not always
    // what was asked for: "clip" stops at the last tracked session, so five
    // minutes can bank two. Saying so is the point -- the difference between
    // the two rules is invisible in the period list unless the banner names
    // it, and a person who cannot see which rule ran cannot tell they picked
    // the wrong one.
    func trackBack(minutes: Int, mode: String) {
        probeQueue.async {
            guard case .ok(let out) = runProbe(["track", String(minutes), mode]),
                  let data = out.data(using: .utf8),
                  let r = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return }
            let claimed = r["claimed"] as? Int ?? 0
            // Off the probe queue, same as the entry banner.
            DispatchQueue.global(qos: .utility).async {
                notifyTracked(claimed: claimed, asked: minutes)
            }
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // The clicked item carries the mode name, so both rows share one action and
    // neither can drift from the title beside its own checkmark.
    // Purely a way of looking at what the last poll already returned, so this
    // asks the probe for nothing and changes nothing but which rows are
    // hidden. Deliberately NOT a rebuild: emptying the menu to put the other
    // list in dismisses it, which is what made this a one-view-per-opening
    // choice before.
    @objc func toggleGrouped() {
        grouped.toggle()
        UserDefaults.standard.set(grouped, forKey: "activityGrouped")
        showActivityView()
    }

    // Applies `grouped` to rows that are already in the menu: the two lists and
    // the two labels that name which one is being read. Called at the end of
    // every build, so a fresh menu opens on the remembered view, and on every
    // flip, so an open one changes under the pointer.
    func showActivityView() {
        for i in rawItems { i.isHidden = grouped }
        for i in sessionItems { i.isHidden = !grouped }
        activityHeader?.title = grouped ? "Recent activity — sessions"
                                        : "Recent activity"
        activityToggle?.setOn(grouped)
        // The panel belongs to the sessions list, so leaving the sessions view
        // has to take it with it -- the row it was opened from is now hidden
        // and will never deliver the mouseExited that would have closed it.
        if !grouped { popover.hide() }
    }

    @objc func pickMode(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        probeQueue.async {
            _ = runProbe(["mode", name])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // End Session. Closes whatever is holding the day open -- an open mark, a
    // meeting still running on the calendar's schedule -- at one minute, either
    // this one or the last entry the tracker saw. The probe decides which
    // minute "the last entry" is, using the same rule the shift stop uses, so
    // there is one answer to that question and not two.
    @objc func endSession(_ sender: NSMenuItem) {
        // Logged rather than defaulted: the two rows mean different minutes, so
        // guessing one would silently end the day at a time nobody asked for.
        guard let atLast = sender.representedObject as? Bool else {
            FileHandle.standardError.write(
                "end session: row carried no minute choice\n".data(using: .utf8)!)
            return
        }
        endSession(atLast: atLast)
    }

    // ⌘E, before it is known which of the two endings it means. One press ends
    // the session at this minute; two end it at the last entry.
    //
    // Which means the single press cannot act until the double press has been
    // ruled out, so it is scheduled rather than run -- the same shape, and for
    // the same reason, as ⌥W. Here the reason is sharper: acting immediately
    // and then also acting on the second press would declare the day over
    // twice, at two different minutes, and the second declaration cannot undo
    // the first. `split_at_session_ends` cuts at every minute in the file, so
    // the stray End Now would go on breaking the period at a minute nobody
    // chose, with nothing in the menu to say where it came from.
    //
    // Always on main: the Carbon handler hops here before calling this, and
    // pendingEnd is read and written from nowhere else, so the cancel and the
    // fire cannot race.
    @objc func endHotKey() {
        if let pending = pendingEnd {
            pending.cancel()
            pendingEnd = nil
            endSession(atLast: true)
            return
        }
        let work = DispatchWorkItem { [weak self] in
            self?.pendingEnd = nil
            self?.endSession(atLast: false)
        }
        pendingEnd = work
        DispatchQueue.main.asyncAfter(deadline: .now() + END_DOUBLE_PRESS_SEC, execute: work)
    }

    // The declaration itself, reached from the two rows and from ⌘E. Split out
    // so the chord does not have to invent an NSMenuItem to carry a Bool the
    // row-clicked path reads back out of one.
    func endSession(atLast: Bool) {
        probeQueue.async {
            // The minute comes back out of the probe rather than being
            // computed again here. `at_last` resolves to a minute only the
            // probe knows -- last_entry_end reads the day's events -- and a
            // banner that named a locally-guessed time would eventually
            // advertise one minute while the file recorded another.
            guard case .ok(let out) = runProbe(atLast ? ["end_session", "last"] : ["end_session"]),
                  let data = out.data(using: .utf8),
                  let r = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let at = r["at"] as? String
            else {
                // Said out loud rather than passed over: ending the day is the
                // one action whose whole visible result is the banner, so a
                // silent failure here looks exactly like a hot key that never
                // fired.
                FileHandle.standardError.write(
                    "end session: probe returned no minute\n".data(using: .utf8)!)
                return
            }
            // Off the probe queue, same as the entry banner: the banner is an
            // osascript of its own and every poll queues behind this one.
            DispatchQueue.global(qos: .utility).async {
                notifySessionEnded(at: at, atLast: atLast)
            }
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // Link with Last Session. Claims the gap between where the last session
    // ended and now as work, and leaves the claim open so the stretch it just
    // rejoined keeps running. Which minute counts as "where the last session
    // ended" is the probe's to decide -- it is the one that drew the period
    // list -- so this passes no minute and cannot name a different one than
    // the title advertised.
    @objc func linkLastSession() {
        probeQueue.async {
            // A refusal is not supposed to be reachable -- the item greys out
            // on the same None the probe refuses for -- but the menu can go
            // stale while it is open, and a click that lands then would
            // otherwise be indistinguishable from one that worked. The log is
            // where that difference gets recorded.
            if case .ok(let out) = runProbe(["link_last"]),
               let data = out.data(using: .utf8),
               let r = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               r["linked"] as? Bool == false {
                FileHandle.standardError.write(
                    "link last: \(r["why"] as? String ?? "refused")\n"
                        .data(using: .utf8)!)
            }
            DispatchQueue.main.async { self.refresh() }
        }
    }

    // The menu auto-enables, which ignores isEnabled on any item that has an
    // action -- so the link row has to be greyed from here. Everything else
    // keeps the default: items with no action grey themselves, and every other
    // action row applies whenever the menu is open.
    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        item.action == #selector(linkLastSession) ? status.linkFrom != nil : true
    }

    @objc func endMeetingEarly() {
        probeQueue.async {
            _ = runProbe(["meeting_end"])
            DispatchQueue.main.async { self.refresh() }
        }
    }

    @objc func refreshNow() { refresh() }

    // The whole failure, not the clipped three lines the menu shows: a
    // traceback pasted into a message or an editor is the difference between
    // reporting "the dot is red" and reporting what broke.
    @objc func copyProbeDiagnostics() {
        // Nil is reachable and is not a bug: the row is built only while a
        // failure is current, and a poll that succeeds while the menu is open
        // clears it under the click. There is nothing to copy at that point,
        // and the dot has already gone back to a colour.
        guard let f = probeFail else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(f.report, forType: .string)
    }

    // Opens in whatever reads a .err, which on this machine is Console. The
    // log holds every failure rather than the current one, so it is where a
    // run of them is read -- when it started, and whether it ever stopped.
    @objc func openBarLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: BAR_LOG))
    }
    @objc func quit() { NSApplication.shared.terminate(nil) }
}

let app = NSApplication.shared
// Accessory, so it lives only in the menu bar: no Dock icon, no app switcher
// entry, and it never steals focus.
app.setActivationPolicy(.accessory)
let bar = Bar()
app.delegate = bar
app.run()
