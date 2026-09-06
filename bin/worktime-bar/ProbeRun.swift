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

/// How many characters of the 10pt monospaced face a debug row holds. Measured
/// against the row it is drawn in rather than guessed: at 44 the wrapped rows
/// came out middle-truncated on screen, which reads as a bug in the block
/// rather than as a long message.
let DEBUG_ROW_CHARS = 46

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

    /// The exception a Python traceback ends on, split into its class and its
    /// message. The last line of a traceback is exactly "pkg.mod.ClassName:
    /// message", or the class alone when the exception carries none.
    ///
    /// The class is what the dot should be naming. "exit 1" is the same two
    /// words for a Slack lookup that could not resolve a hostname and for a
    /// probe that indexed a missing key -- it says a poll failed, which the
    /// colour already said. "URLError" says where to start.
    var exception: (name: String, message: String)? {
        guard let last = stderrTail.last else { return nil }
        let parts = last.split(separator: ":", maxSplits: 1,
                               omittingEmptySubsequences: false)
        let head = String(parts[0])
        // A dotted identifier and nothing else, ending in a class name. The
        // uppercase requirement is what keeps ordinary prose out: "make: ***"
        // and "error: no such file" both parse as an identifier and a message,
        // and neither is an exception.
        guard head.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "_" || $0 == "." }),
              let name = head.split(separator: ".").last,
              let first = name.first, first.isUppercase
        else { return nil }
        let message = parts.count > 1
            ? String(parts[1]).trimmingCharacters(in: .whitespaces) : ""
        return (String(name), message)
    }

    /// One line, for the status row and the tooltip -- where "probe did not
    /// answer" used to be.
    var summary: String {
        if let e = launchError { return "probe would not launch: \(clip(e, 60))" }
        if killed {
            return "probe killed after \(secs) — no answer"
        }
        // The exception and nothing before it. The status row is one line
        // beside a red bullet, and "probe exit 1: " spends a third of it
        // saying what the bullet said -- while the exit code has a row of its
        // own directly underneath. Named without its module path, too:
        // "urllib.error." is thirteen more characters of where a class is
        // defined, paid for out of the message.
        if let e = exception {
            return e.message.isEmpty ? e.name : "\(e.name): \(e.message)"
        }
        if let last = stderrTail.last {
            return "probe exit \(status): \(clip(last, 60))"
        }
        return "probe exit \(status), no output"
    }

    /// Every frame of the traceback, as file, line and function. Python writes
    /// them one per pair of lines: `  File "/x/y.py", line 639, in fetch`.
    ///
    /// Parsed rather than shown raw because a raw frame is an absolute path
    /// that fills a menu row and leaves the line number off the end of it,
    /// while what is wanted out of it is three short things.
    var frames: [(file: String, line: String, function: String)] {
        let pattern = #"File "([^"]+)", line (\d+), in (\S+)"#
        let text = plainStderr
        let re = try? NSRegularExpression(pattern: pattern)
        let range = NSRange(text.startIndex..., in: text)
        return (re?.matches(in: text, range: range) ?? []).compactMap { m in
            guard let f = Range(m.range(at: 1), in: text),
                  let l = Range(m.range(at: 2), in: text),
                  let fn = Range(m.range(at: 3), in: text) else { return nil }
            return ((text[f] as Substring).split(separator: "/").last.map(String.init)
                        ?? String(text[f]),
                    String(text[l]), String(text[fn]))
        }
    }

    /// The frames worth a row of their own: the one that raised, and the last
    /// one inside the probe itself when the raise happened further down.
    ///
    /// Both, because they answer different questions. A URLError raised in
    /// urllib says what went wrong; the probe's own frame says which of the
    /// five things it gathers was being gathered at the time -- Slack, in the
    /// failure this was written for, which is the difference between "the
    /// network" and "the network, and the day is otherwise readable".
    var blame: [String] {
        let all = frames
        guard let raised = all.last else { return [] }
        let mine = String(path.split(separator: "/").last ?? "")
        var out = ["raised in \(raised.file):\(raised.line) \(raised.function)"]
        if let own = all.last(where: { $0.file == mine }), own.file != raised.file {
            out.append("reached from \(own.file):\(own.line) \(own.function)")
        }
        return out
    }

    /// The detail rows, in the order they earn their height. What was run, how
    /// it ended, where it was when it ended, and what it said.
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
            return out
        }
        out.append("exit \(status) after \(secs)")
        out.append(contentsOf: blame)
        // The exception on its own row and last, where the eye lands after the
        // frames that lead to it. Split rather than clipped when it is long:
        // this is the sentence the whole block exists to deliver, and losing
        // its end to an ellipsis is losing the errno.
        if let e = exception {
            let said = e.message.isEmpty ? e.name : "\(e.name): \(e.message)"
            out.append(contentsOf: wrap(said, DEBUG_ROW_CHARS, rows: 3))
        } else {
            out.append(contentsOf: stderrTail.map { clip($0, DEBUG_ROW_CHARS) })
        }
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

/// Breaks a line into at most `rows` rows of at most `width`, on spaces where
/// there are any. The last row is clipped if the text outruns the budget, so
/// the block cannot grow without limit on a long exception message.
func wrap(_ s: String, _ width: Int, rows: Int) -> [String] {
    var out: [String] = []
    var rest = Substring(s)
    while !rest.isEmpty {
        // The last row carries whatever is left, clipped: an ellipsis at the
        // end of the block is honest, an ellipsis with rows still to spare is
        // just a narrow column.
        if out.count == rows - 1 { out.append(clip(String(rest), width)); break }
        if rest.count <= width { out.append(String(rest)); break }
        let limit = rest.index(rest.startIndex, offsetBy: width)
        let cut = rest[..<limit].lastIndex(of: " ") ?? limit
        out.append(String(rest[..<cut]))
        rest = rest[cut...].drop(while: { $0 == " " })
    }
    return out
}

/// Truncates on a character count, with an ellipsis so a cut line cannot be
/// mistaken for a short one.
func clip(_ s: String, _ n: Int) -> String {
    s.count <= n ? s : String(s.prefix(n - 1)) + "…"
}
