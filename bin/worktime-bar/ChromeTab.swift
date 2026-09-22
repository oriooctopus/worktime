// Asking Chrome what page is in front, and making sense of the answer.
//
// Split out of main.swift because both halves have now failed silently in
// production -- once in the script, once in the parse -- and neither was
// reachable by a test while they lived in the file that defines `main`. The
// Swift suites replace main.swift with their own entry point, so anything
// there is, by construction, the part of the app nothing can check.
import Foundation
import ScriptingBridge

// The read is addressed at a PROCESS, not at the name "Google Chrome", and
// that is the whole point of this file.
//
// It used to run `osascript -e 'tell application "Google Chrome" to ...'`.
// An Apple Event addressed by name is delivered to whichever instance of the
// bundle the system picks, and there is routinely more than one: a launchd
// agent keeps a CDP browser on :9222 for Playwright to attach to, and
// `playwright --browser=chrome` launches the SAME binary again for every
// automated run. All of them are com.google.Chrome.
//
// So the reply came back from a browser nobody was looking at. Verified on
// 2026-09-22: frontmost was the daily-driver Chrome on pools.events, the
// script answered "Log In / http://localhost:3000/" from the automation
// instance, and the focus log recorded the automation's page as the user's
// for every Chrome sample since the agent started the evening before.
//
// Addressing the event at a pid removes the ambiguity rather than guessing
// at it: NSWorkspace names the process that is frontmost, and that exact
// process is the one asked. A second Chrome can do as it likes.

// Ticks, at 60 per second -- SBApplication counts its timeout in them. The
// wall this puts up is the same one the osascript version had, and it is not
// tuned for the healthy case: it is the guard against a browser wedged behind
// a modal, where the reply never comes and would otherwise hang the sample.
let CHROME_TAB_TIMEOUT_TICKS = 120

/// The page in front of one specific Chrome process, or nil if there is none.
///
/// Nil is the honest answer to every failure here -- no windows, a refused
/// Apple Event, a wedged browser, a process that is not scriptable -- and a
/// Chrome sample with no tab on it earns nothing, which is what every Chrome
/// sample earned before any of this existed. The one thing it must never do
/// is fall back to asking "Google Chrome" by name: that is the bug.
func chromeActiveTab(pid: pid_t) -> (title: String, url: String)? {
    guard let app = SBApplication(processIdentifier: pid) else {
        reportChromeTabFailure("pid \(pid) is not scriptable")
        return nil
    }
    app.timeout = CHROME_TAB_TIMEOUT_TICKS
    guard let windows = app.value(forKey: "windows") as? [AnyObject] else {
        reportChromeTabFailure("pid \(pid) would not list its windows")
        return nil
    }
    // Chrome orders `windows` front to back, so the first is AppleScript's
    // `front window`. No windows at all is ordinary -- a browser open with
    // nothing on screen -- and not worth a line on stderr.
    guard let front = windows.first else { return nil }
    let tab = front.value(forKey: "activeTab") as AnyObject?
    return chromeTabFields(title: tab?.value(forKey: "title") as? String,
                           url: tab?.value(forKey: "URL") as? String)
}

/// A title and address as a page, or nil if there is no page in them.
///
/// The address is what must be there. The title need not be: a page that has
/// not finished loading has no title at all, and Workday's login page never
/// has one. An untitled page is still a page; the probe names it by its app
/// instead. Discarding it for the sake of a title nobody needed throws away a
/// perfectly good address -- which is what an earlier version of this did.
func chromeTabFields(title: String?, url: String?) -> (title: String, url: String)? {
    guard let url = url?.trimmingCharacters(in: .whitespacesAndNewlines),
          !url.isEmpty
    else { return nil }
    return ((title ?? "").trimmingCharacters(in: .whitespacesAndNewlines), url)
}

// Whether a failed tab read has already been reported this launch.
var chromeTabFailureReported = false

/// Say why no page could be read -- once per launch.
///
/// Once, because none of the reasons can be fixed from here: a refused Apple
/// Event is granted in System Settings, a browser with no windows opens one
/// when somebody does, and the poll would otherwise repeat the same line
/// every few seconds until then.
///
/// At all, because silence is the failure mode both of these bugs shipped
/// behind. A Chrome sample written with no tab and no url is indistinguishable
/// in the focus log from a machine whose owner never opened a browser, and
/// nothing else in the system was ever going to notice the difference.
func reportChromeTabFailure(_ text: String) {
    guard !chromeTabFailureReported else { return }
    chromeTabFailureReported = true
    let detail = text.trimmingCharacters(in: .whitespacesAndNewlines)
    FileHandle.standardError.write(Data(
        ("worktime-bar: cannot read the front Chrome tab, so no browser page "
         + "can count. Chrome said: \(detail.isEmpty ? "<nothing>" : detail)\n")
        .utf8))
}
