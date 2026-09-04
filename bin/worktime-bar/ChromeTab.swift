// Asking Chrome what page is in front, and making sense of the answer.
//
// Split out of main.swift because both halves have now failed silently in
// production -- once in the script, once in the parse -- and neither was
// reachable by a test while they lived in the file that defines `main`. The
// Swift suites replace main.swift with their own entry point, so anything
// there is, by construction, the part of the app nothing can check.
import Foundation

// The osascript argv. Two halves, tab-separated: a tab is the one character a
// page title reliably does not carry, so it survives a title made entirely of
// punctuation.
//
// The delimiter is built in its own statement, OUTSIDE the tell block, and
// that is the whole reason tab capture ever worked. Inside
// `tell application "Google Chrome"`, `tab` is not AppleScript's tab
// character -- it is Chrome's own `tab` class, which the terminology of the
// application being told shadows it with, and concatenating a class into a
// string yields the word. Every reply used to come back as
// "Inbox - Gmailtabhttps://mail.google.com/...", exit status 0, and the parse
// below quietly refused all of them.
let CHROME_TAB_SCRIPT = [
    "-e", "set delim to ASCII character 9",
    "-e", "tell application \"Google Chrome\" to set answer to "
        + "(title of active tab of front window) & delim & "
        + "(URL of active tab of front window)",
    "-e", "return answer",
]

/// The title and address in a reply from CHROME_TAB_SCRIPT, or nil if there
/// is no page in it.
///
/// The address is what must be there. The title need not be: a page that has
/// not finished loading has no title at all, and Workday's login page never
/// has one, so the reply is "\thttps://wd5.myworkday.com/...". Trimming that
/// before splitting -- which is what the first version did -- eats the
/// leading tab along with the newline, leaves one component, and throws away
/// a perfectly good address for the sake of a title nobody needed. An
/// untitled page is still a page; the probe names it by its app instead.
func parseChromeTabReply(_ text: String) -> (title: String, url: String)? {
    // Trailing only. osascript ends its reply with a newline, and the leading
    // whitespace here is data.
    var reply = text
    while let last = reply.last, last == "\n" || last == "\r" {
        reply.removeLast()
    }
    let parts = reply.components(separatedBy: "\t")
    guard parts.count == 2 else { return nil }
    let url = parts[1].trimmingCharacters(in: .whitespacesAndNewlines)
    guard !url.isEmpty else { return nil }
    return (parts[0].trimmingCharacters(in: .whitespacesAndNewlines), url)
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
