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
    check(stalled().tag != crashed().tag, "a stall and a crash carry different tags")
    check(stalled().summary != crashed().summary,
          "a stall and a crash carry different summaries")
}

func testStalledSaysItWasKilled() {
    let f = stalled()
    has(f.tag, "stalled", "a killed probe is tagged as stalled")
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
    check(!f.tag.contains("stalled"), "an unkilled exit 15 is not tagged a stall")
    has(f.tag, "15", "the tag still names the exit status")
    has(f.lines.joined(separator: "\n"), "3.1s",
        "the detail says how long the run lasted")
}

func testCrashedCarriesTheException() {
    let f = crashed()
    has(f.tag, "exit 1", "the tag names the exit status")
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
    has(f.tag, "missing", "a probe that never started is tagged missing")
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
@main
enum ProbeFailureTests {
    static func main() {
        testStallAndCrashReadDifferently()
        testStalledSaysItWasKilled()
        testUnkilledSignalIsNotCalledAStall()
        testCrashedCarriesTheException()
        testCaretUnderlinesAreNotRows()
        testStderrTailIsTheLastThreeLines()
        testAnExitWithNothingToSaySaysSo()
        testLaunchFailureNamesItself()
        testReportCarriesTheWholeStderr()
        testReportSaysWhenStderrWasEmpty()
        testTerminalColorCodesAreStripped()
        testClipMarksWhatItCut()

        print(failures == 0 ? "all probe failure checks passed"
                            : "\(failures) probe failure check(s) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
