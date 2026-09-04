import Foundation

// What SingleFlight has to hold, driven rather than read. The bug it exists
// for is not a wrong value on screen -- it is fifteen subprocesses, which no
// assertion about the menu would have caught.

var failures = 0

func check(_ ok: Bool, _ what: String) {
    if ok {
        print("ok   \(what)")
    } else {
        print("FAIL \(what)")
        failures += 1
    }
}

func eq(_ got: Int, _ want: Int, _ what: String) {
    check(got == want, "\(what) (got \(got), want \(want))")
}

// A run that never finishes on its own, so the test decides when it does.
final class Job {
    var starts = 0
    private(set) var flight: SingleFlight!

    init() {
        flight = SingleFlight { [weak self] in self?.starts += 1 }
    }

    func request() { flight.request() }
    func finish() { flight.finish() }
}

// The first ask runs. Nothing subtle, but it is the half that must keep
// working: a guard that never starts anything is a dot that never updates.
func testFirstRequestStarts() {
    let j = Job()
    j.request()
    eq(j.starts, 1, "the first request starts a run")
    check(j.flight.isRunning, "a started run is in flight")
}

// The actual regression. Twelve polls arriving while one probe is still out
// used to be twelve subprocesses; it must now be the one that is already
// running, and nothing else, until it reports back.
func testRequestsWhileRunningDoNotStart() {
    let j = Job()
    j.request()
    for _ in 0 ..< 12 { j.request() }
    eq(j.starts, 1, "twelve polls during one run start nothing more")
    check(j.flight.hasQueued, "they are remembered, not simply dropped")
}

// And they collapse to ONE. The remembered request is not a counter: what the
// twelve had in common was the question, and one answer settles it for all of
// them.
func testTheRememberedRunIsSingular() {
    let j = Job()
    j.request()
    for _ in 0 ..< 12 { j.request() }
    j.finish()
    eq(j.starts, 2, "twelve remembered polls become one run")
    check(j.flight.isRunning, "and that run is now the one in flight")
    j.finish()
    eq(j.starts, 2, "with nothing left behind it")
    check(!j.flight.isRunning, "and nothing in flight")
}

// The half that keeps the menu honest. Dropping requests is only safe because
// a later one still gets read: whatever changed after the outstanding run
// began is picked up by the remembered run, so the reading on screen is never
// older than the last thing that asked for it.
func testAskingAfterAFinishStartsAgain() {
    let j = Job()
    j.request()
    j.finish()
    eq(j.starts, 1, "a finished run leaves nothing pending")
    j.request()
    eq(j.starts, 2, "and the next ask runs immediately")
}

// finish() is what the failure path calls too -- a probe that could not answer
// still has to release the slot, or the dot freezes on the last good reading
// forever. A spare finish() must not start a phantom run.
func testFinishWithNothingInFlightIsInert() {
    let j = Job()
    j.finish()
    eq(j.starts, 0, "finishing what was never started runs nothing")
    j.request()
    j.finish()
    j.finish()
    eq(j.starts, 1, "and a doubled finish does not double the run")
    check(!j.flight.isRunning, "nor leave a run in flight")
}

// The steady state this was built for: a poll every tick against a probe that
// takes longer than the tick. Whatever the ratio, the number of runs is
// governed by how fast they finish, never by how fast they are asked for.
func testASlowRunUnderAFastPollNeverPilesUp() {
    let j = Job()
    var finished = 0
    // 100 polls, one finish every tenth. Unguarded this is 100 runs.
    for i in 0 ..< 100 {
        j.request()
        if i % 10 == 9 {
            j.finish()
            finished += 1
        }
    }
    check(j.starts <= finished + 1,
          "100 polls with \(finished) completions started \(j.starts) runs, "
              + "which is at most one more than finished")
}

// Top-level code only compiles in a file called main.swift, and this one is
// named after what it tests; same shape as the other Swift suites here.
@main
enum SingleFlightTests {
    static func main() {
        testFirstRequestStarts()
        testRequestsWhileRunningDoNotStart()
        testTheRememberedRunIsSingular()
        testAskingAfterAFinishStartsAgain()
        testFinishWithNothingInFlightIsInert()
        testASlowRunUnderAFastPollNeverPilesUp()

        print(failures == 0 ? "all single flight checks passed"
                            : "\(failures) single flight check(s) failed")
        exit(failures == 0 ? 0 : 1)
    }
}
