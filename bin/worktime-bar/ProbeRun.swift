import Foundation

// What a failed probe run knows about itself, and how that reads on screen.
//
// The dot has exactly one red state and it used to say one thing: "probe did
// not answer". That sentence is true of every way a poll can fail and tells
// them apart not at all -- and the two ways that actually happen want opposite
// responses. A probe killed at the watchdog with no output is stalled in a
// syscall, and the thing to look at is what it was trying to read; a probe that
// exits non-zero has a traceback, and the thing to look at is the traceback.
// Both were already being written to the bar's stderr, which is a file nobody
// reads until an afternoon has gone red -- so the run's own account of itself
// comes back up into the menu instead.
//
// Separate from main.swift and free of AppKit so the wording can be driven by
// a test: the strings are the whole feature, and every one of them describes a
// state that is a nuisance to reproduce on demand.

// The wall against a probe that never returns, for the same reason
// CHROME_TAB_TIMEOUT_SEC exists: on a serial queue one wedged child is every
// later probe blocked behind it, including the one a click is waiting on.
// Generous rather than tight -- a genuinely cold run that has to re-read every
// transcript on disk takes several seconds and is not a fault.
//
// Here rather than beside runProbe because the failure text names it: a kill at
// this number is the difference between "stalled" and "crashed", and the two
// must not be able to disagree about what the number is.
let PROBE_TIMEOUT_SEC = 30.0

struct ProbeFailure {
    /// The probe's own arguments, without the interpreter or script path.
    let args: [String]
    /// Absolute path of the script that was launched -- part of the diagnosis
    /// rather than decoration: the deployed probe is a symlink into a folder
    /// macOS guards, and which path this is was the answer the one time this
    /// mattered most.
    let path: String
    /// Process exit status. 15 is SIGTERM, which is the watchdog's own kill.
    let status: Int32
    let stderr: String
    let elapsed: Double
    /// True when the watchdog fired, rather than inferred from status 15 --
    /// a probe can be signalled by something other than this app.
    let killed: Bool
    /// Set only when the process never started, in which case nothing else
    /// here is meaningful.
    let launchError: String?

    init(args: [String], path: String, status: Int32 = 0, stderr: String = "",
         elapsed: Double = 0, killed: Bool = false, launchError: String? = nil) {
        self.args = args
        self.path = path
        self.status = status
        self.stderr = stderr
        self.elapsed = elapsed
        self.killed = killed
        self.launchError = launchError
    }

    /// The last few non-empty lines of stderr, newest last. A Python traceback
    /// ends with the exception, so the tail is the part worth the space.
    var stderrTail: [String] {
        let lines = plainStderr.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
            // Python underlines the failing expression with a row of ~ and ^.
            // It only means anything directly beneath the line it points at, in
            // a monospaced block that starts at the same column -- and in three
            // rows lifted out of a traceback it is a row of punctuation where
            // a frame could have been.
            .filter { !$0.allSatisfy { "~^ ".contains($0) } }
        return Array(lines.suffix(3))
    }

    /// Short enough to sit beside the dot in the menu bar. The red dot is
    /// noticed away from the menu, and a dot alone cannot say whether the
    /// tracker is stalled or crashing.
    var tag: String {
        if launchError != nil { return "probe missing" }
        if killed { return "probe stalled" }
        return "probe exit \(status)"
    }

    /// One line, for the status row and the tooltip -- where "probe did not
    /// answer" used to be.
    var summary: String {
        if let e = launchError { return "probe would not launch: \(clip(e, 60))" }
        if killed {
            return "probe killed after \(secs) — no answer"
        }
        if let last = stderrTail.last {
            return "probe exit \(status): \(clip(last, 60))"
        }
        return "probe exit \(status), no output"
    }

    /// The detail rows, in the order they earn their height. First what was
    /// run, then how it ended, then whatever it managed to say.
    var lines: [String] {
        if let e = launchError {
            return ["\(path) \(args.joined(separator: " "))", e]
        }
        var out = ["\(path) \(args.joined(separator: " "))"]
        if killed {
            // Named as a stall rather than a crash, because the distinction is
            // the whole point of reading this: no output before the kill means
            // the probe never got far enough to fail, so what to look at is
            // what it was blocked on and not what it computed.
            out.append("killed at the \(Int(PROBE_TIMEOUT_SEC))s watchdog"
                       + " — stalled, no traceback")
        } else {
            out.append("exit \(status) after \(secs)")
        }
        out.append(contentsOf: stderrTail.map { clip($0, 70) })
        return out
    }

    /// Everything, as text, for the clipboard: the menu can only afford three
    /// lines of stderr and a traceback is usually longer than that.
    var report: String {
        var out = ["worktime probe failure",
                   "command: \(path) \(args.joined(separator: " "))"]
        if let e = launchError {
            out.append("launch failed: \(e)")
        } else {
            out.append("exit: \(status)\(killed ? " (killed at the watchdog)" : "")")
            out.append("elapsed: \(secs)")
            out.append("stderr:")
            out.append(plainStderr.isEmpty ? "  (empty)" : plainStderr)
        }
        return out.joined(separator: "\n")
    }

    /// stderr with terminal colour codes taken out. Python 3.13 colourises a
    /// traceback whether or not anything on the other end of the pipe can
    /// render it, so the real thing arrives full of escape sequences -- which
    /// an NSTextField draws literally, turning the one line that names the
    /// exception into "[1;35mKeyError[0m: [35m'periods'[0m". Found by
    /// pointing the app at a probe that raises rather than by reading the code.
    private var plainStderr: String {
        stderr.replacingOccurrences(of: "\u{1B}\\[[0-9;]*[a-zA-Z]",
                                    with: "", options: .regularExpression)
    }

    private var secs: String { String(format: "%.1fs", elapsed) }
}

enum ProbeRun {
    case ok(String)
    case failed(ProbeFailure)
}

/// Truncates on a character count, with an ellipsis so a cut line cannot be
/// mistaken for a short one.
func clip(_ s: String, _ n: Int) -> String {
    s.count <= n ? s : String(s.prefix(n - 1)) + "…"
}
