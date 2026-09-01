// Tests for CallDetector's debounce rules.
//
// Driven by a simulated clock rather than real time, so the whole suite runs in
// microseconds and a two-hour meeting costs nothing to express. Compiled and
// run by tests/test_call_detector.py so it lands in the same `pytest tests`
// everything else does -- a Swift test nobody's runner invokes is a file, not a
// test.

import Foundation

var failures: [String] = []

func check(_ cond: Bool, _ what: String) {
    if !cond { failures.append(what) }
}

let MIN_CALL = 60.0
let SETTLE = 5.0

/// Drives a detector over a script of (seconds to advance, capturing) steps,
/// sampling once per second the way the menu bar's timer does, and returns the
/// elapsed times at which it reported a call ending.
func run(_ script: [(Double, Bool)], minCall: Double = MIN_CALL,
         settle: Double = SETTLE) -> [Double] {
    var d = CallDetector(minCallSec: minCall, settleSec: settle)
    let origin = Date(timeIntervalSince1970: 1_700_000_000)
    var t = 0.0
    var fired: [Double] = []
    for (duration, capturing) in script {
        let end = t + duration
        while t < end {
            if d.update(capturing: capturing, now: origin.addingTimeInterval(t)) {
                fired.append(t)
            }
            t += 1.0
        }
    }
    return fired
}

// Swift allows loose statements only in a file called main.swift, and this one
// is compiled alongside the app's own main.swift, so the checks live in a
// @main entry point instead.
@main
enum CallDetectorTests {
static func main() {

// A brief touch of the microphone is not a meeting. Siri, a chime, opening
// Photo Booth -- none of these should ever produce a countdown.
check(run([(30, true), (60, false)]).isEmpty,
      "a 30s burst of capture was treated as a meeting")

// The ordinary case: a real call, then silence.
let ordinary = run([(600, true), (60, false)])
check(ordinary.count == 1, "a finished call fired \(ordinary.count) times, want 1")
if let first = ordinary.first {
    // Fires once the quiet stretch has lasted settleSec, not the instant
    // capture stopped.
    check(first >= 600 + SETTLE && first <= 600 + SETTLE + 1.0,
          "fired at t+\(first), want about t+\(600 + SETTLE)")
}

// Staying quiet must not keep re-reporting the same meeting.
check(run([(600, true), (3600, false)]).count == 1,
      "an hour of silence after one call fired more than once")

// A device handoff mid-meeting: AirPods drop, the built-in microphone takes
// over a few seconds later. Shorter than the settle, so nothing fires.
check(run([(600, true), (3, false), (600, true), (2, false)]).isEmpty,
      "a 3s gap between capture devices ended the meeting")

// The same handoff, followed by the meeting actually ending.
let handoff = run([(600, true), (3, false), (600, true), (60, false)])
check(handoff.count == 1, "a call with one handoff fired \(handoff.count) times, want 1")

// After a handoff the run does NOT have to re-earn its minute: a meeting that
// resumes and then ends 10 seconds later has still ended.
let shortTail = run([(600, true), (3, false), (10, true), (60, false)])
check(shortTail.count == 1,
      "a meeting that ended shortly after a handoff was dropped as too short")

// Two separate calls in a day are two separate endings.
let twice = run([(600, true), (60, false), (600, true), (60, false)])
check(twice.count == 2, "two calls produced \(twice.count) endings, want 2")

// A short burst before a real call must not arm anything on its own, and must
// not stop the real call being detected.
let afterNoise = run([(10, true), (30, false), (600, true), (60, false)])
check(afterNoise.count == 1,
      "a short burst before a real call produced \(afterNoise.count) endings, want 1")

// inCall tracks whether there is a meeting worth withdrawing a countdown for.
var d = CallDetector(minCallSec: MIN_CALL, settleSec: SETTLE)
let origin = Date(timeIntervalSince1970: 1_700_000_000)
check(!d.inCall, "a fresh detector claimed to be in a call")
_ = d.update(capturing: true, now: origin)
check(!d.inCall, "one second of capture already counted as a call")
_ = d.update(capturing: true, now: origin.addingTimeInterval(MIN_CALL))
check(d.inCall, "a full minute of capture did not count as a call")
_ = d.update(capturing: false, now: origin.addingTimeInterval(MIN_CALL + 1))
check(d.inCall, "a one-second dropout ended the call immediately")
_ = d.update(capturing: false, now: origin.addingTimeInterval(MIN_CALL + 1 + SETTLE))
check(!d.inCall, "the call stayed open after it was reported as ended")

if failures.isEmpty {
    print("call detector: all checks passed")
} else {
    for f in failures { print("FAIL: \(f)") }
    exit(1)
}

}  // static func main
}  // enum CallDetectorTests
