#!/usr/bin/env python3
"""Record that a prompt just happened, in one file the probe can stat.

WHY THIS EXISTS. The probe polls every 5 seconds and has to answer one
question before it can reuse a cached day: has anything changed? It used to
answer by walking every Claude Code transcript on the machine -- 3,139 files
and directories across all profiles -- and hashing their mtimes. That is
~40ms when the filesystem metadata is in cache, which is why it was written
that way and why it was fine for a year.

It stops being fine when the machine is under memory pressure. With swap at
15.1GB of 16.4GB the metadata cache is evicted between polls, so every poll
re-reads all 3,139 entries from disk in uninterruptible wait, and a poll that
should take 40ms takes upwards of 25 seconds -- past the menu bar's 30s
watchdog, which SIGTERMs it and shows a red dot. Two thirds of those files had
not been touched in over a week; only 77 of them had changed that day. The
freshness check had become more expensive than the recomputation it existed to
avoid.

The fix is to stop asking the filesystem and let the writer say so. Claude Code
already fires a hook on every prompt; this is that hook. It overwrites one
small file, so the probe's question becomes a single stat of a single path --
3,139 metadata reads down to 1, and exact rather than inferred: there is no
scan interval to miss a prompt inside, and no way for a resumed old session to
slip past the way it would if we pruned the walk by directory mtime (appending
to an existing transcript does not change its directory's mtime).

WHY PROMPTS AND NOT WRITES. The old fingerprint changed on every transcript
write, so an active session forced a recomputation about six times a minute.
Only user prompts are evidence of presence -- an assistant streaming a reply
proves nothing about whether anyone is at the desk -- so marking prompts is
both cheaper and closer to what the tracker actually measures.

EVERY PROFILE MUST INSTALL IT. This machine runs several Claude Code profiles
(~/.claude interactively, ~/.claude-personal for worktime's own background
jobs, ~/.claude-bench). They write transcripts to separate roots but share this
one mark file, which is what keeps the probe at a single stat. A profile
without the hook stops contributing presence silently -- exactly the failure
the walk could not have -- so the probe refuses to run rather than undercount,
and tests/test_prompt_mark.py pins the registration.

FAILURES ARE SWALLOWED, deliberately and narrowly: this runs on the path
between the human pressing return and Claude answering, and instrumentation
must never be able to block that. The loud half of the contract lives in the
probe, which raises when the mark is absent.
"""

import json
import os
import sys
import tempfile
from datetime import datetime

# realpath, not abspath: this file is reached through the
# ~/.claude/hooks/worktime-prompt-mark.py symlink, and abspath would look for
# the shared module in ~/.claude/hooks, where it is not.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktime_common as wc  # noqa: E402

STATE = os.path.expanduser("~/.claude/stats/worktime")
MARK = os.path.join(STATE, "prompt-mark.json")

# Which transcripts a day's prompts landed in, so bin/prompt-count.py can open
# those few instead of walking every transcript on the machine to find them.
# An index of paths, deliberately NOT a copy of the prompts: the counter's
# filters (entrypoint, isMeta, isSidechain, the running session title, the
# fork/rewind dedup) are subtle and worth exactly one implementation, and the
# UserPromptSubmit payload does not carry `entrypoint` to reproduce them with.
# Recording where to look keeps every one of those decisions where it is.
INDEX_DIR = os.path.join(STATE, "prompt-index")
# The day the index went live. Days before it -- and the install day itself,
# whose earlier prompts happened before the hook existed -- are incomplete, so
# the counter walks those rather than trusting a partial answer.
SINCE = os.path.join(INDEX_DIR, "since")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    try:
        os.makedirs(STATE, exist_ok=True)
        now = datetime.now(wc.local_tz())
        # Overwritten, never appended: the probe reads mtime and size, and an
        # append-only log would grow without bound for a signal whose entire
        # content is "the most recent one".
        body = json.dumps({
            "at": now.isoformat(timespec="seconds"),
            "day": now.strftime("%Y-%m-%d"),
            "session": (payload.get("session_id") or "")[:8],
            # Which profile fired, so a mark that stops moving can be traced to
            # the profile whose hook fell off rather than guessed at.
            "profile": os.path.basename(
                os.path.dirname(payload.get("transcript_path") or "")) or "?",
        })
        # Atomic: several profiles write this same path, and the probe must
        # never stat a half-written file.
        fd, tmp = tempfile.mkstemp(dir=STATE, prefix=".prompt-mark-")
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.replace(tmp, MARK)

        transcript = payload.get("transcript_path")
        if transcript:
            os.makedirs(INDEX_DIR, exist_ok=True)
            if not os.path.exists(SINCE):
                with open(SINCE, "w") as fh:
                    fh.write(now.strftime("%Y-%m-%d"))
            # Appended, not overwritten: a day has many sessions and the
            # counter needs all of them. Duplicates are expected -- one line
            # per prompt, not per session -- and the reader dedups, which is
            # cheaper than reading the file back here on every prompt.
            with open(os.path.join(
                    INDEX_DIR, now.strftime("%Y-%m-%d") + ".jsonl"), "a") as fh:
                fh.write(json.dumps({"transcript": transcript,
                                     "session": payload.get("session_id") or ""})
                         + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
