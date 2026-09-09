import Foundation

// The wording of a failed poll, driven rather than read.
//
// These strings are the whole feature: the red dot has one colour and two very
// different causes, and what the menu says is the only thing that separates
// them. Every one of them describes a state that takes a stalled filesystem or
// a broken probe to reproduce by hand, which is exactly why the sentences are
// asserted here instead of being checked the next time an afternoon goes red.

var failures = 0

func check(_ ok: Bool, _ what: String) {
    if ok {
        print("ok   \(what)")
    } else {
        print("FAIL \(what)")
        failures += 1
    }
}

func has(_ haystack: String, _ needle: String, _ what: String) {
    check(haystack.contains(needle), "\(what) (in \"\(haystack)\")")
}

let PATH = "/Users/x/.claude/bin/worktime-probe.py"

func stalled() -> ProbeFailure {
    // What the watchdog's kill actually looks like: SIGTERM, nothing on stderr,
    // and the full timeout on the clock.
    ProbeFailure(args: ["status"], path: PATH, status: 15, stderr: "",
                 elapsed: 30.04, killed: true)
}

let TRACEBACK = """
Traceback (most recent call last):
  File "/Users/x/.claude/bin/worktime-probe.py", line 1402, in <module>
    main()
  File "/Users/x/.claude/bin/worktime-probe.py", line 1390, in main
    print(json.dumps(status()))
KeyError: 'periods'
"""

func crashed() -> ProbeFailure {
    ProbeFailure(args: ["status"], path: PATH, status: 1, stderr: TRACEBACK,
                 elapsed: 0.42)
}

// A stall and a crash must not read alike anywhere. This is the distinction the
// dot could not make: both were "probe did not answer", and one wants the
// filesystem looked at while the other wants a traceback read.
func testStallAndCrashReadDifferently() {
    check(stalled().summary != crashed().summary,
          "a stall and a crash carry different summaries")
    check(stalled().lines != crashed().lines,
          "a stall and a crash carry different detail")
}

func testStalledSaysItWasKilled() {
    let f = stalled()
    has(f.summary, "killed", "a killed probe is described as killed")
    has(f.summary, "30.0s", "the summary says how long it hung")
    has(f.lines.joined(separator: "\n"), "watchdog",
        "the detail names the watchdog that killed it")
    has(f.lines.joined(separator: "\n"), "no traceback",
        "the detail says there is no traceback to look for")
    // The whole point of the row: it says where to look, and where turned out
    // to matter -- the deployed probe is a symlink into a guarded folder.
    has(f.lines[0], PATH, "the first line names the probe that was run")
    has(f.lines[0], "status", "the first line names the arguments")
}

// exit 15 without the watchdog firing is somebody else's SIGTERM, and calling
// that a stall would send the reader to the wrong place.
func testUnkilledSignalIsNotCalledAStall() {
    let f = ProbeFailure(args: ["status"], path: PATH, status: 15,
                         stderr: "", elapsed: 3.1, killed: false)
    let said = f.summary + f.lines.joined(separator: "\n")
    check(!said.contains("stalled"), "an unkilled exit 15 is not called a stall")
    has(said, "15", "it is still reported as exit 15")
    has(said, "3.1s", "the detail says how long the run lasted")
}

func testCrashedCarriesTheException() {
    let f = crashed()
    // The exit status has a row of its own; the one line beside the bullet
    // spends itself on the exception instead.
    has(f.lines.joined(separator: "\n"), "exit 1", "the exit status has a row")
    check(!f.summary.contains("exit 1"),
          "the status line does not repeat the exit status (got \(f.summary))")
    // The exception, not the first line of the traceback: "Traceback (most
    // recent call last)" is the same sentence for every failure there is.
    has(f.summary, "KeyError", "the summary carries the exception, not the header")
    has(f.lines.joined(separator: "\n"), "KeyError: 'periods'",
        "the detail carries the exception")
    check(f.lines.count <= 5, "the detail stays inside five rows (got \(f.lines.count))")
}

// The caret row is what makes the three-line budget bite: two of the three
// rows lifted from a real traceback were "~~~^^^^^^" the first time this was
// pointed at a probe that raises.
func testCaretUnderlinesAreNotRows() {
    let f = ProbeFailure(args: ["status"], path: PATH, status: 1, stderr: """
        return day["periods"]
            ~~~^^^^^^^^^^^
        KeyError: 'periods'
        """, elapsed: 0.4)
    check(f.stderrTail == ["return day[\"periods\"]", "KeyError: 'periods'"],
          "the caret underline is dropped (got \(f.stderrTail))")
}

// The failure that was actually on the dot: the day's Slack cache did not
// exist yet, the machine was offline, and DNS for slack.com failed. "exit 1"
// was every word the menu bar had for it.
func testTheExceptionIsNamedWithoutItsModule() {
    let f = ProbeFailure(args: ["status"], path: PATH, status: 1, stderr: """
        urllib.error.URLError: <urlopen error [Errno 8] nodename nor \
        servname provided, or not known>
        """, elapsed: 1.2)
    check(f.exception?.name == "URLError",
          "the class is named (got \(f.exception?.name ?? "nil"))")
    has(f.summary, "URLError:", "the summary names the class")
    check(f.summary.hasPrefix("URLError:"),
          "the status line leads with the exception (got \(f.summary))")
    check(!f.summary.contains("urllib.error"),
          "the module path does not eat the message's room")
    has(f.summary, "Errno 8", "enough of the message survives to be a clue")
}

func testAnExceptionWithNoMessageIsStillNamed() {
    let f = ProbeFailure(args: ["status"], path: PATH, status: 1,
                         stderr: "KeyboardInterrupt", elapsed: 1)
    check(f.exception?.name == "KeyboardInterrupt",
          "a class with no message is still a class")
    has(f.summary, "KeyboardInterrupt", "a bare class is the summary")
    check(f.exception?.message == "", "and carries no message")
}

// Not everything ending a stderr is an exception. A tag reading "probe make"
// or "probe Error" would be worse than the exit code it replaced.
func testProseIsNotMistakenForAnException() {
    for line in ["make: *** [all] Error 1",
                 "error: no such file or directory",
                 "Traceback (most recent call last)",
                 "  File \"x.py\", line 3, in status"] {
        let f = ProbeFailure(args: ["status"], path: PATH, status: 1,
                             stderr: line, elapsed: 1)
        check(f.exception == nil,
              "\"\(line)\" is not read as an exception"
                  + " (got \(f.exception?.name ?? "nil"))")
        has(f.summary, "exit 1", "and the exit status is what is shown instead")
    }
}

// Where it was when it failed, which is the granularity a menu can afford and
// a tag never could. Both frames earn their row: urllib says what went wrong,
// the probe's own frame says which of the five things it gathers was being
// gathered -- Slack, here, which is the difference between "the network" and
// "the network, and the rest of the day is still readable".
func testTheBlameNamesTheRaiseAndTheProbesOwnFrame() {
    let f = ProbeFailure(args: ["status"], path: PATH, status: 1, stderr: """
        Traceback (most recent call last):
          File "/Users/x/.claude/bin/worktime-probe.py", line 3911, in status
            last, stamps = live_activity(day)
          File "/Users/x/.claude/bin/worktime-probe.py", line 639, in _slack_fetch
            d = json.loads(urllib.request.urlopen(req).read())
          File "/opt/homebrew/.../urllib/request.py", line 1324, in do_open
            raise URLError(err)
        urllib.error.URLError: <urlopen error [Errno 8] nodename not known>
        """, elapsed: 1.2)
    check(f.blame == ["raised in request.py:1324 do_open",
                      "reached from worktime-probe.py:639 _slack_fetch"],
          "both frames are named (got \(f.blame))")
    // The absolute path is what a raw frame spends a whole row on, and the
    // line number is what falls off the end of it.
    check(!f.blame.joined().contains("/opt/homebrew"),
          "frames are named by file, not by path")
}

// A traceback that never leaves the probe has one frame worth showing, not the
// same one twice.
func testOneFrameWhenTheProbeRaisedItItself() {
    let f = ProbeFailure(args: ["status"], path: PATH, status: 1, stderr: """
        Traceback (most recent call last):
          File "/Users/x/.claude/bin/worktime-probe.py", line 3911, in status
            return day["periods"]
        KeyError: 'periods'
        """, elapsed: 0.4)
    check(f.blame == ["raised in worktime-probe.py:3911 status"],
          "the one frame is named once (got \(f.blame))")
}

// A stall has no frames and must not pretend to: the probe was killed before
// it got far enough to have a position to report.
func testAStallClaimsNoFrames() {
    check(stalled().blame.isEmpty, "a stall names no frame")
    check(stalled().lines.count == 2, "a stall is two rows, not padded out")
}

// The message is the sentence the block exists to deliver, and the errno lives
// at the end of it -- so it wraps onto a second row rather than being clipped.
func testALongExceptionMessageWraps() {
    let f = ProbeFailure(args: ["status"], path: PATH, status: 1,
                         stderr: "urllib.error.URLError: <urlopen error [Errno 8]"
                             + " nodename nor servname provided, or not known>",
                         elapsed: 1.2)
    // The last rows are the message, and putting them back together has to
    // give the message back whole -- broken on a space, nothing dropped.
    let said = f.lines.suffix(2).joined(separator: " ")
    check(said == "URLError: <urlopen error [Errno 8] nodename nor servname"
              + " provided, or not known>",
          "the message wraps whole across rows (got \(f.lines.suffix(2)))")
    // The path row is the one exception: it is middle-truncated by the label
    // itself, which keeps both ends of a path rather than the first half.
    check(f.lines.dropFirst().allSatisfy { $0.count <= DEBUG_ROW_CHARS },
          "a row outruns the menu (got \(f.lines.map(\.count)))")
}

func testStderrTailIsTheLastThreeLines() {
    let f = ProbeFailure(args: ["status"], path: PATH, status: 1,
                         stderr: "a\n\nb\nc\n\nd\n", elapsed: 1)
    check(f.stderrTail == ["b", "c", "d"],
          "the tail is the last three non-empty lines (got \(f.stderrTail))")
}

func testAnExitWithNothingToSaySaysSo() {
    let f = ProbeFailure(args: ["status"], path: PATH, status: 2,
                         stderr: "   \n", elapsed: 0.2)
    has(f.summary, "no output", "an empty stderr is reported as such")
}

func testLaunchFailureNamesItself() {
    let f = ProbeFailure(args: ["status"], path: PATH,
                         launchError: "The file “worktime-probe.py” doesn’t exist.")
    has(f.summary, "would not launch", "the summary says it never ran")
    has(f.lines.joined(separator: "\n"), "doesn’t exist",
        "the detail carries the launch error")
    // Nothing about the process is knowable, so nothing must be claimed: an
    // "exit 0 after 0.0s" row here would read as a probe that ran fine.
    check(!f.lines.joined(separator: "\n").contains("exit"),
          "a launch failure claims no exit status")
}

// Python 3.13 colourises tracebacks down a pipe, so this is what the real
// thing looks like arriving -- not a hypothetical. An NSTextField renders the
// escapes literally, and the line that names the exception came out as
// "[1;35mKeyError[0m: [35m'periods'[0m" the first time the app was pointed
// at a probe that raises.
func testTerminalColorCodesAreStripped() {
    let colored = "\u{1B}[1;35mKeyError\u{1B}[0m: \u{1B}[35m'periods'\u{1B}[0m"
    let f = ProbeFailure(args: ["status"], path: PATH, status: 1,
                         stderr: colored, elapsed: 0.4)
    check(f.stderrTail == ["KeyError: 'periods'"],
          "escapes are stripped from the rows (got \(f.stderrTail))")
    check(!f.summary.contains("\u{1B}"), "escapes are stripped from the summary")
    check(!f.report.contains("\u{1B}"), "escapes are stripped from the clipboard")
    has(f.summary, "KeyError: 'periods'", "the exception survives the stripping")
}

// The menu shows three lines of stderr; a traceback is longer than that. The
// clipboard is where the rest has to be, or the row is a tease.
func testReportCarriesTheWholeStderr() {
    let r = crashed().report
    has(r, "line 1402", "the report keeps the frames the menu clipped")
    has(r, "KeyError: 'periods'", "the report keeps the exception")
    has(r, PATH, "the report names the command")
    has(r, "0.4s", "the report says how long it ran")
}

func testReportSaysWhenStderrWasEmpty() {
    let r = stalled().report
    has(r, "(empty)", "an empty stderr is named rather than left blank")
    has(r, "killed at the watchdog", "the report says the kill was ours")
}

// A clipped line that looks like a short line is worse than a long one: it
// invites reading a truncated path as the path.
func testClipMarksWhatItCut() {
    let long = String(repeating: "x", count: 90)
    check(clip(long, 60).count == 60, "a clipped line is exactly the cap")
    check(clip(long, 60).hasSuffix("…"), "a clipped line says it was clipped")
    check(clip("short", 60) == "short", "a short line is left alone")
}

// Top-level code only compiles in a file called main.swift, and this one is
// named after what it tests; same shape as the other Swift suites here.

// What a stall costs the dot, as opposed to what it says.
//
// A probe killed at the watchdog never got far enough to disagree with the
// last reading, so the reading stands and the timer stops relaunching into a
// machine that just proved it cannot finish one. Both halves are pure
// functions on purpose: the state they describe takes a memory-starved Mac to
// reproduce, and this is the only place it can be asserted on demand.

func testAStallKeepsTheReadingAlreadyOnScreen() {
    let good = Date().addingTimeInterval(-10)
    check(StallPolicy.holdsLastReading(stalled(), lastGood: good),
          "a fresh reading survives a stall")
}

func testAStaleReadingIsNotWorthKeeping() {
    let good = Date().addingTimeInterval(-(STALE_AFTER_SEC + 1))
    check(!StallPolicy.holdsLastReading(stalled(), lastGood: good),
          "a reading past STALE_AFTER_SEC goes red")
}

func testACrashGoesRedImmediately() {
    // A traceback IS a disagreement with the last reading -- something is
    // wrong with the tracker, not with the machine's spare memory.
    let good = Date()
    check(!StallPolicy.holdsLastReading(crashed(), lastGood: good),
          "a traceback is never held")
}

func testAProbeThatHasNeverAnsweredIsBroken() {
    check(!StallPolicy.holdsLastReading(stalled(), lastGood: nil),
          "no good reading yet means nothing to hold")
}

func testALaunchFailureIsNeverHeld() {
    let f = ProbeFailure(args: ["status"], path: PATH, killed: true,
                         launchError: "No such file or directory")
    check(!StallPolicy.holdsLastReading(f, lastGood: Date()),
          "a probe that will not launch is broken however it ended")
}

func testTheHoldEndsExactlyAtTheStaleMark() {
    let good = Date().addingTimeInterval(-STALE_AFTER_SEC)
    check(!StallPolicy.holdsLastReading(stalled(), lastGood: good),
          "the boundary is not held")
}

func testAnAnsweringPollPaysNoBackoff() {
    check(StallPolicy.backoff(consecutiveKills: 0) == 0,
          "no backoff while the polls are answering")
}

func testBackoffGrowsWithConsecutiveKills() {
    let first = StallPolicy.backoff(consecutiveKills: 1)
    let second = StallPolicy.backoff(consecutiveKills: 2)
    check(first > 0 && second > first,
          "a machine still stalling is asked less often, not more")
}

func testBackoffStopsGrowing() {
    // Otherwise a bad afternoon ends with the dot updating once an hour.
    let capped = StallPolicy.backoff(consecutiveKills: 99)
    check(capped == PROBE_BACKOFF_SEC.last!,
          "the wait is capped at the last step")
}

func testBackoffIsLongerThanAPoll() {
    // The whole point: five seconds is what was making the pile-up.
    check(StallPolicy.backoff(consecutiveKills: 1) > 5.0,
          "the first backoff is longer than the poll interval")
}

@main
enum ProbeFailureTests {
    static func main() {
        testStallAndCrashReadDifferently()
        testStalledSaysItWasKilled()
        testUnkilledSignalIsNotCalledAStall()
        testCrashedCarriesTheException()
        testTheBlameNamesTheRaiseAndTheProbesOwnFrame()
        testOneFrameWhenTheProbeRaisedItItself()
        testAStallClaimsNoFrames()
        testALongExceptionMessageWraps()
        testTheExceptionIsNamedWithoutItsModule()
        testAnExceptionWithNoMessageIsStillNamed()
        testProseIsNotMistakenForAnException()
        testCaretUnderlinesAreNotRows()
        testStderrTailIsTheLastThreeLines()
        testAnExitWithNothingToSaySaysSo()
        testLaunchFailureNamesItself()
        testReportCarriesTheWholeStderr()
        testReportSaysWhenStderrWasEmpty()
        testTerminalColorCodesAreStripped()
        testClipMarksWhatItCut()

        testAStallKeepsTheReadingAlreadyOnScreen()
        testAStaleReadingIsNotWorthKeeping()
        testACrashGoesRedImmediately()
        testAProbeThatHasNeverAnsweredIsBroken()
        testALaunchFailureIsNeverHeld()
        testTheHoldEndsExactlyAtTheStaleMark()
        testAnAnsweringPollPaysNoBackoff()
        testBackoffGrowsWithConsecutiveKills()
        testBackoffStopsGrowing()
        testBackoffIsLongerThanAPoll()

        print(failures == 0 ? "all probe failure checks passed"
                            : "\(failures) probe failure check(s) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
