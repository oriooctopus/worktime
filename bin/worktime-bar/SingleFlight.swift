import Foundation

/// One outstanding run of a job, and at most one remembered request for
/// another.
///
/// The job here is the status probe, and what it is guarding against is a
/// pile. The probe recomputes the day from every transcript on disk, so a run
/// costs real time -- usually well under a second, several seconds when the
/// caches are cold -- while the poll that asks for one is on a fixed 5s grid
/// and the menu asks for another on every open. Nothing used to connect the
/// two: each request launched its own subprocess regardless of what was
/// already out. On 2026-09-04 this app was found with fifteen `probe status`
/// children alive at once, the oldest 65 seconds old. Past a certain depth
/// each probe is slow BECAUSE of the others, so the pile does not drain: the
/// menu goes minutes stale, and a clicked row -- which needs the queue too --
/// waits behind all of it. That is what "the menu is slow to open and clicking
/// an option does nothing" was.
///
/// Requests are dropped rather than queued, because a queued one asks the same
/// question the outstanding one is already answering. Where they differ is
/// only that the queued one is later, and "later" is exactly what the single
/// remembered request preserves: whatever changed after the outstanding run
/// began still gets read, once, by one more run. A hundred waiting copies of
/// "what is the state now" are not a hundred readings.
///
/// Not thread-safe by design, and it does not need to be: every caller in the
/// app is on the main thread. Making it a lock would only hide a call from
/// somewhere that has no business asking.
final class SingleFlight {
    private var inFlight = false
    private var queued = false
    private let work: () -> Void

    init(work: @escaping () -> Void) {
        self.work = work
    }

    /// True while a run is out.
    var isRunning: Bool { inFlight }

    /// True when a run finished while another was already out, and is waiting
    /// for it to report done.
    var hasQueued: Bool { queued }

    /// Ask for a run. Starts one if nothing is out; otherwise remembers that
    /// one more is wanted and returns.
    func request() {
        guard !inFlight else {
            queued = true
            return
        }
        inFlight = true
        work()
    }

    /// The outstanding run has finished. Starts the remembered one, if there
    /// is one.
    ///
    /// Called by the job itself rather than wrapped around it, because the job
    /// is asynchronous: it hands off to a background queue and comes back much
    /// later, and a `defer` around `work()` would release the slot the instant
    /// the subprocess was launched -- which is the un-guarded behaviour with
    /// extra steps.
    func finish() {
        // Tolerated rather than asserted: finish() is what a failed run calls
        // too, and both the success and the failure path of a run that was
        // already cancelled would otherwise take the app down over a slot that
        // is already free.
        guard inFlight else { return }
        inFlight = false
        guard queued else { return }
        queued = false
        request()
    }
}
