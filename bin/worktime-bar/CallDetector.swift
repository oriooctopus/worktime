// When a call started and when it ended, from nothing but a repeated yes/no
// reading of "is some process capturing audio input right now".
//
// Deliberately pure: no CoreAudio, no timers, no clock of its own. Everything
// it knows arrives through update(), which makes the debounce rules -- the part
// with the edge cases -- testable without a microphone or a meeting. The
// CoreAudio reading that feeds it lives in main.swift.

import Foundation

struct CallDetector {
    // A run of input capture shorter than this was not a meeting. Siri, a
    // notification chime being sampled, a "can you hear me" in another app,
    // Photo Booth opening -- all are seconds long. Without this floor the
    // countdown would appear after every incidental use of the microphone.
    let minCallSec: Double

    // How long input must stay quiet before the call counts as over. This is
    // not for the HAL, which clears DeviceIsRunningSomewhere about a quarter of
    // a second after the capturing process exits; it is for the gap while audio
    // moves between devices. Switching from AirPods to the built-in microphone
    // mid-meeting takes a moment during which nothing is capturing, and that
    // must not read as the meeting ending.
    let settleSec: Double

    // Start of the current unbroken run of capture, nil while quiet.
    private var runStart: Date?
    // Start of the current unbroken quiet stretch, nil while capturing.
    private var quietStart: Date?
    // Whether the call in progress (or the one that just went quiet) ever ran
    // long enough to count. Set once and deliberately NOT cleared when capture
    // blips off, so a device handoff cannot demote a real meeting back to noise
    // and make the run re-earn its minute from scratch.
    private var armed = false

    init(minCallSec: Double, settleSec: Double) {
        self.minCallSec = minCallSec
        self.settleSec = settleSec
    }

    /// Feed one reading. Returns true on the single tick where a call that had
    /// been running is judged to have ended.
    mutating func update(capturing: Bool, now: Date) -> Bool {
        if capturing {
            quietStart = nil
            if runStart == nil { runStart = now }
            if let start = runStart, now.timeIntervalSince(start) >= minCallSec {
                armed = true
            }
            return false
        }

        runStart = nil
        // Quiet, but nothing worth calling a meeting has happened yet.
        guard armed else {
            quietStart = nil
            return false
        }
        guard let since = quietStart else {
            quietStart = now
            return false
        }
        guard now.timeIntervalSince(since) >= settleSec else { return false }
        // Fires exactly once: disarming here is what stops every subsequent
        // quiet tick from reporting the same meeting ending over and over.
        armed = false
        quietStart = nil
        return true
    }

    /// True while a run has lasted long enough to be treated as a call. The
    /// menu bar uses it to tell "the meeting resumed" from "the microphone
    /// twitched" when deciding whether to withdraw a countdown already on
    /// screen.
    var inCall: Bool { armed }
}
