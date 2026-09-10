#!/usr/bin/env python3
"""Reconstruct when the day was worked, from prompt timestamps and the calendar.

Runs unattended. It used to interrupt with a dialog whenever a gap closed,
asking what the absence had been; that is gone. The dialog was wrong often
enough about both the length of an absence and the moment it ended that
answering it cost more than the label was worth, and the explanation it
collected is recoverable after the fact from what was on screen at the time.
Corrections now arrive through `label`, or from context, at leisure.

Only prompt activity and the calendar are wired up. Slack is the remaining
signal that would resolve real ambiguity, so `sources` is a dict rather than a
bare list -- adding it should not reshape the record.

Usage:
  worktime-probe.py check          -- classify the window since the last check
  worktime-probe.py label <verdict> [note]  -- record a human correction
  worktime-probe.py report         -- summarize labels collected so far
  worktime-probe.py backfill [n]   -- rebuild the last n days of snapshots
  worktime-probe.py mode [focused|unfocused]  -- read or set the focus mode
  worktime-probe.py meeting_end    -- the meeting running now ended at this minute
  worktime-probe.py end_session [last]  -- end the day: break the period, close
                                          the mark, cut the meeting, at this
                                          minute or at the last entry
  worktime-probe.py note [text]    -- record work this probe cannot see
  worktime-probe.py track <n> [clip|split]  -- claim the last n minutes as
                                          worked, stopping at work already
                                          counted or stepping over it
"""

from __future__ import annotations  # 3.8 can parse the annotations

import functools
import hashlib
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:  # the Linux box runs 3.8; the Mac does not need this
    from backports.zoneinfo import ZoneInfo

# Ambient local time is not trustworthy here. The sandbox some callers run
# under denies /var/db/timezone/zoneinfo, and tzset() answers that denial by
# silently falling back to UTC -- so the same script reports 16:28 or 12:28
# depending only on how it was invoked, with no error either way. That skew is
# enough to file an evening bout on the wrong day and to make consecutive runs
# disagree about the present. ZoneInfo reads the bundled tzdata instead and is
# unaffected, so every timestamp is resolved through LOCAL explicitly and
# datetime.now() with no argument is never used.
# realpath, not abspath: this file is normally reached through the
# ~/.claude/bin/worktime-probe.py symlink, and abspath would look for the
# shared module in ~/.claude/bin, where it is not. Same trap ROOT documents
# below.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktime_common as wc  # noqa: E402

LOCAL = wc.local_tz()

# Run helpers under THIS interpreter, never a bare "python3" off PATH. The menu
# bar app is started by launchd, whose PATH is /usr/bin:/bin:/usr/sbin:/sbin,
# so "python3" there resolved to Apple's 3.9 -- which cannot import
# prompt-count.py at all (`str | None` in an annotation is a TypeError at def
# time). Every poll from the menu bar therefore saw zero prompts and reported a
# confident "idle" while the same command run from a shell said "working".
PYTHON = sys.executable

# realpath, not abspath. This file is usually reached through the
# ~/.claude/bin/worktime-probe.py symlink, and abspath does not follow symlinks
# -- it returned the symlink's own path, so ROOT became ~/.claude and every
# sibling script was looked for in ~/.claude/bin, where none of them are. The
# probe then failed on every poll with "can't open file
# ~/.claude/bin/prompt-count.py", and the menu bar dot sat on its launch
# placeholder, which is indistinguishable from a genuine idle reading.
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PROMPT_COUNT = os.path.join(ROOT, "bin", "prompt-count.py")


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOCAL)

STATE = os.path.expanduser("~/.claude/stats/worktime")
LABELS = os.path.join(STATE, "labels.jsonl")
CURSOR = os.path.join(STATE, "cursor.json")

PROMPT_ROOTS = wc.PROJECT_ROOTS

# More than this many minutes with no prompt and you were not working. Prompts
# closer together than this chain into one work period; anything further apart
# opens a gap. Fifteen minutes of prompting, a fifteen-minute lull, then ten
# more minutes of prompting is work, gap, work -- not one forty-minute stretch.
#
# A work period runs from its first prompt to its last. An earlier version
# credited each prompt with five whole minutes, on the theory that the response
# still had to be read; that inflated the day and made every gap render as
# (true silence - 5), so a nine-minute silence displayed as a four-minute gap
# and contradicted this very rule on the face of the dashboard.
GAP_AFTER = 5

# What a period gets on top of its last prompt, and the least it can measure.
# Sending a prompt is not an instantaneous act and a period that ends the
# microsecond the last one was sent measures zero, which is how ten of one
# day's thirty-five periods came to read "08:01-08:01 0m".
#
# So: last prompt + TAIL, floored at MIN_PERIOD. With the lead below, a lone
# focused prompt carries forty seconds of padding, above the thirty-second
# floor. Small enough that it cannot inflate a day the way a five-minute credit
# did -- a hundred lone prompts buy well under two hours, and a hundred lone
# prompts is not a real day.
TAIL_SEC = 20

# And what a period gets BEFORE its first prompt. A prompt is not typed
# instantly -- a long one is a minute of work that left its stamp only at the
# moment it was sent, so crediting from the stamp alone loses the writing.
#
# Clamped where it would eat into a gap: see the note in write_vault_snapshot.
# Without that clamp a 5m20s silence would render as a 4m gap, which is the
# exact contradiction the old five-minute grace period produced -- a dashboard
# showing gaps shorter than the gap threshold it claims to use.
#
# Twenty seconds, flat -- the same as the tail, and applied whether or not the
# prompt opens a conversation. A minute of credit for writing was generous
# enough that lone prompts carried real weight on their own; twenty seconds
# covers the act of sending without paying for time that may not have been
# spent here.
LEAD_SEC = 20

# The least a period can measure, in either mode. A bout whose padding gets
# clamped away -- by a tight silence on both sides, or by an unfocused ramp that
# has not started yet -- still stands for something that really happened, and
# publishing it as 0m loses it entirely.
#
# Thirty seconds: a glance is worth less than a minute. Durations and totals
# are published in seconds (len_sec, work_sec) because of it -- the minute-of-day
# boundaries alone floor a thirty-second span to one minute at both ends, and
# the day would lose it.
MIN_PERIOD_SEC = 30

# GAP_AFTER above is the focused rule: five minutes of silence ends a period,
# applied uniformly, on the assumption that a prompt means hands on the keyboard
# and eyes on the answer.
#
# That assumption is wrong for the other way this machine gets used -- something
# long is running, or the hour is really about a meeting, and a prompt goes out
# every few minutes in between. Under the focused rule those prompts are four
# minutes apart, chain into one span, and an hour of half-attention publishes as
# an hour of work. Unfocused mode declines to assume and makes the session earn
# it: a bout opens with a one-minute cutoff and widens toward GAP_AFTER as it
# runs, reaching the full five after UNFOCUSED_RAMP_MIN of continuous work.
#
# The ramp is driven by the bout's own elapsed length, not by a count of
# prompts. Ten prompts inside a minute is one thought typed quickly, not ten
# minutes of concentration; counting them would let a burst buy the widest
# cutoff instantly, which is the inflation this mode exists to stop.
MODES = ("focused", "unfocused")
MODEFILE = os.path.join(STATE, "mode.jsonl")
MEETING_CUT = os.path.join(STATE, "meeting-cut.json")
SESSION_END = os.path.join(STATE, "session-end.json")
UNFOCUSED_GAP_START = 1
UNFOCUSED_RAMP_MIN = 10

# A window with prompts is working, full stop -- no need to ask. A window with
# none is the interesting case. Between those, ask.
#
# Deliberately much coarser than GAP_AFTER, and NOT the same question. Whether
# a gap existed is settled by GAP_AFTER; this only decides when a gap is worth
# interrupting a human over. Asking about every six-minute lull would make the
# tool unusable regardless of how the timeline is drawn.
QUIET_ASK_AFTER = 20      # min of silence before a quiet window is worth asking about
MARK_MAX_OPEN_MIN = 30    # an open manual mark expires after this, un-clicked
CONFIRM_WINDOW = 10       # min of post-ping activity that turns silence into a real label

# The day's window opens at the first work at or after this hour. Anything
# earlier belongs to the previous night, not to this morning.
DAY_ANCHOR = 5 * 60


def read_mode_log() -> list[tuple[datetime, str]]:
    if not os.path.exists(MODEFILE):
        return []
    rows = []
    for line in open(MODEFILE):
        if line.strip():
            r = json.loads(line)
            rows.append((datetime.fromisoformat(r["t"]), r["mode"]))
    rows.sort(key=lambda r: r[0])
    return rows


def mode_now() -> str:
    rows = read_mode_log()
    return rows[-1][1] if rows else "focused"


def set_mode(mode: str) -> str:
    if mode not in MODES:
        raise SystemExit(f"mode must be one of: {', '.join(MODES)}")
    os.makedirs(STATE, exist_ok=True)
    with open(MODEFILE, "a") as fh:
        fh.write(json.dumps({"t": now_local().isoformat(), "mode": mode}) + "\n")
    return mode


def mode_timeline(day: str) -> list[tuple[int, str]]:
    """(second-of-day, mode) transitions governing `day`, earliest first.

    A log rather than one stored current value, because the mode has to apply
    to when the work happened and not to when the question is asked. A single
    setting would re-derive the whole day under whatever is selected now: flip
    to unfocused at three in the afternoon and the morning's real, focused work
    retroactively shatters into thirty-second fragments. The log confines a
    change to what follows it.
    """
    carry = "focused"
    out = []
    for when, mode in read_mode_log():
        d = when.strftime("%Y-%m-%d")
        if d < day:
            carry = mode
        elif d == day:
            out.append((when.hour * 3600 + when.minute * 60 + when.second, mode))
    return [(0, carry)] + out


def mode_at(timeline: list[tuple[int, str]], sec: int) -> str:
    mode = timeline[0][1]
    for at, m in timeline:
        if at > sec:
            break
        mode = m
    return mode


def gap_sec_for(mode: str, elapsed_sec: float) -> int:
    """How long a bout this long is allowed to go quiet before it has ended.

    Constant in focused mode. In unfocused mode it opens at
    UNFOCUSED_GAP_START and grows linearly to GAP_AFTER across
    UNFOCUSED_RAMP_MIN of continuous bout.
    """
    if mode != "unfocused":
        return GAP_AFTER * 60
    ramp = min(1.0, max(0.0, elapsed_sec / (UNFOCUSED_RAMP_MIN * 60)))
    return int((UNFOCUSED_GAP_START + (GAP_AFTER - UNFOCUSED_GAP_START) * ramp) * 60)


def chain_bouts(stamps: list[int],
                timeline: list[tuple[int, str]]) -> tuple[list[list[int]], list[int]]:
    """Group sorted second-of-day stamps into bouts under the mode in force.

    Returns (bouts, split_at): each bout as [first, last], and alongside it the
    threshold in seconds that separated it from the bout before. Those
    thresholds are kept rather than recomputed because the padding below has to
    respect the very silence that split the pair -- in unfocused mode that is a
    different number at every boundary.
    """
    bouts: list[list[int]] = []
    split_at: list[int] = []
    for s in stamps:
        thr = gap_sec_for(mode_at(timeline, s),
                          bouts[-1][1] - bouts[-1][0] if bouts else 0)
        if bouts and s - bouts[-1][1] <= thr:
            bouts[-1][1] = s
        else:
            bouts.append([s, s])
            split_at.append(thr)
    return bouts, split_at


def build_bouts(stamps: list[int],
                timeline: list[tuple[int, str]]) -> list[list[int]]:
    """Sorted second-of-day stamps to padded work spans. Pure -- no I/O.

    Split out of write_vault_snapshot so the tests can exercise this arithmetic
    directly. They used to re-implement it, which held for as long as the two
    copies agreed and would have gone quietly stale the moment they did not.

    Chaining is decided on the RAW stamps, so the cutoff keeps meaning exactly
    what it says: a silence longer than it is a gap. The lead and tail are
    padding applied afterwards and must not be allowed to redefine it --
    widening the chain rule instead would quietly turn a six-minute silence
    into work.

    Padding (lead before the first prompt, tail after the last) comes out of a
    BUDGET rather than being a fixed amount, because the two rules it sits
    between can otherwise contradict each other:

      * a silence longer than the cutoff is a gap -- always, that is the rule
      * a gap must never be DISPLAYED shorter than the cutoff that made it

    A 5m10s silence has only 10 seconds to spare between those. Spending the
    full 40s of padding there would show a 4m gap; a hard floor on the start
    instead (the first attempt) shoved the period past its own prompt and
    published 0m periods. So each pair of adjacent bouts gets exactly the slack
    its silence affords, split tail-first, and neither rule bends.
    """
    present, split_at = chain_bouts(stamps, timeline)
    raw = [list(p) for p in present]
    lead_used = [0] * len(raw)
    tail_used = [TAIL_SEC] * len(raw)
    budgets = [0] * len(raw)
    for i in range(len(raw)):
        want = LEAD_SEC
        # In unfocused mode the lead is earned on the same ramp as the cutoff.
        # Credit before the first prompt is a fair reading of a bout that turned
        # into real work and a poor one for a prompt fired off between other
        # things -- and with the full lead always applied, a lone unfocused
        # prompt would still bank its padding, so the mode would hardly move the
        # day's total however short its cutoff got.
        if mode_at(timeline, raw[i][0]) == "unfocused":
            want = int(want * min(1.0, (raw[i][1] - raw[i][0])
                                  / (UNFOCUSED_RAMP_MIN * 60)))
        if i == 0:
            lead_used[i] = min(want, raw[i][0])
            continue
        # One second more than the threshold that split this pair, so the union
        # later -- which chains anything within that threshold -- cannot swallow
        # the gap it just preserved.
        budgets[i] = max(0, raw[i][0] - raw[i - 1][1] - (split_at[i] + 1))
        tail_used[i - 1] = min(TAIL_SEC, budgets[i])
        lead_used[i] = min(want, budgets[i] - tail_used[i - 1])

    # Whatever the pair did not spend on tail and lead is left over, and
    # MIN_PERIOD may use it to stretch a short bout forward. The last bout has
    # no following silence to protect, so it stretches freely -- otherwise a
    # lone prompt after a bare-minimum gap published as a 0m period, the exact
    # thing MIN_PERIOD exists to prevent.
    for i, p in enumerate(present):
        p[0] = raw[i][0] - lead_used[i]
        floor_end = raw[i][1] + tail_used[i]
        if i == len(raw) - 1:
            p[1] = max(floor_end, p[0] + MIN_PERIOD_SEC)
        else:
            spare = budgets[i + 1] - tail_used[i] - lead_used[i + 1]
            p[1] = max(floor_end, min(p[0] + MIN_PERIOD_SEC, floor_end + spare))

        # Growing forward is not always possible. A bout whose following
        # silence sits exactly on the cutoff has no room at all -- the gap may
        # not be shortened and so the period may not be lengthened -- and it
        # published as a zero-length span that vanished from the day. Unfocused
        # mode makes that the common case rather than a curiosity: with a
        # one-minute cutoff, any two prompts 61 seconds apart hit it.
        #
        # Behind the bout there is no such conflict. The room there is bounded
        # by the preceding silence, which the same budget already accounts for,
        # so spending it can neither overlap the period before nor shrink the
        # gap below its own cutoff.
        short = MIN_PERIOD_SEC - (p[1] - p[0])
        if short > 0:
            # The first bout has no predecessor, only the day anchor -- and a
            # period stretched back across it is dropped outright, which is the
            # very disappearance this is here to prevent.
            room = (max(0, p[0] - DAY_ANCHOR * 60) if i == 0
                    else budgets[i] - tail_used[i - 1] - lead_used[i])
            p[0] -= min(short, room)
    return present


def merge_spans(spans: list[list[int]],
                timeline: list[tuple[int, str]]) -> list[list[int]]:
    """Union overlapping or near-abutting presence spans, sorted by start.

    Spans are `[start, end]` for evidence -- the bouts chain_bouts already
    chained -- or `[start, end, True]` for DECLARED presence: a manual mark or
    a work meeting, which is somebody or something saying the time was worked
    without leaving an event behind.

    The two are joined by different rules, and that is the whole point:

      * Two bouts NEVER rejoin here. chain_bouts has already ruled on the
        silence between them under the cutoff in force at the moment it
        happened; re-asking the question later can only overturn that ruling,
        and it always overturned it the same way. The old threshold was
        measured over the accumulated merged run rather than over the bout
        that earned it, so it grew as it merged: a run past UNFOCUSED_RAMP_MIN
        pinned the cutoff at the full GAP_AFTER and then swallowed every later
        bout within five minutes of it. An unfocused afternoon whose dot went
        out four separate times published as one unbroken period, which is
        exactly the inflation unfocused mode exists to stop -- and it left the
        live dot (which chains the un-merged way, via live_cutoff) and the
        period list disagreeing about the same minute.

        Unfocused mode means the dot takes longer to go out as a run earns it.
        It does not mean the break is provisional. Once the dot goes out the
        period is over, and nothing downstream may quietly sew it back up.

      * Declared presence still joins what it abuts, under the threshold the
        run it is joining has earned. A mark or a meeting is an assertion
        about the silence around it, so it is allowed to bridge one -- that is
        what declaring it was for.

    Overlapping or abutting spans always merge, whichever kind they are:
    that is a union, not a chaining decision, and no cutoff is consulted.
    """
    merged: list[list[int]] = []
    # Whether the component that set the current end was declared. A run that
    # ends in a mark may bridge forward on the mark's authority; one that ends
    # in a bout may not.
    ends_declared: list[bool] = []
    for span in spans:
        s, e = span[0], span[1]
        declared = len(span) > 2 and bool(span[2])
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
                ends_declared[-1] = declared
            continue
        if merged and (declared or ends_declared[-1]):
            thr = gap_sec_for(mode_at(timeline, s),
                              merged[-1][1] - merged[-1][0])
            if s - merged[-1][1] <= thr:
                merged[-1][1] = max(merged[-1][1], e)
                ends_declared[-1] = declared
                continue
        merged.append([s, e])
        ends_declared.append(declared)
    return merged


def desktop_holes(stamps_sec: list[int], work_minutes: set[int],
                  timeline: list[tuple[int, str]]) -> list[list[int]]:
    """Intervals the other machine was in use, to cut out of Mac work periods.

    Each stamp spans the minute it names and no more. The export writes HH:MM,
    so "13:09" means "somewhere inside minute 13:09" -- the containing minute
    is what the data literally says, which is why this rule has no constant to
    tune. It must NOT reuse the Mac padding (LEAD_SEC, TAIL_SEC): those exist
    because a prompt under-represents the work around it, and every one of them
    errs toward crediting work. Applied to a stream that means the opposite
    they would err toward erasing it, and the two cannot share a budget.

    Work wins ties. A minute holding both a desktop prompt and a Mac event is
    kept, because the Mac sources are timestamped to the second while the
    export only names a minute, and the coarser reading should not overrule the
    finer one. 16% of desktop minutes collide this way; deleting them would
    throw away minutes there is direct evidence of working in.

    The chaining cutoff IS shared with the Mac stream -- it answers "did this
    stop or continue", which is the same question on either machine.
    """
    stamps = sorted({s for s in stamps_sec if s // 60 not in work_minutes})
    if not stamps:
        return []
    runs: list[list[int]] = []
    cur = [stamps[0], stamps[0] + 60]
    for s in stamps[1:]:
        if s - cur[1] <= gap_sec_for(mode_at(timeline, s), cur[1] - cur[0]):
            cur[1] = s + 60
        else:
            runs.append(cur)
            cur = [s, s + 60]
    runs.append(cur)
    return runs


def subtract_spans(spans: list[list[int]],
                   holes: list[list[int]]) -> list[list[int]]:
    """spans minus holes. A hole landing mid-span splits it in two.

    A remnant a hole left shorter than MIN_PERIOD_SEC is dropped: it is residue
    of a stretch already judged to be desktop time, not a period of its own.
    """
    out: list[list[int]] = []
    for s, e in spans:
        pieces = [[s, e]]
        for hs, he in holes:
            nxt: list[list[int]] = []
            for ps, pe in pieces:
                if he <= ps or hs >= pe:
                    nxt.append([ps, pe])
                    continue
                if hs > ps:
                    nxt.append([ps, min(hs, pe)])
                if he < pe:
                    nxt.append([max(he, ps), pe])
            pieces = nxt
        out += (pieces if pieces == [[s, e]]
                else [p for p in pieces if p[1] - p[0] >= MIN_PERIOD_SEC])
    return out


def live_cutoff(day: str, stamps_sec: list[int]) -> int:
    """The cutoff the run in progress has earned, for the live verdict.

    Uses mode_now() rather than the mode at the last stamp: switching to
    focused is a statement about what is happening at this moment, and the dot
    should answer to it immediately.
    """
    bouts, _ = chain_bouts(sorted(stamps_sec), mode_timeline(day))
    return gap_sec_for(mode_now(), bouts[-1][1] - bouts[-1][0] if bouts else 0)


@functools.lru_cache(maxsize=None)
def _prompts_for_cached(day: str) -> tuple:
    """One prompt-count subprocess per day, per probe run.

    Four call sites ask for the same day's prompts and a status poll reaches
    seven of them, each previously paying its own `subprocess.run` -- seven
    fresh interpreters where one answer was wanted. That is the probe's single
    largest cost (2.1s of a 4.3s run) and, more to the point, seven eighths of
    the interpreter startups it performs.

    Startup is what actually kills this process. A stalled poll sampled at
    11:17 was in state `U` -- uninterruptible disk wait -- inside dyld's
    `dlopen` and Python's import machinery, faulting extension modules and
    .pyc files back in one page at a time. With swap at 17.9GB of 19.4GB the
    pages a Python launch needs are evicted between polls, so each of the eight
    launches pays a cold start, and eight cold starts overrun the menu bar's
    30s watchdog: SIGTERM, exit 15, red dot. Collapsing seven of them to one
    cuts the exposure by the same factor.

    Caching is sound because the process is short-lived -- a poll answers in
    well under a second and exits -- so no transcript can gain a prompt between
    two calls within one run. A tuple is returned because lru_cache hands every
    caller the same object and a list would let one of them mutate the others'
    copy; `prompts_for` unpacks it back into a fresh list.
    """
    r = subprocess.run(
        [PYTHON, PROMPT_COUNT, "--day", day],
        capture_output=True, text=True,
    )
    # A crashed counter is NOT a day with no prompts. Treating the two alike is
    # what hid the launchd interpreter bug: the helper died on every poll, the
    # empty stdout read as "nothing happened today", and the tracker reported
    # idle with full confidence for hours.
    if r.returncode != 0:
        raise RuntimeError(f"prompt-count failed ({r.returncode}): {r.stderr.strip()}")
    out = r.stdout
    if not out.strip():
        return ()
    data = json.loads(out)
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    seen = []
    for s in data.get("sessions", []):
        # `times` is uncapped; `prompts` keeps only the first 20 so the note it
        # feeds stays small. Reading `prompts` here made every long session
        # look like it stopped at its 20th message, which manufactured gaps out
        # of the busiest afternoons.
        for ts in s.get("times", []):
            h, m, sec = ts.split(":")
            seen.append(base.replace(hour=int(h), minute=int(m), second=int(sec)))
    return tuple(sorted(seen))


def prompts_for(day: str) -> list[datetime]:
    """Merged, sorted prompt datetimes for one local calendar day."""
    return list(_prompts_for_cached(day))


# Slack is the other place the work happens. With Claude prompts as the only
# evidence, half an hour spent answering people in #ruby-dev rendered as a gap
# -- the single largest source of false gaps in the day.
#
# Messages SENT are the signal. Receiving one says nothing about whether anyone
# was at the keyboard, and reads are invisible to the API, so neither is usable.
SLACK_TOKEN_FILE = os.path.expanduser("~/.slack-mcp-token.json")
SLACK_USER = "oliver.ullman"
SLACK_DIR = os.path.join(STATE, "slack")
# Today's answer changes constantly; a finished day never changes again, so its
# cache is permanent and backfill costs one request per day, once, forever.
SLACK_TTL_SEC = 240
SLACK_TEXT_CHARS = 200
# The whole fetch, not one request. The menu bar kills a probe that has not
# answered in 30s and paints the dot red, so a search that walks ten pages at
# 30s each could spend five minutes earning that red -- while a perfectly good
# cached copy of the day sat on disk unread. Whatever is fetched by the
# deadline is abandoned in favour of the stale copy, which costs one refresh
# interval of missing sends and nothing else.
SLACK_FETCH_BUDGET_SEC = 12

# Slack's wire format wraps links and mentions in angle brackets:
# <https://x|label>, <https://x>, <@U123>, <#C123|name>. Escaped for HTML and
# left as-is they render as a wall of &lt;https://...&gt; that buries the words.
SLACK_LINK = re.compile(r"<([^<>|]*)(?:\|([^<>]*))?>")


def slack_plain(text: str) -> str:
    """Slack wire text as something readable, escaped for the dashboard.

    Escaped HERE rather than in the widget because the snapshot's other text
    (prompt previews, session labels) is already escaped at this layer -- one
    convention for the whole file means the dashboard never has to know which
    fields are safe.
    """
    def unwrap(m):
        target, label = m.group(1), m.group(2)
        if label:
            return label
        if target.startswith("@"):
            return "@mention"
        if target.startswith("#"):
            return "#channel"
        return target

    t = SLACK_LINK.sub(unwrap, text)
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .strip())


def slack_token() -> str | None:
    """The user token the Slack MCP server was configured with.

    Nested at an undocumented depth and the shape has moved before, so the
    file is walked for the first xox* string rather than indexed into.
    """
    def walk(o):
        if isinstance(o, str):
            return o if o.startswith("xox") else None
        if isinstance(o, dict):
            for v in o.values():
                if t := walk(v):
                    return t
        if isinstance(o, list):
            for v in o:
                if t := walk(v):
                    return t
        return None

    try:
        return walk(json.load(open(SLACK_TOKEN_FILE)))
    except (OSError, ValueError):
        return None


def _slack_fetch(day: str) -> list[dict]:
    tok = slack_token()
    if not tok:
        return []
    # before/after are EXCLUSIVE, so this pair is exactly `day`. Preferred over
    # `on:` because both operators resolve against the workspace's timezone
    # rather than this machine's; every match is re-filtered by local date
    # below, which is what actually decides membership.
    rows, page, pages = [], 1, 1
    deadline = time.monotonic() + SLACK_FETCH_BUDGET_SEC
    while page <= pages and page <= 10:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(
                f"slack search.messages over {SLACK_FETCH_BUDGET_SEC}s budget"
                f" at page {page}")
        q = urllib.parse.urlencode({
            "query": f"from:@{SLACK_USER} after:{shift_day(day, -1)}"
                     f" before:{shift_day(day, 1)}",
            "count": 100, "page": page, "sort": "timestamp",
        })
        req = urllib.request.Request(
            "https://slack.com/api/search.messages?" + q,
            headers={"Authorization": f"Bearer {tok}"})
        d = json.loads(urllib.request.urlopen(req, timeout=left).read())
        if not d.get("ok"):
            raise RuntimeError(f"slack search.messages failed: {d.get('error')}")
        msgs = d.get("messages", {}) or {}
        for m in msgs.get("matches", []):
            when = datetime.fromtimestamp(float(m["ts"]), LOCAL)
            if when.strftime("%Y-%m-%d") != day:
                continue
            ch = m.get("channel", {}) or {}
            rows.append({
                "t": when.strftime("%H:%M:%S"),
                # For a DM this is the other party's user ID, not a name -- the
                # search response carries no display name for them. `im` is what
                # the dashboard should key on, not a name-shaped ID.
                "ch": (ch.get("name") or "").replace("&", "&amp;")
                       .replace("<", "&lt;").replace(">", "&gt;"),
                "im": m.get("type") == "im",
                "text": slack_plain(m.get("text") or "")[:SLACK_TEXT_CHARS],
            })
        pages = (msgs.get("paging") or {}).get("pages") or 1
        page += 1
    return sorted(rows, key=lambda r: r["t"])


def slack_for(day: str) -> list[dict]:
    """Messages sent on one local day, cached on disk.

    The probe runs every few minutes and search.messages is rate-limited, so an
    uncached call per run would be both slow and rude to the API.
    """
    os.makedirs(SLACK_DIR, exist_ok=True)
    path = os.path.join(SLACK_DIR, f"{day}.json")
    fresh = os.path.exists(path) and (
        day != now_local().strftime("%Y-%m-%d")
        or time.time() - os.path.getmtime(path) < SLACK_TTL_SEC)
    if fresh:
        return json.load(open(path))
    try:
        rows = _slack_fetch(day)
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError,
            ValueError, http.client.HTTPException):
        # http.client.HTTPException is here for IncompleteRead: search.messages
        # hands back a truncated body often enough that leaving it out of this
        # tuple was, on its own, minutes of red dot -- the exception escaped
        # past the stale copy this handler exists to serve and killed the probe.
        #
        # A blip must not erase a day that was already fetched -- serving the
        # stale copy is strictly better than publishing a day with the Slack
        # evidence silently missing. With nothing cached there is nothing to
        # serve, so the failure propagates rather than inventing an empty day.
        if os.path.exists(path):
            return json.load(open(path))
        raise
    with open(path, "w") as fh:
        json.dump(rows, fh)
    return rows


def shift_day(day: str, n: int) -> str:
    d = datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)
    return d.strftime("%Y-%m-%d")

# The desktop client's own console log, and the line it writes the instant a
# message leaves the compose box. Reading it is the same move github_live_rows()
# makes against Chrome's history: the authoritative source is remote and slow,
# the app already keeps a local record, so the local one carries the live
# verdict and the remote one stays the record.
#
# What it buys is the whole of SLACK_TTL_SEC. search.messages is cached for
# four minutes because a network round trip cannot sit on a five-second poll,
# so a send could be four minutes old before anything here could see it -- and
# the live dot is exactly the consumer that cannot wait. Measured against a
# day of real sends the log line lands about a second BEFORE the API's own
# timestamp, since it is written at call time rather than at server receipt.
#
# It sees strictly less than the API, and every omission is one this wants:
# only messages typed in this Mac's client appear, so a send from the phone or
# from a script holding the same token -- both of which held the dot green
# without anybody at this desk -- leaves no line here.
#
# Undocumented and unversioned, so it is a freshness accelerator and never the
# record: if Slack renames the line tomorrow this goes quiet and slack_for()
# carries on unaffected.
SLACK_LOG_DIR = os.path.expanduser(
    "~/Library/Application Support/Slack/logs/default")
SLACK_LOG = os.path.join(SLACK_LOG_DIR, "webapp-console.log")

# `[09/02/26, 12:37:23:833] info: [API-Q] (T...) <id> chat.postMessage called
#  with reason: webapp_message_send`
SLACK_SEND_LINE = re.compile(
    r"^\[(\d\d)/(\d\d)/(\d\d), (\d\d):(\d\d):(\d\d):\d+\].*webapp_message_send")

# Where the last read of the live log stopped, so a poll costs the bytes Slack
# has written since rather than the whole file. The log grows continuously --
# RTM events land every few seconds whether or not anybody is typing -- so
# memoising on (mtime, size) the way github_live_rows() does would miss on
# nearly every poll and re-read megabytes each time.
_slack_log_state: dict = {}


def last_slack_send(day: str) -> datetime | None:
    """When a message was last sent from this Mac's Slack client, or None.

    Tails the console log rather than re-reading it: the offset and the answer
    so far are kept between calls, and a shrunken file means Slack rotated the
    log, which restarts the read from the top of the new one.
    """
    try:
        size = os.stat(SLACK_LOG).st_size
    except OSError:
        return None

    st = _slack_log_state
    if st.get("day") != day:
        st.clear()
        st["day"] = day
    # Rotation truncates the active log, so an offset past the end is stale
    # rather than merely behind.
    if size < st.get("offset", 0):
        st["offset"] = 0
        st["at"] = None
    if size == st.get("offset") and "at" in st:
        return st["at"]

    stamp = day[5:7] + "/" + day[8:10] + "/" + day[2:4]
    best = st.get("at")
    try:
        with open(SLACK_LOG, errors="replace") as fh:
            fh.seek(st.get("offset", 0))
            for line in fh:
                m = SLACK_SEND_LINE.match(line)
                if not m or f"{m.group(1)}/{m.group(2)}/{m.group(3)}" != stamp:
                    continue
                base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
                when = base + timedelta(hours=int(m.group(4)),
                                        minutes=int(m.group(5)),
                                        seconds=int(m.group(6)))
                if best is None or when > best:
                    best = when
            st["offset"] = fh.tell()
    except OSError:
        return best
    st["at"] = best
    return best



def sec_of(hms: str) -> int:
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


MARKS = os.path.join(STATE, "marks.jsonl")

# The note link_last_session() writes. A linked stretch is work nobody
# described -- the whole reason it needs claiming is that the tracker saw
# nothing in it -- so the note says where the minutes came from rather than
# what they were, and the period list has something to show besides a blank.
LINK_NOTE = "linked to the last session"


def mark_stamps(day: str) -> list[int]:
    """The real events a mark is resolved against, as minutes-of-day.

    Consults prompts and focus directly rather than the snapshot: events_for()
    reads marks back through the snapshot, and going that way round would be a
    cycle.
    """
    stamps = [t.hour * 60 + t.minute for t in prompts_for(day)]
    stamps += [t.hour * 60 + t.minute for t in focus_for(day)]
    return sorted(stamps)


def open_mark_end(r: dict, stamps: list[int], now_m: int, live: str) -> int:
    """Where a still-open mark reaches. The one answer, for the one question.

    Shared with close_open_marks, which has to write the very minute this
    returns: a mark that is only ever READ through here, then CLOSED at some
    later minute by a different rule, is a mark whose meaning changes when it
    is closed -- and that is not a hypothetical. A link left open at 10:08
    resolved here to 10:07 all morning, then End Session at 14:28 stamped
    14:28 onto it and four hours of breaks became one unbroken period.
    """
    start = r["start"]
    # The first real activity strictly after the mark closes it.
    after = [s for s in stamps if s > start]
    end = after[0] if after else (now_m if r.get("day") == live else start)
    # ...but never longer than MARK_MAX_OPEN_MIN. Without a ceiling an open
    # mark credits an entire absence: one clicked at 14:00 and forgotten ran
    # until 15:30, silently adding 90 minutes, because nothing typed in between
    # could close it. That failure hides itself -- the longer you are away, the
    # more work it invents -- which is precisely what this tracker exists to
    # catch. Expiring is the safe direction: re-clicking costs a second, and
    # the minutes after the cap are still recoverable from prompts and Slack.
    #
    # Measured from when the mark was MADE, not from where it starts. The two
    # are the same minute for a mark claiming time from the click forward, so
    # this changes nothing for the ordinary case -- but a mark can start in the
    # past, either from `mark HH:MM` or from a link, and there the distinction
    # decides whether it works at all. Capping a backdated mark from its start
    # spends the allowance on minutes that had already elapsed when it was
    # written: a 35-minute gap linked at 12:15 would reach only 12:10, falling
    # five minutes short of the moment it was asked to reach, and one backdated
    # an hour would expire before it was even made. The forgotten-mark this
    # ceiling defends against is forgotten from the click onwards, so that is
    # where the clock starts.
    made = datetime.fromisoformat(r["created"])
    return min(end, max(start, made.hour * 60 + made.minute)
               + MARK_MAX_OPEN_MIN)


def marks_for(day: str, stamps: list[int] | None = None) -> list[dict]:
    """Manually declared work, resolved into concrete spans in minutes.

    A mark is work that leaves no trace anywhere else -- reading a PR, thinking,
    a whiteboard. It is open-ended by design: declaring "I am working" should
    not also require predicting when you will stop. It runs until the next real
    event (a prompt, or an attended minute at the front of a work app) proves
    the tracker can see you again, and stops there rather than continuing to
    credit time the normal signals now cover. If nothing has happened since, it
    runs to now.

    Focus makes this close much sooner than it used to, and that is the point:
    a mark declared while sitting at the machine is superseded within the
    minute by evidence of sitting at the machine, and the minutes carry on
    being counted by focus_for() rather than by the human's word. What still
    runs long is a mark made while genuinely away -- a whiteboard, an offsite,
    a phone call -- which is the case marks exist for.

    An explicit end is honoured when given, which is how a stretch is recorded
    after the fact.
    """
    if not os.path.exists(MARKS):
        return []
    live = now_local().strftime("%Y-%m-%d")
    now_m = now_local().hour * 60 + now_local().minute
    if stamps is None:
        stamps = mark_stamps(day)

    out = []
    for line in open(MARKS):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("day") != day:
            continue
        start = r["start"]
        if r.get("end") is not None:
            end = r["end"]
        else:
            end = open_mark_end(r, stamps, now_m, live)
        is_open = r.get("end") is None
        # An open mark counts from the moment it is made, including the minute
        # it was made in. Requiring end > start hid every fresh mark until the
        # clock ticked over: click at 14:00:05 and start == end == 840, the
        # mark was dropped, and the dot stayed idle for up to a minute -- which
        # reads as the button having done nothing at all.
        #
        # A CLOSED zero-length span is a different thing and still dropped: it
        # is a mark that was undone in the same minute it was made, and it
        # should contribute neither time nor a live state.
        if end > start or (is_open and end == start):
            out.append({"start": start, "end": end,
                        "note": r.get("note", ""), "open": is_open})
    return sorted(out, key=lambda m: m["start"])


def add_mark(spec: str, note: str) -> dict:
    """Record a manual mark. `spec` is "", "HH:MM", or "HH:MM-HH:MM"."""
    now = now_local()
    day = now.strftime("%Y-%m-%d")
    start, end = now.hour * 60 + now.minute, None
    spec = spec.strip()
    if spec:
        if "-" in spec:
            a, b = spec.split("-", 1)
            start, end = to_min(a.strip()), to_min(b.strip())
        else:
            start = to_min(spec)
    rec = {"day": day, "start": start, "end": end, "note": note,
           "created": now.isoformat()}
    os.makedirs(STATE, exist_ok=True)
    with open(MARKS, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def close_open_marks(when: int | None = None) -> list[dict]:
    """Stamp an end onto every still-open mark for today.

    An open mark otherwise runs until the next prompt or Slack send, which is
    the right default for "I am working on something the tracker cannot see"
    but the wrong one for a mark left running by mistake -- with nothing else
    happening it keeps accruing minutes to now, and by the time it is noticed
    the day already reads an hour long.

    `when` is the end as minutes-of-day; None means the current minute. The
    menu's stop item passes None -- you reached for the mouse and clicked it,
    so now is the truth. The hotkey passes the last real event instead: a
    hotkey gets pressed on the way out the door, and the minutes between the
    last prompt and the keypress are the leaving, not the work.

    Either end is clamped up to the mark's own start, because a span cannot
    finish before it began. The end may equal the start: a mark that began and
    ended in the same minute is a zero-length span, which marks_for() drops on
    its `end > start` test -- an accidental click leaves a record of having
    happened without contributing time. Stopping at the last event when nothing
    has happened since the mark closes it zero-length by that same rule, which
    is the honest reading of "credit the work the tracker saw"; the menu item
    is the way to bank a stretch it could not see.
    """
    if not os.path.exists(MARKS):
        return []
    now = now_local()
    day, now_m = now.strftime("%Y-%m-%d"), now.hour * 60 + now.minute
    end_m = now_m if when is None else when
    rows = [json.loads(l) for l in open(MARKS) if l.strip()]
    stamps = mark_stamps(day)
    closed = []
    for r in rows:
        if r.get("day") == day and r.get("end") is None:
            # Never past where the mark already reached. Closing is stamping on
            # the end a mark HAD, not granting it a new one: while open it ran
            # to the first event after it and no further than the cap, and every
            # reading of the day was made on those terms. Writing the closing
            # minute flat would hand back the minutes both rules withheld --
            # which on 2026-09-04 turned a 4-minute link into 4h25m, swallowing
            # two hours of breaks, at the click of End Session.
            r["end"] = max(min(end_m, open_mark_end(r, stamps, now_m, day)),
                           r["start"])
            r["closed"] = now.isoformat()
            closed.append(r)
    if not closed:
        return []
    tmp = f"{MARKS}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, MARKS)
    return closed


def last_entry_end(events: list[datetime]) -> int:
    """Minute-of-day the day's work ends at, if it ends at the last thing seen.

    The one definition of "end after the last entry", shared by the ⌘⌥S stop
    and by End Session so the two cannot drift into meaning different minutes.

    TAIL_SEC, not the bare stamp. A period already gets that much on top of its
    last prompt -- sending one is not an instantaneous act -- so ending a shift
    exactly on the stamp would credit the same last minute less than the
    ordinary rule does, and a person who pressed the stop shortcut would come
    out behind one who simply walked away. Following the buffer the model
    already applies is what keeps the two endings agreeing.

    A day with nothing in it yet returns 0, which close_open_marks clamps up to
    each mark's own start and thereby closes it zero-length -- the honest
    reading of "credit the work the tracker saw" when it saw none.
    """
    if not events:
        return 0
    last = max(events) + timedelta(seconds=TAIL_SEC)
    return last.hour * 60 + last.minute


def end_session(at_last: bool = False) -> dict:
    """Close everything currently holding the day open, at one minute.

    `unmark` alone is not that. It ends a manual mark, and a mark is only one of
    the things that keep the dot lit -- a scheduled meeting holds it green on
    the calendar's schedule regardless, and on an ordinary afternoon of
    prompting neither is running at all. So the shift toggle had nothing to
    offer someone who never pressed start and is now leaving: the only way to
    say "that was the day" was to click Stop on a mark that did not exist.

    This is that statement. It closes any open mark and cuts any meeting that
    would otherwise run past the chosen minute, in one step, so the menu item
    means the same thing whichever of them happened to be true.

    `at_last` ends at the last entry instead of at this minute, through the same
    last_entry_end the ⌘⌥S stop uses: they are one decision made in two places
    and must not resolve to two different minutes.

    A cut written when no meeting is running is inert -- effective_meeting_end
    only applies a cut to the meeting it landed inside -- so this does not need
    to ask whether one is, and cannot get that question wrong.

    And it records the minute itself, which for a long time it did not -- so on
    the ordinary afternoon this exists for, with nothing marked and nothing
    scheduled, both of the above were inert and the click changed nothing that
    could be seen. The dot stayed green, because prompting was what was holding
    it green, and the period ran straight on through the minute the day had
    just been declared over at. The declaration is now a thing in its own right:
    it breaks the period there and puts the dot out, and neither of those needs
    a mark or a meeting to have been running.

    Rebuilds the snapshot the way `mark`, `mode` and `meeting_end` do: ending
    the day changes the total as well as the dot, and waiting for the next
    20-minute check would leave the dashboard still counting a day the person
    just declared over.
    """
    now = now_local()
    day = now.strftime("%Y-%m-%d")
    events = events_for(day)
    when = last_entry_end(events) if at_last else now.hour * 60 + now.minute
    closed = close_open_marks(when)
    cuts = append_meeting_cut(when)
    ends = append_session_end(when)
    write_vault_snapshot(day, events)
    return {
        "at": hhmm_of(when),
        "at_last_entry": at_last,
        "closed": [{"start": hhmm_of(r["start"]), "end": hhmm_of(r["end"]),
                    "note": r["note"]} for r in closed],
        "cuts": cuts,
        "ends": [hhmm_of(m) for m in ends],
    }


def link_anchor(worked: list[dict], now_m: int, cutoff_sec: int,
                mark_start: int | None = None) -> int | None:
    """The minute "link with the last session" would join from, or None.

    One definition, read by both the menu item and the action behind it. The
    menu has to know whether the item applies before the click -- an item that
    is always offered and sometimes silently does nothing is worse than one
    that greys out -- and the action has to know which minute to claim from.
    Deriving those separately is how they end up disagreeing about which period
    counts as the last one, so they share this.

    Whether the newest period is still live is the only subtle part. Prompting
    right now means that period IS the current one, so linking has to reach
    past it to the period before: the claimed stretch then covers the gap
    between the two and they merge. With nothing live the newest period is
    itself the last one and the stretch simply carries it to now.

    Live is measured off the period's end against the cutoff the run in
    progress has earned -- the same cutoff the dot answers to -- so the item
    reaches past the current session exactly when the menu is calling one
    current.

    None means there is nothing to link to: an empty day, or a live period that
    is the only one there is. Not an error, just the ordinary state of the
    first session of the morning, and the item greys out for it.

    `mark_start` is where a currently-running mark begins, when one is running.
    A mark that began at or before the anchor is already holding those minutes
    open and there is nothing left to claim, so that greys the item out too. A
    mark that began AFTER the anchor does not: it holds the stretch in front of
    it and leaves the gap behind it exactly as unclaimed as if no mark existed,
    which is the case that used to be refused. Marking as working on returning
    to the desk and then reaching for the link is the obvious order to do those
    two things in, and it made the link a no-op.

    The minute returned is never in the future, and does not need clamping to
    ensure it. A period's end can sit slightly ahead of the clock, because
    TAIL_SEC is added to its last event -- but a period ending after now is
    live by this very test, so it is the one being reached past rather than the
    one being anchored on, and what gets anchored on is always behind it.
    """
    if not worked:
        return None
    live = (now_m - worked[-1]["end"]) * 60 <= cutoff_sec
    periods = worked[:-1] if live else worked
    if not periods:
        return None
    start = periods[-1]["end"]
    return None if mark_start is not None and mark_start <= start else start


def link_last_session() -> dict:
    """Join the last session to now, claiming the gap between them as work.

    The counterpart to End Session. That one says a stretch is over; this says
    it never stopped. A step away long enough to lapse -- a corridor
    conversation, a whiteboard, a call taken on the phone -- leaves a hole the
    tracker saw nothing in, and the work on either side of it comes back as two
    sessions with a gap between. Nothing in the menu could say the hole was
    work: `mark` starts at the current minute, so it could claim the time from
    the click forward but never the stretch already behind it, which is the
    only part that needs claiming.

    So the mark this writes starts in the past, at the minute the last session
    ended, and is left open. Open because the stretch it just rejoined is still
    going -- the person is back at the desk, that is why they clicked -- and
    closing it at the click would end the day in the act of extending it. It
    closes the way any other mark does: the next prompt resolves it, ⌘⌥S stops
    it, End Session ends it.

    Unless something is already holding it open. A mark running from AFTER the
    anchor is the common case -- back at the desk, marked as working, then
    reached for the link -- and there the gap behind that mark still needs
    claiming while the stretch in front of it is already spoken for. So the
    span written then is closed, ending where the running mark begins: it fills
    the hole and nothing more, and the day stays open on the mark that was
    already open rather than on a second one beside it. Two open marks was a
    real bug on the day marks shipped, and this is how that stays true without
    refusing the click.

    A mark running from at or before the anchor really does leave nothing to
    do, and link_anchor returns None for it -- the same None the menu greys out
    on, so the refusal below is reachable only by clicking through a menu that
    went stale while it was open.

    Rebuilds the snapshot like every other mutating command: the whole point is
    that the two sessions become one in the list the person is looking at, and
    waiting for the next check would leave them staring at the gap they just
    told it to close.
    """
    now = now_local()
    day = now.strftime("%Y-%m-%d")
    now_m = now.hour * 60 + now.minute
    events = events_for(day)

    path = snapshot_path(day)
    if not os.path.exists(path) or \
            json.load(open(path)).get("fp") != activity_fingerprint(day):
        write_vault_snapshot(day, events)
    worked = json.load(open(path)).get("worked", []) if os.path.exists(path) else []

    _last, stamps, ev_stamps, _acts = live_activity(day)
    running = next((m for m in marks_for(day, stamps)
                    if m["open"] and m["start"] <= now_m <= m["end"]), None)

    start = link_anchor(worked, now_m,
                        live_cutoff(day, [m * 60 for m in ev_stamps]),
                        running["start"] if running else None)
    if start is None:
        return {"linked": False, "why": "nothing to link to"}

    add_mark(f"{hhmm_of(start)}-{hhmm_of(running['start'])}" if running
             else hhmm_of(start), LINK_NOTE)
    write_vault_snapshot(day, events_for(day))
    # Where the claim actually reaches, which is the running mark's start when
    # there is one -- the minutes past that were already claimed, and reporting
    # `now` there would credit the link with them.
    to = running["start"] if running else now_m
    return {"linked": True, "from": hhmm_of(start), "to": hhmm_of(to),
            "gap": to - start}


# The note track_back() writes. Same shape of statement as LINK_NOTE: the
# minutes needed claiming precisely because nothing was watching them, so the
# note says where they came from rather than what they were.
TRACK_NOTE = "tracked by hand"

# How the claim behaves when it runs into work the tracker already counted.
#
# "clip" stops there and banks only the minutes between that period and now --
# asking for five minutes when the last two are all that is unaccounted for
# gets two, and does not invent the other three on top of time that is already
# counted. It is the default because it is the reading that cannot overstate
# the day.
#
# "split" keeps going instead: it steps over the period and carries the
# remainder to the free minutes in front of it. The five minutes then land as
# two after the period and three before it -- the same total, placed where
# there was actually a hole to put it in.
TRACK_MODES = ("clip", "split")


def claim_spans(worked: list[dict], now_m: int, minutes: int,
                split: bool) -> tuple[list[list[int]], int]:
    """Where `minutes` of hand-declared work go, walking back from `now_m`.

    Returns the spans to claim, oldest first, and how many minutes could not
    be placed. Every span is free of `worked`: claiming a minute the tracker
    already counted would not add to the day -- the periods are unioned -- but
    it would silently shorten the claim, so the minutes that "went missing"
    would be exactly the ones the person was trying to record.

    The walk is what both modes are made of. From the cursor it takes the free
    stretch behind it, up to whatever is still owed; when it hits a worked
    period it either stops (clip) or steps to the far side of it and carries
    on (split). One level of that is the case worth describing -- five minutes
    asked for, two free behind a Slack period, three placed in front of it --
    but a busy stretch can have several periods in it, and a loop is the only
    version that does not quietly drop the remainder at the second one.

    Stops at midnight rather than crossing into yesterday. A claim that ran
    backwards over the day boundary would file minutes against a day that is
    already summarised and closed, where nobody would look for them.
    """
    spans: list[list[int]] = []
    remaining = minutes
    cursor = now_m
    # Newest first, and only what could possibly be behind the cursor.
    ahead = sorted((p for p in worked if p["start"] < now_m),
                   key=lambda p: p["end"], reverse=True)
    i = 0
    while remaining > 0 and cursor > 0:
        # The newest period that begins before the cursor. Anything starting
        # at or after it is already behind us and cannot block this stretch.
        while i < len(ahead) and ahead[i]["start"] >= cursor:
            i += 1
        blocker = ahead[i] if i < len(ahead) else None
        # A period whose end runs past the cursor -- a live one, whose end
        # carries TAIL_SEC past the last event -- leaves no free minutes here
        # at all, so floor lands on the cursor and the take is zero.
        floor = min(blocker["end"], cursor) if blocker else 0
        take = min(cursor - floor, remaining)
        if take > 0:
            spans.append([cursor - take, cursor])
            remaining -= take
            cursor -= take
        if remaining <= 0 or blocker is None or not split:
            break
        cursor = blocker["start"]
        i += 1
    return list(reversed(spans)), remaining


def track_back(minutes: int, mode: str = "clip") -> dict:
    """Claim the last `minutes` as worked, around what is already counted.

    The hotkey's double press. A single press says "this minute was worked",
    which is the right size for a thought that arrives mid-task; this is for
    the stretch that just ended -- a phone call, a conversation at somebody's
    desk -- where the length is known and the minutes are behind you.

    `mark HH:MM-HH:MM` could always have written this, and that is the point:
    it asks for two clock times to be worked out in your head, in the minute
    after the thing you were doing ended. The count is the number a person
    actually has ("that was about ten minutes"), and what to do about the
    Slack message sent halfway through it is a question they should not have
    to answer in arithmetic.

    Written as ordinary closed marks, one per span, so nothing downstream
    needs to know this command exists: the period list, the day total and the
    dashboard all read them the way they read any other declared stretch.
    """
    if mode not in TRACK_MODES:
        return {"tracked": False, "why": f"unknown mode {mode!r}"}
    if minutes <= 0:
        return {"tracked": False, "why": "nothing to track"}
    now = now_local()
    day = now.strftime("%Y-%m-%d")
    now_m = now.hour * 60 + now.minute
    events = events_for(day)

    # The same snapshot the menu drew its period list from, refreshed if the
    # day has moved on since. Claiming against a stale one would step over a
    # period that is no longer there, or clip against one that has since grown.
    path = snapshot_path(day)
    if not os.path.exists(path) or \
            json.load(open(path)).get("fp") != activity_fingerprint(day):
        write_vault_snapshot(day, events)
    worked = json.load(open(path)).get("worked", []) if os.path.exists(path) else []

    spans, unplaced = claim_spans(worked, now_m, minutes, mode == "split")
    for start, end in spans:
        add_mark(f"{hhmm_of(start)}-{hhmm_of(end)}", TRACK_NOTE)
    if spans:
        write_vault_snapshot(day, events_for(day))
    return {"tracked": bool(spans), "mode": mode, "asked": minutes,
            "claimed": minutes - unplaced, "unplaced": unplaced,
            "spans": [{"start": hhmm_of(a), "end": hhmm_of(b)}
                      for a, b in spans]}


def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


APPROVALS = os.path.join(STATE, "approvals.jsonl")


def approval_rows_for(day: str) -> list[dict]:
    """One day's permission decisions, whole records, oldest first.

    Written by the Notification hook, because the transcript cannot express
    this: an approved tool and an unattended one produce the same `tool_result`
    row, so reading approvals out of the transcript would count agent activity
    as human presence. Only forward from the day the hook was installed --
    past approvals are genuinely unrecoverable.
    """
    if not os.path.exists(APPROVALS):
        return []
    out = []
    for line in open(APPROVALS):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("day") == day:
            out.append(r)
    return sorted(out, key=lambda r: r["t"])


def approvals_for(day: str) -> list[str]:
    """Just the times, as HH:MM:SS -- what the event streams need."""
    return [r["t"] for r in approval_rows_for(day)]


NOTES = os.path.join(STATE, "notes.jsonl")

# What an entry with no text says in the activity list. Phrased as what the
# person did rather than as what the tracker lacks -- "untracked work" would
# describe the probe's blind spot, and the row is about the work.
GENERIC_NOTE = "working (logged by hand)"


def note_rows_for(day: str) -> list[dict]:
    """One day's typed entries, whole records, oldest first.

    Every other stream infers presence from a trace the work happened to
    leave. This one is the person saying so directly -- a whiteboard, a phone
    call, a document in an app nothing here reads -- and it is treated as
    evidence on exactly the same footing as a prompt, because a statement
    from the only witness who was actually there outranks every signal that
    is guessing from a side effect.

    A point event, not a span: it says a minute was worked, and the ordinary
    chaining rule joins it to whatever is around it. `mark` remains the way
    to claim a stretch. Written by the menu bar's manual-entry hotkey.
    """
    if not os.path.exists(NOTES):
        return []
    out = []
    for line in open(NOTES):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("day") == day:
            out.append(r)
    return sorted(out, key=lambda r: r["t"])


def notes_for(day: str) -> list[str]:
    """Just the times, as HH:MM:SS -- what the event streams need."""
    return [r["t"] for r in note_rows_for(day)]


def add_note(text: str = "") -> dict:
    """Append one entry at this moment. Returns the record written.

    Text is optional because the shortcut that writes these does not ask for
    any: the whole point of a one-key entry is that it costs nothing to press,
    and a prompt for a description would interrupt the work being recorded.
    An entry with nothing typed still says the only thing this stream exists
    to say -- that the minute was worked -- so it gets a fixed label rather
    than a blank row, which would read as a bug in the list.
    """
    now = now_local()
    rec = {"day": now.strftime("%Y-%m-%d"), "t": now.strftime("%H:%M:%S"),
           "text": " ".join(text.split()) or GENERIC_NOTE}
    os.makedirs(STATE, exist_ok=True)
    with open(NOTES, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


FOCUS_DIR = os.path.join(STATE, "focus")

# How long after your last keystroke or mouse move the machine stops counting
# as attended. Below this you are working with a pause in it; above it you are
# somewhere else and the app that happens to be frontmost is just the app that
# was frontmost when you left.
#
# Two minutes is tight for reading -- a long diff or a spoken meeting produces
# no input for far longer -- and it is only safe because neither of those cases
# depends on this signal: covered_by_meeting() holds the call and
# github_visits_for() holds the review, both from evidence of their own. What
# this is left covering is the case with no other evidence at all, where the
# honest default is to stop counting rather than to keep crediting silence.
FOCUS_IDLE_SEC = 120

# How long a self-raising app must HOLD the front before its activation counts
# as somebody choosing it. Granola takes the foreground for a few seconds when
# a meeting ends and gives it straight back; a person who opens Granola stays
# in it. One heartbeat's worth was the old threshold, expressed as "survive to
# the next sample", and thirty seconds is the same line drawn where activations
# rather than samples can see it.
FOCUS_HOLD_SEC = 30

# How far an activation's NAME carries when nothing replaces it. Labelling
# only -- it buys no time, since credit is the activation itself.
#
# Needed because the last activation of a day has no successor to bound it, and
# because an app left in front overnight would otherwise put its name on every
# minute until morning. Ten minutes is roughly how long a window stays a fair
# description of what somebody was doing.
FOCUS_LABEL_MAX_SEC = 600

# Foreground time that counts as work, by bundle id. An allow list: an app
# earns credit only by being named here, and everything else in the foreground
# earns nothing.
#
# This used to be an exclude list, on the theory that nearly everything in the
# foreground of a work machine is the job. That is true of the apps a person
# opens and false of the machine they run on: macOS puts things in front that
# nobody chose to open -- a notification alert, the lock screen, the printer
# dialog that named a minute of the day "Add Printer". Each of those was a
# separate discovery made after it had already inflated a day, because an
# exclude list can only be right about the apps somebody thought to name, and
# the ones that need naming are exactly the ones nobody predicted.
#
# The cost of inverting it is real and is accepted: a new tool joining the
# rotation earns nothing until it is added below, and that looks like an
# ordinary quiet afternoon rather than like a bug. It is the better failure --
# bounded, and wrong in the direction of undercounting, where the exclude
# list's failure invented work that never happened.
#
# Two deliberate absences:
#
# Obsidian, because the vault is one app over two lives. The same window holds
# meeting notes and the grocery list, and being frontmost cannot say which, so
# an evening spent in personal notes read as an evening of work. Chrome had the
# same problem and got the same answer everywhere else in this file -- decide
# per page, not per app -- but a note has no URL to test, so the honest move is
# to count none of it. The real work done there leaves other traces anyway.
#
# The terminal, because it is ambiguous rather than because it is leisure. The
# same Ghostty window is the front app whether the work is on this Mac or on
# the Linux desktop, so its foreground time cannot tell the two apart, and the
# probe treats desktop work as absence from Rubrik work rather than as
# presence. Counting it would quietly re-add the very hours the desktop-prompt
# subtraction exists to remove. Little is lost: real terminal work here is
# Claude Code, and prompts already say so precisely.
FOCUS_INCLUDE = {
    "com.tinyspeck.slackmacgap",  # Slack
    "us.zoom.xos",                # Zoom
    "dev.zed.Zed",                # Zed
    "com.granola.app",            # Granola
    "com.github.GitHubClient",    # GitHub Desktop
}

# Apps on the list above that put THEMSELVES in front. For everything else,
# being frontmost is the residue of a person having chosen it, which is what
# makes the allow list mean anything; Granola opens its own window when a
# meeting starts and again when one ends, so "Granola is frontmost" can equally
# mean nobody has been at the machine for a quarter of an hour.
#
# That is not hypothetical. On 2026-09-02 the front app flipped from the
# terminal to Granola at 13:07:52 with the idle reading already at 783s, held
# it until the human came back at 13:12, and the day gained six minutes and a
# whole session row named after an app nobody had touched.
#
# The input gate in focus_counts() now covers the long absence for every app,
# and this set is what remains: the SHORT flash. Granola taking the front for a
# single heartbeat while somebody types in another window reads as attended --
# the idle is one second, because a person really is at the machine, just not
# here. What separates that from a window somebody opened is that this one was
# never chosen, so it has to survive a heartbeat before any of it counts.
FOCUS_SELF_RAISING = {
    "com.granola.app",            # Granola
}

# Chrome is not on that list and cannot be, because the question it answers is
# the wrong one: "is Chrome in front" says a browser is open, not what is in
# it, and shopping in the foreground is not work.
#
# It used to be excluded outright on those grounds, with its work routed
# through the history instead -- GitHub via github_visits_for(), everything
# else via chrome-work-blocks.py. The flaw in that was the flaw in history
# itself: it records NAVIGATIONS. A doc opened yesterday, left in a tab and
# read all morning produces no visit row at all, so the morning simply did not
# exist. That is not a rare shape; it is how a long document actually gets
# read.
#
# The bar now writes the active tab beside the app (see chromeActiveTab in
# main.swift), which makes the right question askable for the first time: not
# "is Chrome in front" but "is Chrome in front with a work page in it". So
# Chrome earns credit per SAMPLE and per PAGE rather than per app, which is
# strictly narrower than the allow list -- an hour of Hacker News in the
# foreground still earns nothing.
CHROME_BUNDLE = "com.google.Chrome"


# Resolved once. The profile is a file on disk and focus_app() is called for
# every row of a ~2,900-row log, several times per poll.
_FOCUS_INCLUDE_RESOLVED = FOCUS_INCLUDE | wc.focus_extra_apps()
_LONG_READ = wc.long_read()


def focus_idle_limit() -> int:
    """How long since the last input a sample may be and still count.

    Two numbers rather than one because they answer different questions.
    FOCUS_IDLE_SEC is tight because the case it guards -- an app left in front
    of an empty chair -- is the common one, and because the reading it would
    otherwise lose is held up by evidence of its own elsewhere. Somebody whose
    work is reading has no such other evidence, and for them the tight number
    is not caution, it is the deletion of their afternoon.
    """
    return _LONG_READ["max_idle_sec"] if _LONG_READ else FOCUS_IDLE_SEC


def focus_app(sample: dict) -> bool:
    """Whether the app in this sample is one whose foreground means work.

    Membership only -- it says nothing about whether anybody was there. The
    two questions were one function until a night with Slack left in front
    billed forty-one minutes to a machine nobody had touched.

    Title and URL are tested separately rather than as one string because
    is_work_url() reads everything before the first "?" as the address, and
    either half can carry a "?" that would swallow the other: a title ending
    in a question mark truncates the URL behind it, and a URL with a query
    string truncates a title placed after it. Two calls have neither problem.
    """
    bundle = sample.get("bundle")
    if bundle != CHROME_BUNDLE:
        return bundle in _FOCUS_INCLUDE_RESOLVED
    return (_work_site_hit(sample.get("tab", ""))
            or _work_site_hit(sample.get("url", "")))


def focus_counts(sample: dict) -> bool:
    """Whether the app in this sample earns foreground credit.

    A work app in front, and somebody at the machine within the last
    FOCUS_IDLE_SEC. The second half is what makes putting an app in front an
    EVENT rather than a subscription: the foreground does not renew itself,
    so a window holding still earns two minutes and then stops, and the next
    keystroke starts it again.

    Without it, being frontmost was the one piece of evidence here that a
    person did not have to be present to produce. Every other input is a thing
    somebody DID -- a prompt, a send, a meeting they sat in -- and stops
    arriving when they leave. Focus keeps arriving at the same rate all night,
    which is why an untouched screen used to bill like a working one.

    The cost is the case the gate cannot see: reading a long thread, typing
    nothing, for more than two minutes. That is a real loss and it is the
    right one to take -- an unattended machine reads identically, is far more
    common, and inflates a day by tens of minutes rather than deflating it.
    """
    return focus_app(sample) and sample.get("idle", 0) <= focus_idle_limit()


def focus_name(sample: dict) -> str:
    """What to call the app in this sample, for a row somebody reads.

    A browser is named by its page rather than by itself. "Google Chrome"
    describes a minute spent on a design doc and a minute spent on a PR
    identically, and the tab title is the only part of either that anybody
    would recognise as the thing they were doing.
    """
    if sample.get("bundle") == CHROME_BUNDLE and sample.get("tab"):
        return sample["tab"]
    return sample.get("app") or sample["bundle"]


def self_raised(prev: dict | None, sample: dict) -> bool:
    """Whether this activation is an app putting itself in front, not a person.

    The input gate in focus_counts() catches the long absence and misses the
    short one it is the same bug as: Granola flashing to the front for a single
    heartbeat while somebody types in another window. The idle reading there is
    one second, because a person really is at the machine -- just not in that
    window.

    So a self-raising app has to HOLD the front to count: `prev` is the
    activation that follows this one, and if it lands within FOCUS_HOLD_SEC the
    flash is discarded. The argument is named for the pairing it had under the
    span model, where it was the row before rather than the row after.

    The cost is a Granola session somebody opened and abandoned inside thirty
    seconds, which is not a session.
    """
    if sample.get("bundle") not in FOCUS_SELF_RAISING:
        return False
    if prev is None:
        return False
    return sec_of(prev["t"]) - sec_of(sample["t"]) < FOCUS_HOLD_SEC


# The bar's live reading: what is in front right now and how long since the
# last input. Overwritten every poll, never appended to -- see writePresence()
# in main.swift for why the two signals were split apart.
PRESENCE_PATH = os.path.join(STATE, "presence.json")


def presence_row() -> dict | None:
    """The bar's current reading, or None before it has ever written one."""
    if not os.path.exists(PRESENCE_PATH):
        return None
    with open(PRESENCE_PATH) as f:
        return json.load(f)


def focus_rows(day: str) -> list[dict]:
    """Raw focus samples for one day, in order.

    Written by the menu bar app on every switch of the front application; see
    the FocusLog comment in main.swift. Repeated rows for the same app are the
    heartbeat, which exists for the live dot's idle reading and not for credit
    -- focus_activations() is what strips them back to the switches.
    """
    path = os.path.join(FOCUS_DIR, f"{day}.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("day") == day:
            out.append(r)
    return sorted(out, key=lambda r: r["t"])


def focus_activations(day: str):
    """The moments a work app CAME to the front, in order.

    A row whose front THING differs from the row before it. The bar writes on
    every switch, so this is the switch itself; it also writes a heartbeat
    while nothing changes, and those repeats are what this drops.

    Derived rather than recorded, which is what makes it readable back through
    every log already on disk: a day of heartbeat rows reduces to exactly the
    switches that produced it. 2026-09-02's 2,989 rows are 569 activations.

    The "thing" is the bundle everywhere except Chrome, where it is the bundle
    and the tab. Chrome earns per PAGE rather than per app -- an hour of Hacker
    News in the foreground earns nothing -- so leaving a document for a PR
    inside the same window is a switch by every measure this code cares about,
    and keying on the bundle alone would file it as no event at all.

    The first row of the day counts as one. Whatever was in front at the first
    sample was put there by somebody, even if the switch itself happened
    yesterday -- and the alternative, skipping it, silently drops the opening
    app of every morning.
    """
    prev = None
    for r in focus_rows(day):
        key = (r.get("bundle"), r.get("tab") if r.get("bundle") == CHROME_BUNDLE
               else None)
        if key != prev:
            yield r
        prev = key


def focus_windows(day: str):
    """Spans for LABELLING, as (start_sec, end_sec, sample) -- not credit.

    An activation names the foreground until the next one replaces it, and
    that span is what answers "which app held this minute" and "what was I in
    during this period". It is not what answers "was this minute worked":
    focus_for() decides that from the activations alone, so a span here can
    cover minutes the day never counted. That separation is the point. The two
    questions were one function, and the answer to the second was silently
    borrowing the first's arithmetic -- which is how a window nobody touched
    came to bill forty-one minutes.

    Capped at FOCUS_LABEL_MAX_SEC. A name is only worth carrying as far as it
    stays true, and an app left in front overnight stops describing anything
    long before morning.
    """
    acts = list(focus_activations(day))
    for a, b in zip(acts, acts[1:] + [None]):
        if not focus_counts(a) or self_raised(b, a):
            continue
        lo = sec_of(a["t"])
        hi = sec_of(b["t"]) if b else lo + FOCUS_LABEL_MAX_SEC
        hi = min(hi, lo + FOCUS_LABEL_MAX_SEC)
        if hi > lo:
            yield lo, hi, a


def focus_for(day: str) -> list[datetime]:
    """Minutes spent attended, at the front of an app that counts as work.

    This is what replaced Slack sends as the evidence that a stretch in Slack
    was work. Sends were a bad proxy in the one direction that mattered:
    reading half an hour of a thread and answering nothing produced no evidence
    whatsoever, so the largest genuinely-working stretches Slack ever generated
    were exactly the ones it reported as gaps.

    One datetime per ACTIVATION -- the moment somebody put the app in front --
    and nothing for the time it then spent sitting there. Reaching for an app
    is a thing a person does, at a moment, exactly like sending a prompt; the
    period machinery is built on chaining point events and this is now one of
    them rather than a duration wearing the same shape.

    It used to credit the span between consecutive samples, which made being
    in front a subscription: the log ticks every FOCUS_HEARTBEAT_SEC whether or
    not anybody is there, so an app left in front billed at the same rate all
    night. Gating on idle capped the damage at two minutes per absence and left
    the model wrong in the same direction. What ends it is that a switch
    happens once.

    A stretch genuinely spent in one app is not lost with the duration. Nobody
    sits in a single window for an hour -- 2026-09-02 holds 569 activations --
    and each one seeds a bout that GAP_AFTER joins to its neighbours, so real
    work reads as a run of switches. Measured against the span model on live
    logs the whole day moves by -2 minutes (2026-09-01); the -39 on 2026-09-02
    is the untouched Slack evening this was built to stop counting.

    Somebody who does not switch is the one shape this misses, and
    focus_dwell_for() is what covers it -- folded in here rather than beside
    events_for() so that every caller of this function gets the same day. The
    dot, the total, the sessions and the snapshot all read focus through this
    one door, and a second door would have had them disagreeing about how long
    an afternoon was.
    """
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    acts = list(focus_activations(day))
    out = [base + timedelta(seconds=sec_of(a["t"]))
           for a, nxt in zip(acts, acts[1:] + [None])
           if focus_counts(a) and not self_raised(nxt, a)]
    return sorted(set(out) | set(focus_dwell_for(day)))


def focus_dwell_for(day: str) -> list[datetime]:
    """Events DURING a stay on one thing, for people whose work is reading.

    Empty unless long_read is enabled, so this changes nothing for a setup
    that does not ask for it.

    The problem it solves is the one focus_for() names as its accepted cost.
    Credit there is the activation -- the moment somebody reached for an app
    -- which works because a fast-navigating day is a run of switches, ~569 of
    them, close enough together that GAP_AFTER chains them into periods. Read
    one long article instead and the same day produces a SINGLE switch. The
    period machinery then sees one point event, and the thirty-nine minutes
    that followed it are silence indistinguishable from an empty room.

    So a stay emits more events as it continues -- but only while the log
    keeps saying somebody is there. This is deliberately NOT the span model
    that used to bill an untouched Slack window all night: that model credited
    the gap between two samples on the strength of the samples existing, and
    the samples exist whether or not anybody is in the chair. Here every event
    stands on its own row's idle reading, which is the number that goes up
    when the room empties. A parked tab climbs past max_idle_sec within
    minutes and stops earning; a person scrolling resets it to nothing on
    every flick of the wheel, because the reading counts scroll and gestures
    the same as keys.

    That is the whole distinction between reading and parking, and it is the
    only one available: both look identical from the history, which records
    navigations and so cannot see either.

    Events land at stride_sec apart rather than at every 30-second heartbeat
    because the period machinery needs them chained, not dense -- one every
    four minutes is enough to hold a bout open under a five-minute cutoff, and
    ninety-nine per hour would be the same day at ninety-nine times the cost.
    """
    if not _LONG_READ:
        return []
    stride = _LONG_READ["stride_sec"]
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    out, key_prev, anchor = [], None, None
    for r in focus_rows(day):
        key = (r.get("bundle"),
               r.get("tab") if r.get("bundle") == CHROME_BUNDLE else None)
        at = sec_of(r["t"])
        # A switch restarts the clock: the activation itself is focus_for()'s
        # to credit, and this function only ever adds to a stay already under
        # way. Without the reset, a run of quick switches would each inherit
        # the previous thing's anchor and earn a dwell event they did not sit
        # still for.
        if key != key_prev:
            key_prev, anchor = key, at
            continue
        if at - anchor >= stride and focus_counts(r):
            out.append(base + timedelta(seconds=at))
            anchor = at
    return out


def focus_app_by_minute(day: str) -> dict[int, str]:
    """The app that held most of each attended minute, in one pass.

    focus_apps() answers the same question for an arbitrary range, which is the
    right shape for labelling a period (a handful of calls a day) and the wrong
    one for labelling every minute: called in a loop it rescans the whole log
    each time, and the log is ~2,900 rows against ~1,400 minutes. That is four
    million row-visits on a path the menu bar polls every five seconds.
    """
    per: dict[int, dict[str, int]] = {}
    for lo, hi, a in focus_windows(day):
        name = focus_name(a)
        # A window can straddle a minute boundary, so its seconds are split
        # across the minutes it actually covers rather than all landing on the
        # minute it started in.
        for m in range(lo // 60, hi // 60 + 1):
            span = min(hi, (m + 1) * 60) - max(lo, m * 60)
            if span > 0:
                per.setdefault(m, {})[name] = per.setdefault(m, {}).get(name, 0) + span
    return {m: max(apps.items(), key=lambda kv: kv[1])[0] for m, apps in per.items()}


def focus_apps(day: str, lo: int, hi: int) -> list[str]:
    """Which apps held the foreground between two seconds-of-day, most first.

    Used to describe a period that has no prompts and no sends in it -- the
    stretch the old Slack-send signal could not see at all. Naming the app is
    the only description available for it, and is a better one than silence.
    """
    seen: dict[str, int] = {}
    for alo, ahi, a in focus_windows(day):
        span = min(ahi, hi) - max(alo, lo)
        if span <= 0:
            continue
        name = focus_name(a)
        seen[name] = seen.get(name, 0) + span
    return [k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


def last_focus_input(day: str) -> datetime | None:
    """The most recent moment somebody touched this Mac with a work app in front.

    Exists because focus_for() cannot answer the live question. It reports
    ACTIVATIONS, and somebody who switched to Slack at 09:00 and has been
    typing in it since produces exactly one point, at 09:00 -- which is the
    right answer for how much of the day was worked and the wrong one for
    whether they are at the desk now.

    So the live reading comes from presence.json, which the bar overwrites on
    every poll whether or not anything changed. That file is the heartbeat,
    moved off the focus log and out of the day's arithmetic: it has no history,
    so nothing can accumulate credit from it.

    A row's `idle` is measured backwards from it, so a sample at 09:05:00
    reading 45 makes the claim "somebody was at this machine at 09:04:15" --
    a point in time, at second resolution, true on its own without a
    neighbouring row to bound it. That is the same shape as a prompt, and it is
    the same arithmetic idle_stretches() already uses to place the START of an
    absence.

    Deliberately NOT folded into focus_for() or events_for(). The day's
    arithmetic is settled by the activations, and this can only ever make the
    live dot fresher, never a period longer or a total larger.
    """
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    best = None
    rows = focus_rows(day)
    live = presence_row()
    if live and live.get("day") == day:
        rows = rows + [live]
    for r in rows:
        # focus_app(), not focus_counts(): this asks WHEN somebody last typed,
        # and a stale row answers it as well as a fresh one -- t minus its own
        # idle reading is the same moment either way. Gating on idle here would
        # only drop rows that agree with the ones kept.
        if not focus_app(r):
            continue
        # Clamped at the day boundary: an idle reading spans midnight after a
        # night with the machine left on, and the day's own log is the wrong
        # place to record that the input happened yesterday.
        at = max(0, sec_of(r["t"]) - r.get("idle", 0))
        if best is None or at > best:
            best = at
    return base + timedelta(seconds=best) if best is not None else None

# Where the bar records that a person answered an idle prompt. Only the claims
# are written: the idle stretches themselves are already in the focus log, and
# deriving them here rather than having the app report them keeps one account
# of when the machine was untouched instead of two that can disagree.
IDLE_CLAIMS = os.path.join(STATE, "idle-claims.jsonl")

# How long a claim keeps counting after the click. Answering "yes I am here"
# while reading a long diff should not have to be answered again two minutes
# later; the cost of the window is that a claim made on the way out banks it.
IDLE_GRACE_SEC = 20 * 60

# Whether an unclaimed absence is actually taken out of the day. Off: the
# reading is too coarse to spend real minutes on -- HID idle counts only key
# and mouse events, so reading, watching a build, being on a call and thinking
# all look identical to walking out of the room, and the day lost time that
# was worked. Everything that MEASURES the absence still runs: idle_stretches()
# and bridged_idle() still report it, the bar still asks, and a claim is still
# recorded. Only the arithmetic ignores it, so the evidence needed to build a
# better rule keeps accumulating while a bad rule stops costing anything.
IDLE_SUBTRACTS = False


def idle_stretches(day: str) -> list[list[int]]:
    """Stretches the machine went untouched, as [start, end] seconds of day.

    Each sample's `idle` is measured backwards from the sample, so a row
    reporting 300s at 10:05:00 says nobody touched the machine since 10:00:00 --
    the stretch is recovered by subtracting, not by pairing rows. That is what
    makes the exclusion retroactive to the last real input rather than to the
    moment the threshold tripped, which would leave a two-minute tail of
    phantom work on the end of every absence.

    Overlapping readings are unioned: while away, every 30s sample reports a
    longer idle covering the same silence, so the raw list is ~60 nested spans
    for a half-hour absence and exactly one stretch is the truth.

    Returned unprotected. Meetings, marks and claims are cut out by the caller,
    which is the same order desktop_holes is applied in -- the exemption list
    belongs with the subtraction, not with the evidence.
    """
    raw = []
    for r in focus_rows(day):
        idle = r.get("idle", 0)
        if idle <= FOCUS_IDLE_SEC:
            continue
        end = sec_of(r["t"])
        raw.append([max(0, end - idle), end])
    if not raw:
        return []
    raw.sort()
    out = [raw[0]]
    for lo, hi in raw[1:]:
        if lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def idle_claims_for(day: str) -> list[list[int]]:
    """Spans a person claimed by answering the idle prompt, in seconds of day.

    A claim covers the silence it was asked about AND the grace window after
    it, so the two are one span rather than a point plus a rule applied later.
    """
    if not os.path.exists(IDLE_CLAIMS):
        return []
    out = []
    for line in open(IDLE_CLAIMS):
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("day") == day:
            out.append([r["from"], r["until"]])
    return sorted(out)


def idle_cut(day: str, protected: list[list[int]]) -> list[list[int]]:
    """What the day loses to absence: every untouched stretch, less the ones
    already spoken for.

    A claim is protected exactly like a mark, because it is one: the same
    declaration that this silence was work, made in answer to a question
    rather than unprompted.

    Empty while IDLE_SUBTRACTS is off, which is the switch's whole effect --
    kept as one function rather than an `if` at the call site so the
    off-behaviour is something a test can run rather than something it has to
    read the caller to know about.
    """
    if not IDLE_SUBTRACTS:
        return []
    return subtract_spans(idle_stretches(day), protected + idle_claims_for(day))


def bridged_idle(day: str, timeline: list[tuple[int, str]]) -> list[list[int]]:
    """Idle stretches that a working session closed over, newest last.

    A stretch only shows up in the activity list if work resumed before the
    cutoff that would have ended the period -- three minutes away between two
    bouts changed the day's arithmetic and is worth seeing, where six minutes
    away simply ended the period, and the period ending already says that
    without a second row repeating it.

    "Resumed" is read off the log rather than assumed: the stretch must be
    closed by a later sample showing the machine touched again. An absence
    still running is not yet a bridged one, and would otherwise appear as a
    completed event while it was still happening.
    """
    rows = focus_rows(day)
    if not rows:
        return []
    touched = [sec_of(r["t"]) for r in rows if r.get("idle", 0) <= FOCUS_IDLE_SEC]
    out = []
    for lo, hi in idle_stretches(day):
        if not any(t > hi for t in touched):
            continue
        if hi - lo >= gap_sec_for(mode_at(timeline, lo), hi - lo):
            continue
        out.append([lo, hi])
    return out


# The WSL box's activity export -- see the header comment at the top of
# Dashboard/Vault Dashboard.md for the full inventory of what it writes.
ACTIVITY_DIR = os.path.join(wc.dashboard_dir(), "activity")

CHROME_ROW = re.compile(
    r"^\|\s*(\d{1,2}:\d{2})\s*\|\s*chrome\s*\|\s*visit\s*\|[^|]*\|\s*(.*?)\s*\|\s*$")

# Non-GitHub evidence of work: any address naming a work keyword (the
# employer's name, which covers the LMS, the IdP and every internal tool that
# would otherwise have to be listed one domain at a time), and any Google page
# signed in as the work account. Both are configured in the profile and read
# once here rather than per visit -- see worktime_common.is_work_url.
WORK_URL_KEYWORDS = wc.work_url_keywords()
GOOGLE_WORK_ACCOUNT = wc.google_work_account()
WORK_LOCALHOST_PORTS = wc.work_localhost_ports()

# The exporter truncates the detail column at 80 characters, and a GitHub page
# puts its title before the URL. A real PR title -- "Add Plugins section to
# Agent Details Capabilities tab by adamgee · Pull Re" -- consumes the whole
# budget, so the URL never appears in the row and a URL-only test drops it.
# Matching on the title as well is what makes code review visible at all: of
# the 13 PR pages in six days of export, a github.com test caught one, and it
# caught that one only because its title happened to be short.
#
# "Pull Re", not "Pull Request #": the truncation lands mid-phrase.
GITHUB_TITLE = re.compile(r"·\s*Pull Re|·\s*scaledata/|/pull/\d+", re.I)

# Logging in is not working. The SAML pair fires every weekday morning at the
# same minute, and the oauth/authorize rows are Supabase and Microsoft sign-ins
# for personal projects on the other machine -- counted as github.com visits,
# they manufactured Rubrik work periods out of a personal login. Scoped to
# GitHub hits only (see _work_site_hit) -- the work SSO's own login pages
# legitimately live under paths like this, and excluding them here would
# defeat the point of tracking that domain at all.
GITHUB_AUTH = re.compile(r"/saml/|/login/oauth/", re.I)

# Workday has the same problem GITHUB_TITLE was written for, from the other
# end. Its addresses are already work by host (ALWAYS_WORK_HOSTS), but the
# 80-character export row spends its budget on the title and truncates the URL
# away: "Self Assessment: FY27 OPE Mid-Year Check-In: Oliver Ullman - Workday
# — https://w" is a real row, and nothing left in it is a Workday address.
# What survives is the suffix Workday puts on every page title, so that is
# what the row has to be matched on.
#
# Anchored to the end of the title -- the em dash the exporter puts before the
# URL, or the end of a live tab title -- so it means the page named itself
# Workday, not that the word happened to appear in a headline.
WORKDAY_TITLE = re.compile(r"-\s*workday\s*(?:—|$)", re.I)


def _work_site_hit(text: str) -> bool:
    """True if `text` (a URL, or an export detail that may carry one) is
    evidence of work: a GitHub code page that isn't a login redirect, a
    Workday page named by its title, or a visit that is work in its own right
    (see worktime_common.is_work_url)."""
    lowered = text.lower()
    if "github.com" in lowered or GITHUB_TITLE.search(text):
        return not GITHUB_AUTH.search(text)
    if WORKDAY_TITLE.search(text):
        return True
    return wc.is_work_url(text, WORK_URL_KEYWORDS, GOOGLE_WORK_ACCOUNT,
                          WORK_LOCALHOST_PORTS)


# Chrome's own History DB, this machine's live counterpart to the activity
# export. The export is written by a nightly job, so on its own it makes today
# the one day with no browser evidence at all -- a morning of code review shows
# up tomorrow, which is precisely when it is no longer useful.
chrome_history_path = wc.chrome_history_path


_gh_live_cache: dict[str, tuple] = {}


def _live_url_filter() -> tuple[str, list]:
    """The SQL that narrows Chrome's history to candidate work pages.

    A prefilter only -- every row it returns is still put through
    _work_site_hit, which is what rejects a search for the company name or a
    Google page signed in as the personal account. Its job is to keep the
    query off the other 99% of a 58MB history, so it is deliberately looser
    than the real test, never tighter: anything it drops here can never be
    recovered downstream.
    """
    clauses = ["urls.url LIKE '%github.com%'"]
    params = []
    for host in wc.ALWAYS_WORK_HOSTS:
        clauses.append("urls.url LIKE ?")
        params.append("%{}%".format(host))
    for keyword in WORK_URL_KEYWORDS:
        clauses.append("urls.url LIKE ?")
        params.append("%{}%".format(keyword))
    if GOOGLE_WORK_ACCOUNT is not None:
        clauses.append("urls.url LIKE '%.google.com/%'")
    return " OR ".join(clauses), params


def github_live_rows(day: str) -> list[tuple[datetime, str]]:
    """Today's work-site visits (GitHub, and whatever else the profile counts
    as work) read straight from this Mac's Chrome history.

    Chrome holds the DB open, so it is copied before being read -- the copy is
    ~0.04s for 58MB and the result is memoised on the file's mtime and size,
    which keeps it off the five-second poll.

    Unlike the export this carries the full URL, so the page can be matched
    properly instead of through the truncated-title heuristics GITHUB_TITLE
    needs. Lag is about a minute: Chrome batches writes, but not by much.
    """
    path = chrome_history_path()
    if path is None:
        return []
    st = os.stat(path)
    key = (day, st.st_mtime_ns, st.st_size)
    if _gh_live_cache.get("key") == key:
        return _gh_live_cache["rows"]

    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    url_filter, url_params = _live_url_filter()
    day_start = wc.chrome_micros(base)
    day_end = wc.chrome_micros(base + timedelta(days=1))
    # Whether a visit is one somebody made cannot be read off the visit alone
    # -- it is a property of the redirect chain it belongs to, and a chain's
    # earlier hops are on whatever URL redirected here, which the prefilter
    # has every reason to have dropped. So the chains are read separately and
    # unfiltered, widened by the chain window so one starting just before
    # midnight is still whole. Both queries share the one copy of the DB,
    # which is the only part of this that costs anything.
    margin = timedelta(seconds=wc.REDIRECT_CHAIN_SEC)
    raw, chains = wc.read_history_queries(path, [
        ("""SELECT visits.id, visits.visit_time, urls.url, urls.title
              FROM visits JOIN urls ON urls.id = visits.url
             WHERE visits.visit_time BETWEEN ? AND ?
               AND ({})
          ORDER BY visits.visit_time""".format(url_filter),
         [day_start, day_end] + url_params),
        ("""SELECT id, from_visit, visit_time, transition FROM visits
             WHERE visit_time BETWEEN ? AND ? ORDER BY visit_time""",
         [wc.chrome_micros(base - margin),
          wc.chrome_micros(base + timedelta(days=1) + margin)]),
    ])
    user_initiated = wc.user_initiated_visit_ids(chains)

    rows = []
    for vid, stamp, url, title in raw:
        if vid not in user_initiated or not _work_site_hit(url):
            continue
        when = wc.chrome_time(stamp, LOCAL)
        # Title only when there is one. The URL is the fallback rather than a
        # suffix because the tooltip truncates at roughly a PR title's length,
        # so appending it buys nothing and costs the end of the title.
        rows.append((when, title or url))
    _gh_live_cache.clear()
    _gh_live_cache["key"], _gh_live_cache["rows"] = key, rows
    return rows


def github_rows_for(day: str) -> list[tuple[datetime, str]]:
    """GitHub code pages read, from the live history and the export together.

    Live rows win their minute outright. They are the better record of the same
    browsing -- full URL, second resolution, available the moment it happens --
    and the export's value is the days and the machine the live read cannot
    see: yesterday, and the Linux desktop's synced visits.
    """
    live = github_live_rows(day)
    covered = {when.strftime("%H:%M") for when, _ in live}
    merged = live + [(when, detail) for when, detail in github_export_rows(day)
                     if when.strftime("%H:%M") not in covered]
    return sorted(merged, key=lambda r: r[0])


def github_export_rows(day: str) -> list[tuple[datetime, str]]:
    """Chrome visits to GitHub and the other work sites, with the page each
    one landed on.

    Reading a PR or a diff is real work and was previously invisible to this
    probe -- prompts and Slack sends were the only evidence of working, so a
    stretch spent entirely in a browser reviewing code read as a gap. It is
    also the only evidence that stretch produces: a review generates no prompt
    and no Slack message. The same is true of a training course, an internal
    tool reached through SSO, or an hour in the work Google account.

    Restricted to these sites rather than all Chrome activity: the export
    also carries plain browsing (shopping, general search) that is not work,
    and counting every visit would manufacture "working" out of that.
    """
    path = os.path.join(ACTIVITY_DIR, f"{day}.md")
    if not os.path.exists(path):
        return []
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    out = []
    for line in open(path):
        m = CHROME_ROW.match(line)
        if not m:
            continue
        detail = m.group(2)
        if not _work_site_hit(detail):
            continue
        h, mnt = m.group(1).split(":")
        out.append((base.replace(hour=int(h), minute=int(mnt)), detail))
    return sorted(out, key=lambda r: r[0])


def github_visits_for(day: str) -> list[datetime]:
    """Just the visit times -- what the event streams need."""
    return [when for when, _detail in github_rows_for(day)]


DESKTOP_ROW = re.compile(
    r"^\|\s*(\d{1,2}:\d{2})\s*\|\s*claude\s*\|\s*prompt\s*\|")


def desktop_prompts_for(day: str) -> list[datetime]:
    """Claude prompts sent from the OTHER machine, per its activity export.

    The desktop runs personal projects only -- Overland-iOS, property-search,
    autojournal and the rest. Rubrik work happens on the Mac and nowhere else,
    so a prompt over there is evidence of absence from the job, the one signal
    in this file with that polarity. Everything else here proves presence.

    Measured against the Mac's own stream these are all but disjoint: over six
    days, 305 desktop prompts against 799 Mac-side work events, overlapping in
    only a handful of minutes. Two thirds of them already land in gaps the
    tracker had closed anyway -- switching machines makes the Mac go quiet, and
    the ordinary cutoff ends the period without help. It is the other third,
    inside a period the Mac was still holding open, that this exists for.
    """
    path = os.path.join(ACTIVITY_DIR, f"{day}.md")
    if not os.path.exists(path):
        return []
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    out = []
    for line in open(path):
        m = DESKTOP_ROW.match(line)
        if not m:
            continue
        h, mnt = m.group(1).split(":")
        out.append(base.replace(hour=int(h), minute=int(mnt)))
    return sorted(out)


# The exporter runs every five minutes while the desktop is up, so an hour
# without a write is the box off or the job dead -- either way the gaps this
# feed would have explained are going unexplained. Reported beside the dot,
# never through it: the Mac's own reading is still right, just less informed.
ACTIVITY_STALE_SEC = 60 * 60


def activity_feed_notice(now: datetime) -> str | None:
    """A one-line note when the desktop's activity export has gone quiet."""
    names = sorted(n for n in os.listdir(ACTIVITY_DIR) if n.endswith(".md"))
    if not names:
        return "Desktop activity feed: no exports yet"
    path = os.path.join(ACTIVITY_DIR, names[-1])
    with open(path) as fh:
        stamp = next(line.split(":", 1)[1].strip() for line in fh
                     if line.startswith("generated_at:"))
    at = datetime.fromisoformat(stamp)
    if (now - at).total_seconds() <= ACTIVITY_STALE_SEC:
        return None
    when = at.strftime("%H:%M") if at.date() == now.date() \
        else at.strftime("%b %-d %H:%M")
    return f"Desktop activity feed silent since {when}"


def events_for(day: str) -> list[datetime]:
    """Everything that proves someone was working.

    Prompts, attended foreground minutes, permission approvals, hand-logged
    entries, and github.com Chrome visits, merged into one list on purpose. A
    Slack reply two minutes after a prompt continues that work period;
    treating the streams separately would put a gap between them and then
    count the same stretch twice.

    Slack SENDS are deliberately absent, though slack_for() still runs and
    still fills the tooltip. Every minute a send used to prove is a minute the
    machine was attended with Slack in front of it, so focus_for() already
    covers them -- and covers the far larger set of minutes spent reading,
    which sends never could. Keeping both would not double-count (the union is
    by minute) but it would leave two definitions of the same evidence to drift
    apart, and the weaker one is only ever a subset of the stronger.

    It also drops the one thing sends could see that focus cannot: a message
    sent from the phone. That is the intended trade -- typing on a phone is
    poor evidence of being at the desk working, and it was previously enough to
    hold the dot green from a sofa.
    """
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)

    def at(hms: str) -> datetime:
        h, m, s = hms.split(":")
        return base.replace(hour=int(h), minute=int(m), second=int(s))

    out = list(prompts_for(day))
    out += focus_for(day)
    out += [at(t) for t in approvals_for(day)]
    out += [at(t) for t in notes_for(day)]
    out += github_visits_for(day)
    return sorted(out)


# How many recent events the menu bar lists. Ten is roughly one working
# period's worth of evidence -- enough to recognise the stretch the dot is
# currently reporting on, and to see the moment it started.
ACTIVITY_LIST_N = 10

# Long enough to recognise a prompt or a message, short enough that the widget
# is choosing where to truncate for its own width rather than being handed a
# paragraph. Matches the spirit of TIP_PROMPT_CHARS.
ACTIVITY_TEXT_CHARS = 120


def one_line(text: str, unescape: bool = False) -> str:
    """Flatten to a single line, capped, for a native menu row.

    Optionally reverses the HTML escaping the snapshot layer applies. That
    escaping exists for the dashboard, which renders into markup; this payload
    is read by an AppKit menu, where `&amp;` is not an ampersand but the
    literal five characters. Only the streams that were escaped on the way in
    get unescaped here -- doing it blindly would eat a real "&amp;" someone had
    typed.
    """
    t = " ".join(text.split())
    if unescape:
        t = t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return t[:ACTIVITY_TEXT_CHARS]


def activity_rows(day: str) -> list[dict]:
    """The day's work events, newest first, each with what it was.

    The same streams events_for() merges, deliberately: this is meant to be the
    readable form of exactly what the dot's verdict was derived from, so a
    stream that moves the dot but is missing here -- or the reverse -- would
    make the list a second opinion on the day rather than an explanation of it.

    Slack is the one entry that is no longer evidence in its own right, since
    sends stopped counting as presence. It stays because it is not a separate
    claim about the day: a message was typed with Slack in front of you on an
    attended machine, so the minute it names is a minute focus already counted.
    It says what that minute was ABOUT, which is the one thing focus cannot.

    That last part is checked rather than assumed -- a send only appears if
    focus really did count its minute. Sending from the phone breaks the
    assumption, and an unchecked row is worse here than a missing one: the
    header above this list reads "Nm since last activity" off the last event,
    which cannot see Slack, so a send newer than any tracked event would sit
    at the top of the list showing a smaller age than the header, disagreeing
    with it about the one number they are both reporting.

    Focus fills in last, and only for minutes nothing else explains. Emitting
    it for every attended minute would satisfy the invariant and destroy the
    list: focus covers nearly every minute at the keyboard, so it would
    interleave with the prompts and push all ten slots onto "Ghostty". What is
    worth showing is the stretch that has no other evidence -- which is exactly
    the stretch this signal was added to see.

    Desktop prompts stay out for the same reason. They do reach the model, but
    with the opposite polarity: a prompt on the other machine is evidence of
    being away from this job, and listing it under "work" would read as the
    tracker claiming the exact opposite of what it concluded.

    Scoped to one day because everything else the widget shows is -- a list
    that quietly reached into yesterday would be the only part of the menu not
    describing today.
    """
    rows: list[tuple[str, dict]] = []
    attended = focus_for(day)
    attended_minutes = {t.hour * 60 + t.minute for t in attended}

    # Sort keys are HH:MM:SS where the stream has seconds and HH:MM:00 where it
    # does not. Prompts and Chrome visits are only recorded to the minute, so
    # within a shared minute their order against Slack is arbitrary -- which is
    # invisible, since the rows are displayed to the minute too.
    for sess in full_day(day).get("sessions", []):
        for pr in sess.get("prompts", []):
            rows.append((f"{pr['ts']}:00", {
                "t": pr["ts"], "kind": "prompt",
                "what": one_line(pr["text"])}))

    for r in slack_for(day):
        hh, mm = int(r["t"][:2]), int(r["t"][3:5])
        if hh * 60 + mm not in attended_minutes:
            continue
        # `ch` is empty for a DM, where the search response carries the other
        # party's user ID rather than a name -- so `im` is what decides the
        # label, exactly as the period summaries key on it.
        where = "DM" if r["im"] or not r["ch"] else f"#{r['ch']}"
        rows.append((r["t"], {
            "t": r["t"][:5], "kind": "slack",
            "what": one_line(f"{where} · {r['text']}", unescape=True)}))

    for r in approval_rows_for(day):
        rows.append((r["t"], {
            "t": r["t"][:5], "kind": "approval",
            "what": f"approved {r.get('tool') or 'a tool'}"}))

    for r in note_rows_for(day):
        rows.append((r["t"], {
            "t": r["t"][:5], "kind": "note",
            "what": one_line(r["text"])}))

    for when, detail in github_rows_for(day):
        rows.append((when.strftime("%H:%M:00"), {
            "t": when.strftime("%H:%M"), "kind": "browsing",
            "what": one_line(detail)}))

    # Every stretch the machine went untouched, listed alongside the events.
    # With IDLE_SUBTRACTS off these no longer change the day's arithmetic, so
    # the row is now an observation rather than an explanation of a missing
    # total -- which is exactly what makes it worth keeping: it is the record
    # of what the rule WOULD have cut, and the only way to tell whether the
    # threshold is anywhere near right before turning it back on. Shown at the
    # moment the machine went quiet, and only once it is over -- see
    # bridged_idle.
    for lo, hi in bridged_idle(day, mode_timeline(day)):
        fate = ", not counted" if IDLE_SUBTRACTS else " (still counted)"
        rows.append((f"{lo // 3600:02d}:{lo // 60 % 60:02d}:{lo % 60:02d}", {
            "t": f"{lo // 3600:02d}:{lo // 60 % 60:02d}", "kind": "idle",
            "what": f"away {round((hi - lo) / 60)}m{fate}"}))

    spoken = {k[:5] for k, _ in rows}
    by_minute = focus_app_by_minute(day)
    for when in attended:
        hm = when.strftime("%H:%M")
        app = by_minute.get(when.hour * 60 + when.minute)
        if hm in spoken or not app:
            continue
        rows.append((f"{hm}:00", {"t": hm, "kind": "focus", "what": app}))

    rows.sort(key=lambda r: r[0], reverse=True)

    # Collapse a consecutive run of the identical event into one row carrying
    # its count -- the same trap chrome-work-blocks unions intervals for. One
    # PR page reloaded through a redirect chain lands eight times in two
    # minutes, and eight rows of it is not eight things that happened; it filled
    # the entire list on a real day, hiding every other piece of evidence behind
    # one page. Only an adjacent run merges, so returning to that PR an hour
    # later is still its own row rather than being folded into the earlier one.
    #
    # Collapsed before the limit the caller applies, so ten rows are ten
    # distinct activities rather than ten samples of however few. The whole day
    # is collapsed rather than stopping at the tenth row: cutting the walk short
    # there would leave that last row's own duplicates uncounted, so it alone
    # would report a smaller number than it should.
    #
    # The whole day is returned, uncapped. The menu's raw list takes the top
    # ten; the sessions view groups all of them, and a grouped view built from
    # only the newest ten would report counts for its oldest session that were
    # a slice of the list rather than what the session contained.
    out: list[dict] = []
    for _, a in rows:
        if out and out[-1]["kind"] == a["kind"] and out[-1]["what"] == a["what"]:
            out[-1]["n"] += 1
            continue
        out.append(dict(a, n=1))

    # An absolute instant alongside the clock time, because the widget shows
    # these as ages ("4m", "2h") and an age has to be recomputed against the
    # current time, not baked in here. This result is memoised behind a
    # fingerprint that only changes when the underlying files do, so a "4m"
    # written at write time would still say "4m" an hour later.
    #
    # Resolved through LOCAL, like every other day-anchored construction here.
    # A naive strptime().timestamp() reads the ambient C-library zone instead,
    # and that is precisely the zone this file never trusts: under the sandbox
    # some callers run in it answers UTC, which shifts every `at` by the whole
    # offset -- four hours in EDT -- so the widget ages a thing that just
    # happened as "4h" while the clock time beside it stays right.
    for a in out:
        a["at"] = stamp_epoch(day, a["t"])
    return out


def stamp_epoch(day: str, t: str) -> float:
    """A day plus an "HH:MM" wall-clock string as an absolute instant."""
    return (datetime.strptime(f"{day} {t[:5]}", "%Y-%m-%d %H:%M")
            .replace(tzinfo=LOCAL).timestamp())


# How many sessions the grouped view lists. Fewer than the raw list because a
# session is a whole stretch of the day rather than one event, so eight of them
# already reaches back further than ten raw rows ever do.
SESSION_LIST_N = 8

# How many of a session's own rows travel with it, for the submenu that shows
# what the session was made of. Twenty rather than the dozen a tooltip could
# hold: a submenu is a menu, so it scrolls when it outgrows the screen instead
# of spilling off it, and an hour-long stretch has more than a dozen things in
# it worth reading.
SESSION_TIP_N = 20


def group_sessions(rows: list[dict], worked: list[dict],
                   limit: int = SESSION_LIST_N,
                   live: bool = True) -> list[dict]:
    """The same rows, divided into the day's work periods. Newest first.

    `live` is whether the newest period is still running, and only it can carry
    the "now" the widget draws. Being last in the list is not the same claim: a
    day whose work stopped at 13:39 still has a newest period at 18:00, and
    marking it current put "· now" on a stretch three quarters of an hour dead
    -- beside a header that correctly said the day had gone quiet. The caller
    decides with the same lapse test the header uses, so the two agree by
    construction rather than by coincidence.

    Deliberately grouped by the periods the snapshot already recorded rather
    than by re-deriving boundaries from gaps between these rows. A second rule
    would divide the same day a second way, and the two divisions would sit
    three rows apart in one menu disagreeing about when the morning ended --
    the exact failure the period list and the "since last activity" header were
    folded together to stop. Here the sessions ARE the periods; what this adds
    is what each one was made of.

    Every row lands in exactly one session, including rows in minutes no period
    covers -- an approval during a stretch that lapsed, say. Those group into
    their own uncounted sessions instead of being dropped, because the raw list
    shows them and a grouped view that quietly held fewer events than the list
    it toggles with would be the second opinion this is trying not to be.
    """
    def minute(r: dict) -> int:
        return int(r["t"][:2]) * 60 + int(r["t"][3:5])

    def period_of(m: int) -> int | None:
        for i, w in enumerate(worked):
            if w["start"] <= m <= w["end"]:
                return i
        return None

    out: list[dict] = []
    for r in rows:
        m = minute(r)
        idx = period_of(m)
        # Rows inside a period always join it. Uncounted rows have no period to
        # belong to, so they run together only while they stay within the same
        # silence that ends a period -- otherwise two stray approvals hours
        # apart would print as one session spanning the afternoon between them.
        if out and out[-1]["_idx"] == idx and (idx is not None
                                               or out[-1]["start"] - m <= GAP_AFTER):
            s = out[-1]
        else:
            w = worked[idx] if idx is not None else None
            s = {"_idx": idx,
                 "start": w["start"] if w else m,
                 "end": w["end"] if w else m,
                 # An uncounted run has no length to report: its minutes are
                 # precisely the ones the day total left out, and printing a
                 # span here would read as time credited.
                 "len_sec": w["len_sec"] if w else 0,
                 "what": (w.get("what") or "") if w else "",
                 "counted": w is not None,
                 "current": live and idx is not None and idx == len(worked) - 1,
                 "n": 0, "kinds": [], "rows": []}
            out.append(s)
        # Rows arrive newest first, so the oldest one seen for an uncounted run
        # is the one that sets its start.
        if not s["counted"]:
            s["start"] = m
        s["n"] += r["n"]
        kinds = dict(s["kinds"])
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + r["n"]
        # Biggest first: the breakdown is one line and the widget truncates it,
        # so what falls off the end should be the smallest contributor.
        s["kinds"] = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
        # The rows themselves ride along so hovering a session can show what it
        # was made of without a second call and without the widget having to
        # re-derive which rows belonged to which session. Capped, because a
        # busy period holds a hundred of them and a tooltip taller than the
        # screen shows nothing usefully; `n` above still counts them all, and
        # the widget says how many the cap left out.
        if len(s["rows"]) < SESSION_TIP_N:
            s["rows"].append({k: r[k] for k in ("t", "kind", "what", "n")})
        # And the row is told which session took it. The raw list uses this to
        # show, when one of its rows is hovered, which of the others came from
        # the same stretch -- the grouping is the answer the sessions view
        # gives, and this is that answer without leaving the list. Written onto
        # the caller's own row dicts, since those are what the raw list is cut
        # from and a parallel index would be a second thing to keep in step.
        r["session"] = len(out) - 1

    for s in out:
        del s["_idx"]
    return out[:limit]


def recent_activities(day: str, limit: int = ACTIVITY_LIST_N) -> list[dict]:
    """The newest events only -- what the menu's raw list shows."""
    return activity_rows(day)[:limit]


# Markdown, not JSON. Obsidian Sync ships .md between devices by default but
# skips every other file type unless "Sync all other file types" is switched
# on, so a .json dump written on another machine silently never arrives -- it
# is not a transport that can be relied on without that setting. A table in a
# note syncs, and stays readable in Obsidian besides.
CAL_FILE = os.path.join(wc.dashboard_dir(), "calendar-today.md")
CAL_STALE_HOURS = 6

CAL_ROW = re.compile(
    r"^\|\s*(\d{1,2}:\d{2})\s*\|\s*(\d{1,2}:\d{2})\s*\|\s*(.*?)\s*\|"
    r"(?:\s*([A-Za-z]+)\s*\|)?")
# The day the dump covers, read from its heading and nowhere else. An
# unanchored date matched the frontmatter's `generated:` line first, so a file
# written today but headed with yesterday read as current -- exactly the shape
# a refresh that ran against the wrong day would leave behind. The heading is
# the contract; the frontmatter says when the file was made, not what is in it.
# Both dashes appear in the wild (the exporter emits an em dash; older files a
# hyphen), so the separator is not part of the match.
CAL_DATE = re.compile(r"^#.*?(\d{4}-\d{2}-\d{2})", re.M)
# `generated:` is preferred and `updated:` is the fallback. Obsidian's
# update-time-on-edit plugin rewrites `updated:` seconds after any write and
# strips the timezone offset while doing it, so the exporter also emits
# `generated:`, which nothing touches. Reading only `updated:` meant the
# freshness check ran against a naive timestamp the plugin had rewritten.
CAL_GENERATED = re.compile(r"^generated:\s*(\S+)", re.M)
CAL_UPDATED = re.compile(r"^updated:\s*(\S+)", re.M)

# Which side of the personal/work line a calendar row falls on. Only work
# blocks are evidence of *working*; a therapy appointment or a football match
# is time genuinely spent, but not on the job, and counting it inflated a whole
# afternoon. Rows from an exporter that predates the Calendar column carry no
# tag at all -- those stay counted, because the old free/busy feed was the work
# calendar and silently dropping them would erase every real meeting.
CAL_WORK_TAGS = {"work", "rubrik", ""}


SUMMARIES = os.path.join(STATE, "summaries.json")


def load_api_key() -> str | None:
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    try:
        for line in open(os.path.expanduser("~/.claude/tokens.env")):
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return None


_FULL_CACHE: dict[str, dict] = {}


def full_day(day: str) -> dict:
    """The uncapped prompt-count payload for one day, fetched at most once.

    Memoised because the callers below run per work period, and a day has
    dozens -- without this a single snapshot spawned dozens of identical
    subprocesses, each re-reading every transcript on disk.
    """
    if day not in _FULL_CACHE:
        r = subprocess.run(
            [PYTHON, PROMPT_COUNT,
             "--day", day, "--full"],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"prompt-count --full failed ({r.returncode}): {r.stderr.strip()}")
        _FULL_CACHE[day] = json.loads(r.stdout) if r.stdout.strip() else {}
    return _FULL_CACHE[day]


def texts_in(day: str, lo: int, hi: int) -> list[str]:
    """Prompt texts inside a minute range, oldest first. Uncapped."""
    hits = []
    for sess in full_day(day).get("sessions", []):
        for pr in sess.get("prompts", []):
            m = int(pr["ts"][:2]) * 60 + int(pr["ts"][3:])
            if lo <= m <= hi:
                hits.append((m, pr["text"]))
    return [t for _, t in sorted(hits)]


# Enough to recognise a prompt, not enough to turn the tooltip into the
# transcript. The full text is a click away in the session itself.
TIP_PROMPT_CHARS = 110


def sessions_in(day: str, lo: int, hi: int) -> list[dict]:
    """Prompts inside a minute range, grouped by the conversation they came from.

    A work period regularly spans two or three sessions at once, and a flat
    list of prompts loses the one thing that explains why the period looks
    scattered: they were different conversations, interleaved.
    """
    # Keyed by label, not by session id: resuming a conversation starts a new
    # session on disk but is the same conversation to the human, and listing
    # "automated daily digest" twice in one tooltip reads as a bug.
    by_label: dict[str, list[dict]] = {}
    for sess in full_day(day).get("sessions", []):
        for pr in sess.get("prompts", []):
            m = int(pr["ts"][:2]) * 60 + int(pr["ts"][3:])
            if not lo <= m <= hi:
                continue
            label = sess.get("label") or "(untitled session)"
            # Newlines and angle brackets go straight into tooltip markup, so
            # they are flattened and escaped here rather than at each renderer.
            text = " ".join(pr["text"].split())[:TIP_PROMPT_CHARS]
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            by_label.setdefault(label, []).append({"ts": pr["ts"], "text": text})

    out = [{"label": k.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
            "prompts": sorted(v, key=lambda p: p["ts"])}
           for k, v in by_label.items()]
    # Ordered by when each conversation was first touched in this period, so
    # the list reads in the order the work actually happened.
    return sorted(out, key=lambda s: s["prompts"][0]["ts"])


# Temporarily disabled: these calls were billing tokens from this laptop.
# Once summaries route through a desktop-side relay, flip this back on.
SUMMARIZE_SPANS_ENABLED = False


def summarize_span(day: str, lo: int, hi: int) -> str:
    """A <=10-word description of one work period, cached on disk.

    Cached by (day, start, end) because the probe reruns every 20 minutes and
    a finished period never changes -- without it each cycle would re-bill the
    same handful of spans forever. The still-open final period is deliberately
    NOT cached: its content grows as the day does, so a cached line would
    freeze at whatever the first prompt happened to be about.
    """
    if not SUMMARIZE_SPANS_ENABLED:
        return ""
    key = f"{day}:{lo}-{hi}"
    try:
        cache = json.load(open(SUMMARIES))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]

    texts = texts_in(day, lo, hi)
    if not texts:
        return ""
    api = load_api_key()
    if not api:
        return ""

    body = json.dumps({
        "model": os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL",
                                "claude-haiku-4-5-20251001"),
        "max_tokens": 40,
        "messages": [{"role": "user", "content":
            "Here are the prompts someone typed during one stretch of work:\n\n"
            + "\n".join(f"- {t[:160]}" for t in texts[:40])
            + "\n\nDescribe what they worked on in AT MOST 10 words. "
              "No preamble, no trailing period, no quotes. "
              "Name the actual subject, not the activity: "
              "'timezone bug in prompt counter' beats 'debugging an issue'."}],
    }).encode()
    req = urllib.request.Request(
        os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        + "/v1/messages",
        data=body,
        headers={"x-api-key": api, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = json.loads(r.read())["content"][0]["text"].strip()
    except Exception:
        return ""
    text = " ".join(text.split()[:10])
    cache[key] = text
    tmp = f"{SUMMARIES}.{os.getpid()}.tmp"
    os.makedirs(os.path.dirname(SUMMARIES), exist_ok=True)
    json.dump(cache, open(tmp, "w"))
    os.replace(tmp, SUMMARIES)
    return text


def calendar_from_vault(day: str) -> list[dict] | None:
    """Read a calendar dump some other client wrote into the vault.

    Exists so a Claude client that already holds a Calendar connection can
    supply the data without this machine needing its own OAuth grant. Expects a
    note carrying `updated:` frontmatter, the date in its heading, and a
    markdown table of `| HH:MM | HH:MM | title |` rows.

    The file is refused once it is older than CAL_STALE_HOURS or is for the
    wrong day. Silently serving a stale note would be worse than having no
    calendar at all: the probe would suppress pings against meetings that
    already ended and mark real idle time as work, and nothing downstream
    could tell.
    """
    try:
        with open(CAL_FILE) as fh:
            text = fh.read()
    except OSError:
        return None

    head = CAL_DATE.search(text)
    if not head or head.group(1) != day:
        return None
    up = CAL_GENERATED.search(text) or CAL_UPDATED.search(text)
    if not up:
        return None
    try:
        gen = datetime.fromisoformat(up.group(1))
    except ValueError:
        return None
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=LOCAL)
    if now_local() - gen > timedelta(hours=CAL_STALE_HOURS):
        return None

    # Identical rows are collapsed. The exporter emits a free/busy calendar
    # where every block carries the same placeholder title, and it wrote the
    # 08:15-09:00 block twice on 2026-08-27 -- two rows agreeing on start, end
    # AND title cannot be distinguished from one another by anything this file
    # records, so they cannot be two different things as far as presence is
    # concerned. Kept as boundary normalisation of an external feed, not as a
    # guard over our own data: overlapping blocks with DIFFERENT titles are
    # left alone, because those are a real double-booking.
    out = []
    seen = set()
    for line in text.split("\n"):
        m = CAL_ROW.match(line.strip())
        if not m:
            continue
        if m.groups() in seen:
            continue
        seen.add(m.groups())
        sh, sm = m.group(1).split(":")
        eh, em = m.group(2).split(":")
        title = m.group(3)
        tag = (m.group(4) or "").strip().lower()
        # A free/busy-only calendar exposes no titles, so the source cannot say
        # what the block was -- only that it was busy. Recorded as such rather
        # than invented, since the title is what a later review reads to judge
        # whether the block really counted as work.
        out.append({
            "start": int(sh) * 60 + int(sm),
            "end": int(eh) * 60 + int(em),
            "title": title or "(busy)",
            "confidence": "accepted",
            "calendar": tag or None,
            # Kept on every row rather than filtered here, so a personal
            # appointment still shows in the dashboard tooltip as an
            # explanation for the quiet -- it just stops counting as work.
            "counts": tag in CAL_WORK_TAGS,
        })
    return out


# How old the vault dump may get before a refresh is launched. Well inside
# CAL_STALE_HOURS so a failed attempt has several more tries before the day
# actually goes calendar-blind, and long enough that the steady state is a
# handful of refreshes a day rather than one per idle hour.
CAL_REFRESH_AFTER = timedelta(hours=2)
# Floor between attempts, which only bites while refreshes are FAILING. A
# broken connection would otherwise spawn one on every probe run -- once a
# minute, all day.
CAL_RETRY_AFTER = timedelta(minutes=30)
CAL_REFRESH_STAMP = os.path.join(STATE, "calendar-refresh-attempt")
CAL_REFRESH_CMD = os.path.expanduser("~/.claude/bin/calendar-refresh.sh")
# Where the detached refresh reports. It outlives the probe run that started it,
# so nothing is left to read its exit code -- this file is the only place a
# failure can surface. It used to go to /dev/null, and a refresh that failed on
# every attempt for a whole morning looked exactly like one that was never run.
CAL_REFRESH_LOG = os.path.join(STATE, "calendar-refresh.log")


def refresh_calendar_if_stale(now: datetime | None = None) -> bool:
    """Launch the calendar refresh when the vault dump is going stale.

    It lives here, rather than in a launchd job of its own, because of what
    reaching the vault costs: the dump, the refresh script and the repo it
    lives in are all under ~/Documents, and a LaunchAgent gets "Operation not
    permitted" on every one of them -- macOS grants that directory per
    executable, and /bin/zsh does not have it. The probe does, by inheritance
    from the bar app, and it already runs every minute. So the machine that
    can do this is the one already awake.

    Fire-and-forget on purpose. The refresh shells out to a language model and
    takes the better part of a minute; waiting on it would stall a probe run
    that has a dot to draw. Nothing here reads the result -- the next run picks
    the new file up by finding it on disk, the same way it would have found one
    written by anything else.

    Returns whether an attempt was launched, for the tests and for the caller.
    False is the ordinary answer: the file is usually fresh.
    """
    now = now or now_local()
    day = now.strftime("%Y-%m-%d")

    # The stamp is written BEFORE the attempt and is what the retry floor is
    # measured from, so a refresh that hangs or dies without writing anything
    # still counts as having been tried. Measuring from the dump's own age
    # instead would retry on every run for as long as the failure lasted.
    try:
        last = datetime.fromtimestamp(os.path.getmtime(CAL_REFRESH_STAMP), LOCAL)
    except OSError:
        last = None
    if last and now - last < CAL_RETRY_AFTER:
        return False

    # Age is read from the file's own `generated:` stamp rather than its mtime,
    # for the same reason calendar_from_vault does: the vault syncs, and a copy
    # landing from another machine touches the mtime without making the
    # contents any newer.
    fresh = False
    try:
        with open(CAL_FILE) as fh:
            text = fh.read()
        head = CAL_DATE.search(text)
        up = CAL_GENERATED.search(text) or CAL_UPDATED.search(text)
        if head and head.group(1) == day and up:
            gen = datetime.fromisoformat(up.group(1))
            if gen.tzinfo is None:
                gen = gen.replace(tzinfo=LOCAL)
            fresh = now - gen < CAL_REFRESH_AFTER
    except (OSError, ValueError):
        fresh = False
    if fresh:
        return False

    if not os.access(CAL_REFRESH_CMD, os.X_OK):
        return False

    os.makedirs(os.path.dirname(CAL_REFRESH_STAMP), exist_ok=True)
    with open(CAL_REFRESH_STAMP, "w") as fh:
        fh.write(now.isoformat())
    try:
        # Overwritten, not appended: it answers "why did the last attempt
        # fail", and at one attempt per half hour an append would grow forever.
        log = open(CAL_REFRESH_LOG, "w")
        log.write(f"{now.isoformat()} refresh launched\n")
        log.flush()
        subprocess.Popen(
            [CAL_REFRESH_CMD],
            stdout=log, stderr=subprocess.STDOUT,
            # Detached, so the refresh outlives the probe run that started it.
            # A one-minute child of a process that exits in seconds would
            # otherwise be killed halfway through and never write anything.
            start_new_session=True)
    except OSError:
        return False
    return True


def calendar_events(day: str) -> list[dict] | None:
    """Busy intervals for one local day, from whichever source can supply them.

    The vault dump is checked first: when it is present and fresh it is the
    deliberate answer from a client that actually holds the calendar
    connection, and it should win over an API grant that may not exist. Both
    paths produce the same shape, so the caller cannot tell them apart and
    nothing downstream depends on which one answered.

    Returns None when NO source could supply data -- distinct from [], which
    means "asked, you had nothing". The caller must not conflate the two:
    treating an auth failure as an empty calendar reproduces the exact bug this
    integration exists to fix, silently and with more confidence than before.

    Declined invitations and all-day events are dropped. A scheduled event is
    not proof of attendance, so `confidence` marks which side of that line an
    event falls on: `accepted` is a strong claim, `tentative` is a weak one the
    fit should be able to down-weight rather than swallow whole.
    """
    vault = calendar_from_vault(day)
    if vault is not None:
        return vault

    # gcloud is optional: this machine may have no SDK installed at all, which
    # is the same answer as an unusable grant -- "no source could supply data",
    # the None this function is documented to return. Letting the missing
    # binary raise instead killed the whole status call, and the menu bar has
    # no way to tell a crash from a real verdict: the dot just went red.
    try:
        token = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True, text=True,
        ).stdout.strip()
    except FileNotFoundError:
        return None
    if not token:
        return None

    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    end = start + timedelta(days=1)
    url = (
        "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        f"?timeMin={start.isoformat()}&timeMax={end.isoformat()}"
        "&singleEvents=true&orderBy=startTime&maxResults=100"
    )
    raw = subprocess.run(
        ["curl", "-s", "-H", f"Authorization: Bearer {token}", url],
        capture_output=True, text=True,
    ).stdout
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if "items" not in data:
        return None  # error payload (403 scopes, 401 expired) -- not "no meetings"

    out = []
    for ev in data["items"]:
        s, e = ev.get("start", {}), ev.get("end", {})
        if "dateTime" not in s:
            continue  # all-day: says nothing about any particular hour
        me = next((a for a in ev.get("attendees", []) if a.get("self")), None)
        status = me.get("responseStatus") if me else "accepted"
        if status == "declined":
            continue
        if ev.get("transparency") == "transparent":
            continue  # marked free by the organiser
        st = datetime.fromisoformat(s["dateTime"]).astimezone(LOCAL)
        en = datetime.fromisoformat(e["dateTime"]).astimezone(LOCAL)
        out.append({
            "start": st.hour * 60 + st.minute,
            "end": en.hour * 60 + en.minute,
            "title": ev.get("summary", "(untitled)"),
            "confidence": "accepted" if status in ("accepted", "needsAction") else status,
            # This path reads the work account's own primary calendar, so
            # everything on it is work by construction. The personal/work split
            # is a property of the vault feed, which merges two accounts.
            "calendar": "work",
            "counts": True,
        })
    return out


def read_meeting_cuts() -> list[int]:
    """Minutes-of-day at which a meeting was declared over, today."""
    try:
        rec = json.load(open(MEETING_CUT))
    except (OSError, ValueError):
        return []
    if rec.get("day") != now_local().strftime("%Y-%m-%d"):
        return []
    return sorted(rec.get("cuts", []))


def append_meeting_cut(cut_min: int) -> list[int]:
    """Record that a meeting ended at cut_min. Returns today's full cut list."""
    cuts = sorted(set(read_meeting_cuts()) | {cut_min})
    tmp = MEETING_CUT + f".{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"day": now_local().strftime("%Y-%m-%d"), "cuts": cuts}, fh)
    os.replace(tmp, MEETING_CUT)
    return cuts


def read_session_ends(day: str | None = None) -> list[int]:
    """Minutes-of-day the person declared the day over at, on `day`.

    Kept apart from the meeting cut even though End Session writes both. A cut
    says a scheduled thing stopped early; this says the person stopped. They
    happen to coincide when the menu item is clicked, and conflating them would
    mean every early-exit from a standup also drew a line through the afternoon.
    """
    try:
        rec = json.load(open(SESSION_END))
    except (OSError, ValueError):
        return []
    if rec.get("day") != (day or now_local().strftime("%Y-%m-%d")):
        return []
    return sorted(rec.get("ends", []))


def read_session_end_ts(day: str | None, end_min: int) -> float | None:
    """The exact epoch second End Session was clicked to produce end_min.

    status() floors activity to the minute before comparing it against
    end_min, which makes activity seconds after the click indistinguishable
    from activity before it when both land in the same minute. This gives
    status() the click's real instant to break that tie, without disturbing
    end_min itself -- split_at_session_ends and the rest of the event model
    still only ever see whole minutes.
    """
    try:
        rec = json.load(open(SESSION_END))
    except (OSError, ValueError):
        return None
    if rec.get("day") != (day or now_local().strftime("%Y-%m-%d")):
        return None
    return rec.get("ends_ts", {}).get(str(end_min))


def append_session_end(end_min: int) -> list[int]:
    """Record that the day was declared over at end_min. Returns today's list."""
    day = now_local().strftime("%Y-%m-%d")
    prior_ends = read_session_ends(day)
    prior_ts = {str(m): read_session_end_ts(day, m) for m in prior_ends}
    ends = sorted(set(prior_ends) | {end_min})
    ends_ts = {k: v for k, v in prior_ts.items() if v is not None}
    ends_ts[str(end_min)] = now_local().timestamp()
    tmp = SESSION_END + f".{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"day": day, "ends": ends, "ends_ts": ends_ts}, fh)
    os.replace(tmp, SESSION_END)
    return ends


def split_at_session_ends(spans: list[list[int]], ends_sec: list[int],
                          stamps: list[int]) -> list[list[int]]:
    """Break work spans where the day was declared over, in seconds.

    A period is built from the events inside it and knows nothing about the
    person's intent, so a stretch that had End Session clicked in the middle of
    it -- because the afternoon resumed twenty minutes later, or because the
    click simply preceded the next prompt -- came out as one unbroken run
    straight through the declaration. The menu then showed a session still
    going at a minute the person had just said was the end of one, which is the
    exact thing the item is for.

    A split, not a truncation: the minutes after the declaration were still
    worked if something was done in them, and dropping them would make the item
    a way to lose time rather than a way to divide it.

    The piece after a break is dropped when no event falls in it, because that
    piece is the tail: build_bouts hands every bout TAIL_SEC past its last
    event, so ending at 08:40 on an 08:39 prompt would otherwise publish a
    phantom 08:40-08:44 period made of nothing but the buffer. Only pieces
    after a break are eligible -- a mark or a meeting is a span with no events
    in it by nature, and the first piece is that span itself.
    """
    out: list[list[int]] = []
    for a, b in spans:
        cuts = sorted(e for e in ends_sec if a < e < b)
        if not cuts:
            out.append([a, b])
            continue
        bounds = [a] + cuts + [b]
        for i, (lo, hi) in enumerate(zip(bounds, bounds[1:])):
            if i and not any(lo <= t < hi for t in stamps):
                continue
            out.append([lo, hi])
    return out


def effective_meeting_end(m: dict, cuts: list[int]) -> int:
    """When a meeting actually ended: its scheduled end, or a cut inside it.

    A cut only truncates the meeting it landed inside. Applying the day's cut
    to every meeting -- which is what a single `cut_min` compared against every
    row did -- meant ending the 10:00 standup at 10:30 also gave the 14:00
    review an effective end of 10:30, i.e. an end before its own start, so it
    could never cover a minute again. One early exit erased every later meeting
    on the calendar. That was survivable while the only way to cut was a human
    clicking a menu item once in a while; it is not survivable now that the end
    of any call can write one.
    """
    inside = [c for c in cuts if m["start"] <= c < m["end"]]
    return min(inside) if inside else m["end"]


def covered_by_meeting(when: datetime, meetings: list[dict]) -> dict | None:
    mins = when.hour * 60 + when.minute
    cuts = read_meeting_cuts()
    for m in meetings:
        if m["start"] <= mins < effective_meeting_end(m, cuts):
            return m
    return None


def read_cursor() -> datetime | None:
    try:
        with open(CURSOR) as fh:
            return datetime.fromisoformat(json.load(fh)["last_check"])
    except (OSError, ValueError, KeyError):
        return None


def write_cursor(when: datetime) -> None:
    os.makedirs(STATE, exist_ok=True)
    tmp = f"{CURSOR}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"last_check": when.isoformat()}, fh)
    os.replace(tmp, CURSOR)


def append_label(rec: dict) -> None:
    os.makedirs(STATE, exist_ok=True)
    with open(LABELS, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def resolve_pending(now: datetime, events: list[datetime]) -> int:
    """Settle prior pings. Silence means the guess stood.

    The interaction is correction-only: a ping goes through unless it is
    contradicted, so silence IS the label and every settled window counts. An
    earlier version parked un-corroborated windows in an `unknown` bucket and
    dropped them, which quietly discarded most of a day's evidence to guard
    against a bias that has not shown up.

    The guard is kept as data rather than as a filter. `corroborated` records
    whether a prompt landed within CONFIRM_WINDOW of the ping -- evidence the
    human was actually at the keyboard to see it. Every row is usable; if
    accepted-but-unseen pings later turn out to skew the fit, that flag is what
    lets a model down-weight them, and nothing had to be thrown away first.
    """
    if not os.path.exists(LABELS):
        return 0
    rows = [json.loads(l) for l in open(LABELS) if l.strip()]
    changed = 0
    for r in rows:
        if r.get("resolution") != "pending":
            continue
        asked = datetime.fromisoformat(r["asked_at"])
        if now - asked < timedelta(minutes=CONFIRM_WINDOW):
            continue  # still inside the window; leave it pending
        saw = any(asked < e <= asked + timedelta(minutes=CONFIRM_WINDOW) for e in events)
        r["resolution"] = "accepted_silent"
        r["corroborated"] = saw
        r["truth"] = r["guess"]          # silence means the guess stood
        r["resolved_at"] = now.isoformat()
        changed += 1
    if changed:
        tmp = f"{LABELS}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        os.replace(tmp, LABELS)
    return changed


# One file per day rather than a single rolling "today". At 00:01 the day the
# human wants to look at is almost always the one that just ended, and a single
# file cannot serve that -- it has already been overwritten with an empty day.
VAULT_SNAPSHOT_DIR = os.path.join(wc.dashboard_dir(), "worktime")


def snapshot_path(day: str) -> str:
    return os.path.join(VAULT_SNAPSHOT_DIR, f"{day}.json")


def meetings_from_snapshot(day: str) -> list[dict] | None:
    """Recover a past day's meetings from the snapshot it already published.

    calendar-today.md holds only today, so calendar_events returns None for
    every earlier day. Rebuilding one without this -- a backfill, or any re-run
    after midnight -- dropped every meeting the day had, and with them the work
    time a meeting was holding open. A `backfill 6` did exactly that: 36 minutes
    disappeared from 2026-08-28 and nothing in the output said a source had gone
    missing, because an absent calendar is indistinguishable from a day with no
    meetings once the read has returned None.

    Reading them back off the snapshot makes a rebuild idempotent, which is the
    property backfill needed all along.
    """
    path = snapshot_path(day)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return None
    seen, out = set(), []
    for w in snap.get("worked", []):
        for m in w.get("meetings") or []:
            key = (m.get("start"), m.get("end"), m.get("title"))
            if key in seen:
                continue
            seen.add(key)
            out.append(m)
    return out or None


def hhmm_of(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def elapsed_s(day: str, now: datetime | None = None) -> int:
    """How much of `day` has happened, in seconds -- the clamp on every span
    with declared bounds, so a meeting still running is not credited past now.

    The clock alone is only the answer for today. It used to be the answer for
    every day: rebuilding 2026-09-09 at 07:56 the next morning clamped that
    day to 07:56, so every meeting and mark after breakfast fell out of the
    worked time while staying on the periods as labels. A 12:15 call went back
    to five minutes of foreground slivers, and nothing said a source was gone.
    A past day has happened in full; a future one has not started.
    """
    now = now or now_local()
    today = now.strftime("%Y-%m-%d")
    if day < today:
        return 24 * 3600
    if day > today:
        return 0
    return now.hour * 3600 + now.minute * 60 + now.second


def write_vault_snapshot(day: str, events: list[datetime],
                         fp: str | None = None) -> None:
    """Publish today's work periods where the Obsidian dashboard can read them.

    `fp` is the fingerprint the caller took BEFORE it read `events`, and it is
    what the snapshot publishes as its own. Taking it here instead -- which is
    what this did, at the very end, beside the rest of the record -- signed the
    file with a later state of the inputs than the periods were built from.
    Deriving a day costs ~650ms, so any event landing inside that window got a
    snapshot claiming to already cover it: status() compared fingerprints,
    found a match, and left the period list frozen while the header line beside
    it -- read live -- moved on. That is the "0m since last activity" sitting
    above a newest period three quarters of an hour old. A fingerprint taken
    too early only costs a redundant rebuild on the next poll, so when the
    caller has not passed one, take it before any derivation starts.

    Dataview can only load paths inside the vault, so the label file in
    ~/.claude cannot be read from a note. Rather than symlink (which Obsidian's
    watcher handles inconsistently), the probe pushes a snapshot on every run
    and the dashboard treats it as read-only. Obsidian's file watcher picks up
    the change, so the widget refreshes on its own within a probe interval.

    Bouts are recomputed from raw prompt timestamps rather than assembled from
    the 20-minute check windows: the windows are an artifact of the cron
    schedule, and rendering them would draw the sampling rate instead of the
    behaviour.
    """
    # Everything below works in SECONDS, and only the published snapshot rounds
    # to minutes. Minute arithmetic is what produced the zero-length periods:
    # four prompts inside one minute are four identical minute stamps, so the
    # period they form measured last-minus-first = 0.
    #
    # A run of prompts no further apart than the cutoff in force is one work
    # period, from its first prompt to its last plus TAIL_SEC, never shorter
    # than MIN_PERIOD_SEC. That cutoff is GAP_AFTER in focused mode and a
    # per-bout ramp in unfocused mode -- see gap_sec_for.
    # Nothing coarser is applied afterwards: an earlier version absorbed every
    # break under thirty minutes into the surrounding work, which is why a
    # single "2h 33m" period could contain three holes of 28, 23 and 23 minutes
    # and still be drawn as one solid block.
    fp = activity_fingerprint(day) if fp is None else fp
    now_s = elapsed_s(day)
    meetings = calendar_events(day)
    if meetings is None:
        meetings = meetings_from_snapshot(day)
    # `events` is prompts AND Slack sends, and that union is what the period
    # boundaries are built from. The per-period prompt list has to come from the
    # prompt stream alone: derived from `events` instead, n_prompts counted
    # every Slack message as a prompt, so a stretch spent entirely in Slack
    # reported prompts it never had and no period was ever Slack-only.
    stamps = sorted(t.hour * 3600 + t.minute * 60 + t.second for t in events)
    prompt_stamps = sorted(t.hour * 3600 + t.minute * 60 + t.second
                           for t in prompts_for(day))

    tl = mode_timeline(day)
    present = build_bouts(stamps, tl)

    # Manual marks are presence, unioned in the same way meetings are. They
    # carry no tail or lead: a mark has declared bounds and needs neither.
    present += [[m["start"] * 60, min(m["end"] * 60, now_s), True]
                for m in marks_for(day) if m["start"] * 60 < now_s]

    # Work meetings count as presence and are unioned in, so a gap the calendar
    # explains never reaches the list at all. They carry no tail -- a meeting
    # has a real end time and does not need one inferred.
    #
    # Personal rows are excluded. They are still real appointments and still
    # explain the silence, which is why they stay on the period for the tooltip
    # to show, but a therapy session is not time on the job: counting them held
    # 2026-08-27 open from 14:30 to 16:30 on a football fixture.
    #
    # Cuts apply here too, not only to the live dot. Declaring a meeting over
    # at 10:30 moved the dot to amber but still handed the day the full hour it
    # was scheduled for, so the total said an hour of work nobody did and the
    # dot and the total disagreed about the same half hour.
    cuts = read_meeting_cuts()
    present += [[m["start"] * 60, min(effective_meeting_end(m, cuts) * 60, now_s),
                 True]
                for m in (meetings or [])
                if m.get("counts", True) and m["start"] * 60 < now_s]

    # The day starts at the first work after DAY_ANCHOR, not at the first work
    # of the calendar day. Work before it is the previous night spilling over --
    # a 01:48 session would otherwise anchor the day there and manufacture a
    # six-hour "gap" out of a night's sleep.
    present = sorted((p for p in present if p[0] >= DAY_ANCHOR * 60),
                     key=lambda p: p[0])

    # A meeting can overlap or abut a prompt run, and a tail can now reach into
    # the next run, so the two lists still have to be unioned.
    merged = merge_spans(present, tl)

    # Where the person said the day was over. After the merge, so a declaration
    # cannot be undone by the very rejoining it was made to prevent, and before
    # the subtractions, which only ever remove -- they cannot close the break
    # back up.
    merged = split_at_session_ends(merged, [m * 60 for m in read_session_ends(day)],
                                   stamps)

    # Cut out the stretches spent on the other machine. This runs AFTER the
    # merge, not before: merge_spans rejoins anything closer together than the
    # cutoff, so a hole opened earlier would be closed again on the way past --
    # the subtraction silently undid itself. The point of the hole is that the
    # silence is not silence, it is evidence of being elsewhere, so nothing
    # downstream may treat it as a gap small enough to absorb.
    #
    # Subtraction rather than a weight on the period. A weight has to invent a
    # rule for which minutes to blame, and it gets the answer wrong: 08-26 has
    # a worse work:desktop ratio than 08-25 and a visibly better day. The
    # desktop's own timestamps already say which minutes went elsewhere, so
    # there is nothing left to model. It also keeps the day total equal to the
    # sum of the published spans, which the dashboard depends on: it derives
    # worked time as day-span-minus-gaps and never reads work_minutes, so a
    # separate "credited" figure would reach the menu bar and not the dashboard,
    # and the two would disagree with nothing to show why.
    #
    # Marks and meetings are exempt. A mark is an explicit declaration and a
    # work meeting is a scheduled commitment; a prompt fired off on the other
    # machine contradicts neither, so their spans are cut out of the holes
    # rather than the other way around.
    protected = [[m["start"] * 60, min(m["end"] * 60, now_s)]
                 for m in marks_for(day) if m["start"] * 60 < now_s]
    protected += [[m["start"] * 60, min(m["end"] * 60, now_s)]
                  for m in (meetings or [])
                  if m.get("counts", True) and m["start"] * 60 < now_s]
    merged = subtract_spans(
        merged,
        subtract_spans(
            desktop_holes([t.hour * 3600 + t.minute * 60
                           for t in desktop_prompts_for(day)],
                          {s // 60 for s in stamps}, tl),
            protected))

    # Cut out the stretches nobody touched the machine, on the same terms and
    # for the same reason: silence with positive evidence behind it is not a
    # gap to be absorbed. Without this a two-minute absence between two prompts
    # sat inside the period they formed and was counted in full -- the period
    # is built from its first and last event, and nothing in between was ever
    # asked whether somebody was there for it.
    #
    # Held behind IDLE_SUBTRACTS, currently off, so idle_cut returns nothing:
    # the cut was wrong far more often than it was right, and a wrong cut
    # removes minutes that were genuinely worked. The absence is still measured
    # and still shown on the timeline -- it just no longer changes the total.
    merged = subtract_spans(merged, idle_cut(day, protected))

    # Back to minutes for publication. Rounding the boundaries rather than the
    # durations keeps work and gaps tiling exactly: every gap still starts where
    # the period before it ends.
    #
    # The seconds-resolution bounds are kept alongside, because membership has
    # to be tested against them. Rounding a period's start up to 10:33 while
    # flooring its own 10:32:40 prompt to 10:32 put the prompt outside the
    # period it created, and the period reported zero prompts.
    # Floor, not round(). Python rounds halves to even, so a period running
    # 13:05:30 to 13:06:30 had both ends round to 786 and a full minute of work
    # published as zero. Flooring both ends can never collapse a span, because
    # floor(x) and floor(x + 1) always differ by exactly one.
    merged_sec = merged
    merged = [[s // 60, e // 60] for s, e in merged_sec]

    # Gaps are holes strictly BETWEEN the first and last work of the day.
    # The stretch after the last one is not a gap: the day is simply still
    # open, and whether it becomes a gap depends on whether work resumes --
    # which is unknowable now and settles itself on a later run.
    gaps = [{"start": a[1] // 60, "end": b[0] // 60,
             "start_sec": a[1], "end_sec": b[0], "len_sec": b[0] - a[1],
             "open": False}
            for a, b in zip(merged_sec, merged_sec[1:]) if b[0] > a[1]]

    # The merged spans ARE the work periods -- the same intervals the gaps are
    # the complement of, so the two lists always tile the day exactly and can
    # be shown side by side without one contradicting the other.
    slack_rows = slack_for(day)
    mark_rows = marks_for(day)
    worked = []
    for i, (a, b) in enumerate(merged):
        lo, hi = merged_sec[i]
        inside = [s // 60 for s in prompt_stamps if lo <= s <= hi]
        sl = [r for r in slack_rows if lo <= sec_of(r["t"]) <= hi]
        # A period held together by Slack alone has no prompts for the
        # summarizer to read, so it would publish an empty description. Naming
        # the conversations is both cheaper and more use than an LLM line.
        mk = [m for m in mark_rows if min(m["end"], b) - max(m["start"], a) > 0]
        what = summarize_span(day, a, b) if i < len(merged) - 1 else ""
        # A marked stretch has a reason attached and no prompts to summarize,
        # so the note the human wrote is the best description available.
        if not what and mk:
            what = next((m["note"] for m in mk if m["note"]), "marked as working")
        if not what and sl:
            names = []
            for r in sl:
                n = "DM" if r["im"] else f"#{r['ch']}"
                if n not in names:
                    names.append(n)
            what = "Slack: " + ", ".join(names[:4])
        # Last, because it is the weakest description available: the channel
        # names above say what the stretch was ABOUT, while this says only
        # which window it happened in. It exists for the stretch that has
        # neither prompts nor sends -- reading rather than writing -- which
        # nothing could describe at all before focus was recorded.
        if not what:
            apps = focus_apps(day, lo, hi)
            if apps:
                what = ", ".join(apps[:3])
        worked.append({
            "start": a, "end": b,
            "start_sec": lo, "end_sec": hi, "len_sec": hi - lo,
            # Every Slack send inside the period, so the tooltip can show what
            # was actually said in a stretch with no prompts in it at all.
            "slack": sl,
            "n_slack": len(sl),
            # Manually declared work overlapping this period, so the dashboard
            # can show which minutes rest on a human's word rather than on a
            # recorded event.
            "marks": mk,
            # The last span is still growing, so its summary would go stale the
            # moment it is written; leave it blank rather than describe a
            # period by its opening minutes.
            "what": what,
            # Every prompt stamp inside the period, so the drill-down can draw
            # density without recomputing the model from a second source.
            "prompts": inside,
            "n_prompts": len(inside),
            # The same prompts again, grouped by conversation and carrying
            # their text, for the tooltip.
            "sessions": sessions_in(day, a, b),
            # A meeting the calendar placed here explains why the period holds
            # together across a stretch with no prompts in it.
            "meetings": [{"start": m["start"], "end": m["end"],
                          "title": m.get("title", ""),
                          "calendar": m.get("calendar"),
                          "counts": m.get("counts", True)}
                         for m in (meetings or [])
                         if min(m["end"], b) - max(m["start"], a) > 0],
        })

    # Every run of `check` already appends its verdict here, so the probe's own
    # decisions are on disk -- they were simply never published, because the
    # snapshot kept only rows a human had been asked about and nothing is asked
    # any more. Publishing them lets the dashboard audit what the probe DECIDED.
    # Re-deriving the verdict in the widget instead would agree with the model
    # by construction and could not catch it misbehaving.
    checks = []
    labels = []
    if os.path.exists(LABELS):
        for line in open(LABELS):
            if not line.strip():
                continue
            r = json.loads(line)
            if not r["window_start"].startswith(day):
                continue
            src = r.get("sources") or {}
            checks.append({
                "start": r["window_start"][11:16],
                "end": r["window_end"][11:16],
                "guess": r["guess"],
                "detail": r.get("detail", ""),
                "prompts": src.get("prompts"),
                "meeting": src.get("meeting"),
                # None means the calendar could not be reached on that run --
                # a different thing from an empty calendar, and the reason a
                # verdict may look wrong without the model being wrong.
                "calendar": src.get("calendar"),
            })
            if not r.get("asked"):
                continue
            labels.append({
                "start": r["window_start"][11:16],
                "end": r["window_end"][11:16],
                "guess": r["guess"],
                "resolution": r.get("resolution"),
                "truth": r.get("truth"),
                "corroborated": r.get("corroborated"),
                "segments": r.get("segments"),
                "note": r.get("note"),
            })

    # Attach each gap to the ping that covered it, so the timeline can show
    # which absences were actually adjudicated and which passed unexamined --
    # the unexamined ones are recorded as not-working on no evidence at all.
    # Human-supplied reasons, as (start, end, text) in minutes. Matched to
    # periods by TIME OVERLAP rather than by an id: an explanation is a
    # statement about a stretch of the clock, and the period it lands in can
    # change underneath it. Both of the first two reasons said "meeting", which
    # is precisely what made the calendar reclassify those stretches as work --
    # keyed on gap identity they would have been orphaned by the very
    # correction they caused.
    said = []
    for l in labels:
        for seg in (l.get("segments") or []):
            if not seg.get("reason"):
                continue
            a = int(seg["start"][:2]) * 60 + int(seg["start"][3:])
            b = int(seg["end"][:2]) * 60 + int(seg["end"][3:])
            said.append((a, b, seg["reason"]))

    def reason_for(lo, hi):
        """The human's words, only when they account for most of the period.

        Measured against the PERIOD's length, not the note's. Against the
        shorter of the two, a twelve-minute "meeting" cleared the bar for a
        153-minute stretch and replaced its whole description -- the label was
        true of 8% of the period and wrong about the rest.
        """
        best, cover = None, 0
        for a, b, txt in said:
            ov = min(b, hi) - max(a, lo)
            if ov > cover and ov >= (hi - lo) / 2:
                best, cover = txt, ov
        return best

    def notes_in(lo, hi):
        """Hand-entered notes falling inside a period, with their own bounds.

        A note too small to describe the period still says something true about
        part of it, so it is shown alongside the generated line rather than
        discarded -- with its own bounds, so it never appears to be a claim
        about the whole stretch. Bounds stay separate from the text so the
        widget can lay them out in the same columns as the parent row.
        """
        out = []
        for a, b, txt in said:
            if min(b, hi) - max(a, lo) > 0 and (b - a) < (hi - lo) / 2:
                out.append({"start": a, "end": b, "len": b - a, "text": txt})
        return out

    for g in gaps:
        g["reason"] = reason_for(g["start"], g["end"])
        g["asked"] = next(
            (l for l in labels
             if g["start"] <= (int(l["end"][:2]) * 60 + int(l["end"][3:])) <= g["end"] + 2),
            None)

    # A period the human explained shows their words, not a generated line --
    # "meeting" is a better account of 12:33-12:45 than anything inferred from
    # the prompts around it, and hiding it would lose the only hand-entered
    # data in the file.
    for w in worked:
        r = reason_for(w["start"], w["end"])
        if r:
            w["said"] = r
        n = notes_in(w["start"], w["end"])
        if n:
            w["notes"] = n

    snap = {
        "date": day,
        "updated": now_local().strftime("%H:%M"),
        # The signature of every input these periods were derived from, so a
        # reader can tell whether the file still describes the present without
        # re-deriving it. status() uses this to notice that work has happened
        # since the last check() and rebuild, which is what stops the menu bar
        # from showing a period list up to a cron interval out of date. Taken
        # before the derivation, never after -- see the docstring.
        "fp": fp,
        "gaps": gaps,
        "worked": worked,
        "gap_sec": sum(g["len_sec"] for g in gaps),
        "day_start": merged[0][0] if merged else None,
        "day_end": merged[-1][1] if merged else None,
        "gaps_asked": sum(1 for g in gaps if g["asked"]),
        # The mode these periods were derived under. Not decoration: with the
        # unfocused ramp in play the same prompt stream produces two quite
        # different days, and a reader looking at a shredded afternoon needs to
        # be able to tell "I was half-attending" from "the tracker broke".
        "mode": mode_now(),
        "calendar_available": calendar_events(day) is not None,
        "labels": labels,
        "gap_after_min": GAP_AFTER,
        # A period ends at its last prompt, so this is the time actually spent
        # prompting and nothing more. It is a floor on the working day, not the
        # working day: reading a response is real work that leaves no stamp.
        "work_sec": sum(w["len_sec"] for w in worked),
        # `events` is prompts and attended foreground minutes together, so it is
        # not the prompt count and must not be published as one -- the
        # dashboard's "N prompts" would otherwise silently start counting focus.
        "prompts": sum(w["n_prompts"] for w in worked),
        "slack_messages": len(slack_rows),
        # Attended minutes at the front of a work app. Published beside the
        # others so a day that reads long can be traced to the signal that made
        # it long, which for anyone used to the send-only numbers this now is.
        "focus_minutes": len(focus_for(day)),
        "approvals": len(approvals_for(day)),
        "events": len(events),
        # Newest last, matching the order they were written.
        "checks": checks,
    }
    # Failures are raised, not swallowed. This used to `except OSError: pass`
    # on the theory that an unreachable vault should not stop the probe -- but
    # the probe's whole job is to write this file, so "carried on without it"
    # is indistinguishable from working. Running under a sandbox that denies
    # the vault, four consecutive runs printed a verdict and wrote nothing; the
    # dashboard sat an hour stale with no error anywhere to explain it.
    os.makedirs(VAULT_SNAPSHOT_DIR, exist_ok=True)
    path = snapshot_path(day)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(snap, fh, indent=1)
    os.replace(tmp, path)

    # Same conclusions in markdown, because the .json above never leaves this
    # machine. Written with the same raise-don't-swallow rule: a sidecar that
    # silently stopped updating is indistinguishable from a quiet day.
    md_path = markdown_snapshot_path(day)
    md_tmp = f"{md_path}.{os.getpid()}.tmp"
    with open(md_tmp, "w") as fh:
        fh.write(render_markdown_snapshot(day, snap))
    os.replace(md_tmp, md_path)


def render_markdown_snapshot(day: str, snap: dict) -> str:
    """The snapshot's conclusions as markdown, so Obsidian Sync will carry them.

    The .json beside this file does not leave the machine that wrote it --
    Obsidian Sync ships .md by default and skips everything else, which is the
    same reason CAL_FILE is markdown. Enabling "Sync all other file types"
    would also work, but it turns on syncing for every non-markdown file in the
    vault, against a 1 GB quota, to move one small file.

    Deliberately omits prompt text and session labels. `sessions` carries what
    was actually typed, and this file syncs to every device and sits in a vault
    that gets shared and backed up; times and counts answer "when was I
    working" without publishing the content of the work. Add them later behind
    the same redaction config activity-export uses, not by widening this.
    """
    worked = snap.get("worked", [])
    gaps = snap.get("gaps", [])

    def hhmm(m):
        return f"{m // 60:02d}:{m % 60:02d}"

    total = sum(w["len_sec"] for w in worked) // 60
    lines = [
        "---",
        f"generated: {now_local().isoformat(timespec='seconds')}",
        "---",
        f"# Worktime — {day}",
        "",
        f"{len(worked)} work {'period' if len(worked) == 1 else 'periods'}, "
        f"{total // 60}h{total % 60:02d}m total.",
        "",
        "Written by worktime-probe.py. Read-only — edits are overwritten on the",
        "next run. Prompt text is deliberately not published here.",
        "",
        "| Start | End | Secs | Prompts | Slack | Marked | Meeting | What |",
        "|-------|-----|------|---------|-------|--------|---------|------|",
    ]
    for w in worked:
        meeting = "; ".join(
            m.get("title") or "(busy)" for m in w.get("meetings", [])
            if m.get("counts", True)) or ""
        marked = "yes" if w.get("marks") else ""
        what = (w.get("what") or "").replace("|", "\\|")
        lines.append(
            f"| {hhmm(w['start'])} | {hhmm(w['end'])} | {w['len_sec']} | "
            f"{w.get('n_prompts', 0)} | {w.get('n_slack', 0)} | {marked} | "
            f"{meeting.replace('|', '/')} | {what} |")
    if gaps:
        lines += ["", "## Gaps", "",
                  "| Start | End | Secs |", "|-------|-----|------|"]
        for g in gaps:
            lines.append(f"| {hhmm(g['start'])} | {hhmm(g['end'])} | {g['len_sec']} |")
    return "\n".join(lines) + "\n"


def markdown_snapshot_path(day: str) -> str:
    return os.path.join(VAULT_SNAPSHOT_DIR, f"{day}.md")


def closed_gap(events: list[datetime], now: datetime):
    """The absence that just ended, if it ended recently and was never asked.

    Returns (start, end, minutes) or None. "Just ended" means work resumed
    after a quiet run of at least QUIET_ASK_AFTER, recently enough to still be
    worth asking about; the check runs on a 20-minute cadence, so the
    resumption is allowed to be up to one cycle old and still count.

    The search walks BACK through the prompt list rather than looking only at
    the last pair. Looking at the last pair alone meant the question could only
    be asked in the window between the first prompt back and the second: send
    three in quick succession and the final pair is seconds apart, the gap sits
    further back, and the absence is never asked about at all. Coming back from
    a break and immediately typing several things is the normal way to return,
    so that missed almost every real gap.

    Already-asked gaps are matched by their END time, which is the moment work
    resumed and is stable across runs -- unlike the check window, which moves
    every cycle and would let one absence be asked about again and again.
    """
    if len(events) < 2:
        return None
    asked = set()
    if os.path.exists(LABELS):
        for line in open(LABELS):
            if line.strip():
                if end := json.loads(line).get("gap_end"):
                    asked.add(end)
    for i in range(len(events) - 1, 0, -1):
        prev, last = events[i - 1], events[i]
        quiet = (last - prev).total_seconds() / 60
        if quiet < QUIET_ASK_AFTER:
            continue
        # The most recent qualifying gap is the only one in play. If its
        # resumption is already stale, an older one is staler still.
        if (now - last).total_seconds() / 60 > 25:
            return None
        if last.strftime("%H:%M") in asked:
            return None              # this absence already got its question
        return prev, last, quiet
    return None


def check() -> None:
    now = now_local()
    refresh_calendar_if_stale(now)
    events = events_for(now.strftime("%Y-%m-%d"))
    resolved = resolve_pending(now, events)

    since = read_cursor() or (now - timedelta(minutes=30))
    window = [e for e in events if since < e <= now]
    span_min = (now - since).total_seconds() / 60

    # Presence of a prompt is not enough -- what matters is whether the run is
    # still live right now. Checking only "did the window contain a prompt"
    # made the verdict a function of window length: when the probe went four
    # hours without running, two stray prompts across that span were labelled
    # WORKING at 1/hr, when almost all of it was a gap. Recency is independent
    # of how long the window happens to be.
    cutoff_min = live_cutoff(
        now.strftime("%Y-%m-%d"),
        [e.hour * 3600 + e.minute * 60 + e.second for e in events]) / 60
    if window and (now - window[-1]).total_seconds() / 60 <= cutoff_min:
        last_gap = (now - window[-1]).total_seconds() / 60
        verdict = "working"
        # Prompts/hour is the intensity axis: p50 is 36/hr, p90 is 84/hr over
        # the last three weeks, so the same "working" verdict covers a 10x
        # range. Supervising an agent and hands-on iteration are not the same
        # activity and the label file should be able to tell them apart.
        rate = len(window) / (span_min / 60) if span_min else 0
        detail = f"{len(window)} prompts, {rate:.0f}/hr"
        ask = False
    else:
        last = events[-1] if events else None
        quiet = (now - last).total_seconds() / 60 if last else span_min
        verdict = "not_working"
        detail = f"quiet {quiet:.0f}m"
        ask = False

    # A gap that has closed is recorded, but never asked about. The dialog is
    # gone: it interrupted to collect an explanation that the desktop's own
    # record of the period already contains, and it was wrong often enough --
    # about the length of the absence and about when it ended -- that answering
    # it cost more than the label was worth. Gaps are labelled after the fact
    # from context, or by hand with the `label` command.
    gap = closed_gap(events, now) if window else None
    if gap:
        verdict = "not_working"
        detail = f"gap {gap[0].strftime('%H:%M')}-{gap[1].strftime('%H:%M')}, {gap[2]:.0f}m"

    # A manual mark outranks silence. It is the strongest evidence there is --
    # the human said so, in the moment -- and it was previously invisible here:
    # events_for() carries prompts, Slack and approvals but not marks, so a
    # marked stretch still came back "quiet 40m" and would have pinged to ask
    # about an absence the menu bar was simultaneously showing as blue.
    marks = marks_for(now.strftime("%Y-%m-%d"))

    def marked_at(when: datetime) -> dict | None:
        m_of = when.hour * 60 + when.minute
        return next((m for m in marks if m["start"] <= m_of <= m["end"]), None)

    mark = marked_at(gap[0] + (gap[1] - gap[0]) / 2) if gap else (
        marked_at(now) if verdict == "not_working" else None)
    if mark:
        verdict = "working"
        detail = f"marked: {mark['note'] or 'marked as working'}"

    # Calendar speaks only to a gap. When prompts are flowing the verdict is
    # already settled and a meeting overlapping them changes nothing.
    meetings = calendar_events(now.strftime("%Y-%m-%d"))
    meeting = None
    if meetings is not None and gap:
        mid = gap[0] + (gap[1] - gap[0]) / 2
        meeting = covered_by_meeting(mid, [m for m in meetings
                                           if m.get("counts", True)])
        if meeting:
            verdict = "working"
            detail = f"meeting: {meeting['title']}"

    rec = {
        "window_start": since.isoformat(),
        "window_end": now.isoformat(),
        "guess": verdict,
        "detail": detail,
        "gap_start": gap[0].strftime("%H:%M") if gap else None,
        "gap_end": gap[1].strftime("%H:%M") if gap else None,
        "sources": {
            "prompts": len(window),
            # None is load-bearing: it records that calendar could not be
            # reached, so a later fit can exclude the window instead of
            # reading a silent auth failure as a confirmed empty calendar.
            "calendar": None if meetings is None else len(meetings),
            "meeting": meeting["title"] if meeting else None,
            "mark": mark["note"] or "marked as working" if mark else None,
        },
        # Nothing is ever asked now, so every row is the model's unaided guess.
        # Kept rather than dropped so rows written while the dialog existed
        # stay distinguishable from rows written after it.
        "asked": False,
        "resolution": "not_asked",
    }

    write_cursor(now)
    append_label(rec)
    write_vault_snapshot(now.strftime("%Y-%m-%d"), events)

    hhmm = lambda d: d.strftime("%H:%M")
    print(json.dumps({
        "verdict": verdict,
        "detail": detail,
        "window": f"{hhmm(since)}-{hhmm(now)}",
        "resolved_prior": resolved,
    }))


def label(spec: str, note: str = "") -> None:
    """Record a human correction against the most recent checked window.

    `spec` is one or more segments, comma-separated:

        working                          whole window
        12:33-12:45=working:meeting      one segment, with a reason
        12:33-12:45=working:meeting,12:45-12:54=not_working

    Segments exist because the first real correction was a partial one -- half
    the window was a meeting and half was genuinely idle. Collapsing that to a
    single verdict would teach the fit the wrong boundary, and "a meeting ran
    into the quiet period" is the single most common way prompt-only detection
    is wrong, not an edge case. A window with no segments is stored as one
    segment spanning the whole thing, so both shapes read back the same.

    Targets the last row in the file, not the last one with asked=True: the
    confirmation dialog that "asked" described is gone, every row check() now
    writes carries asked=False, and gating on it meant a correction always
    landed on whatever fossil row last had asked=True -- silently rewriting
    stale history instead of the window the human just meant.
    """
    if not os.path.exists(LABELS):
        print("no labels yet"); return
    rows = [json.loads(l) for l in open(LABELS) if l.strip()]
    target = rows[-1] if rows else None
    if not target:
        print("no window to correct"); return

    segs = []
    for part in spec.split(","):
        part = part.strip()
        if "=" in part:
            span, rest = part.split("=", 1)
            state, _, reason = rest.partition(":")
            start, _, end = span.partition("-")
            segs.append({"start": start.strip(), "end": end.strip(),
                         "state": state.strip(), "reason": reason.strip() or None})
        else:
            # Bare form still carries an optional reason -- "working:meeting"
            # must mean the same thing here as it does inside a span, or the
            # shorthand silently buries the reason in the state field.
            state, _, reason = part.partition(":")
            segs.append({"start": target["window_start"][11:16],
                         "end": target["window_end"][11:16],
                         "state": state.strip(), "reason": reason.strip() or None})

    target["resolution"] = "corrected"
    target["segments"] = segs
    # Kept for the simple case so `report` and any downstream fit can read a
    # single verdict without unpacking segments; null when the window is mixed.
    states = {s["state"] for s in segs}
    target["truth"] = segs[0]["state"] if len(states) == 1 else None
    target["mixed"] = len(states) > 1
    target["note"] = note
    target["resolved_at"] = now_local().isoformat()

    tmp = f"{LABELS}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, LABELS)
    print(json.dumps({"corrected": target["window_start"], "segments": segs}))


def report() -> None:
    if not os.path.exists(LABELS):
        print("no labels yet"); return
    rows = [json.loads(l) for l in open(LABELS) if l.strip()]
    by = {}
    for r in rows:
        by[r.get("resolution", "?")] = by.get(r.get("resolution", "?"), 0) + 1
    usable = [r for r in rows if r.get("resolution") in ("accepted_silent", "corrected")]

    # Score by minutes, not by window. A corrected window is rarely wrong end to
    # end -- the first real one was a meeting for 12 of its 20 minutes and idle
    # for the rest -- so counting it as a whole miss understates the model badly
    # and makes "accuracy" swing on how often a window happens to straddle a
    # boundary rather than on whether the threshold is any good.
    def to_min(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    right = total = 0
    for r in usable:
        w0, w1 = to_min(r["window_start"][11:16]), to_min(r["window_end"][11:16])
        span = max(w1 - w0, 1)
        if r.get("resolution") == "accepted_silent":
            right += span; total += span
            continue
        for s in r.get("segments", []):
            seg = max(min(to_min(s["end"]), w1) - max(to_min(s["start"]), w0), 0)
            total += seg
            if s["state"] == r["guess"]:
                right += seg

    corroborated = sum(1 for r in usable if r.get("corroborated"))
    print(json.dumps({
        "total_windows": len(rows),
        "by_resolution": by,
        "usable_labels": len(usable),
        "labels_corroborated": corroborated,
        "windows_corrected": sum(1 for r in usable if r.get("resolution") == "corrected"),
        "minutes_scored": total,
        "minutes_correct": right,
        "accuracy": f"{100*right/total:.0f}%" if total else "n/a",
        "gap_after_min": GAP_AFTER,
    }, indent=2))


STATUS_CACHE = os.path.join(STATE, "status-cache.json")

# Shortest gap between two full re-derivations of the live verdict. Sets the
# ceiling on how stale the dot's underlying data can be, independent of how
# often the menu bar polls.
MIN_RECOMPUTE_SEC = 10

# Bumped whenever the cached payload gains a field. A cache written by the
# previous build is missing the new key entirely, and the alternative to
# versioning is a .get() default on every read -- which would quietly serve an
# empty activity list as though the day had none. A version mismatch is simply
# a miss, handled by the path that already exists for a stale day.
STATUS_CACHE_V = 6


# One file, overwritten by bin/worktime-prompt-mark.py on every prompt in every
# profile. Stat'ing it is the whole of the freshness check that used to walk
# 3,139 transcripts and directories -- see activity_fingerprint.
PROMPT_MARK = os.path.join(STATE, "prompt-mark.json")


def profiles_missing_mark_hook() -> list[str]:
    """Profile roots whose settings.json does not fire the prompt-mark hook.

    The mark is shared, so a profile that stops firing it does not break
    anything visibly -- the file keeps moving, updated by the other profiles,
    and that profile's prompts simply stop counting as presence. That is the
    one failure the old walk could not have had, since it read the transcripts
    directly, so it is the one this design has to answer for.
    """
    missing = []
    for root in PROMPT_ROOTS:
        settings = os.path.join(os.path.dirname(root), "settings.json")
        try:
            with open(settings) as fh:
                groups = json.load(fh).get("hooks", {}).get(
                    "UserPromptSubmit", [])
        except (OSError, ValueError):
            groups = []
        wired = any("worktime-prompt-mark" in h.get("command", "")
                    for g in groups for h in g.get("hooks", []))
        if not wired:
            missing.append(os.path.dirname(root))
    return missing


def prompt_mark_stamp() -> str:
    """The prompts half of the fingerprint, as one stat.

    An absent mark is a legitimate state -- no prompt since the file was last
    cleared -- but an unhooked profile is a configuration bug that would show
    up as a person who quietly stopped working, so it stops the probe instead.
    """
    missing = profiles_missing_mark_hook()
    if missing:
        raise RuntimeError(
            "UserPromptSubmit hook worktime-prompt-mark.py is not registered "
            "for: " + ", ".join(missing) + " -- prompts from those profiles "
            "would not count. See the Install section in README.md.")
    try:
        st = os.stat(PROMPT_MARK)
    except OSError:
        return f"{PROMPT_MARK}:absent"
    return f"{PROMPT_MARK}:{st.st_mtime_ns}:{st.st_size}"


def activity_fingerprint(day: str) -> str:
    """A signature of every input the live verdict is derived from.

    One stat per input, and prompts arrive as a single one. This used to walk
    every transcript on the machine -- 3,139 files and directories across the
    profile roots -- and hash their mtimes, which costs ~40ms with the
    filesystem metadata in cache and upwards of 25 seconds without it. Under
    memory pressure the cache is evicted between polls, so a 5-second poll was
    spending half a minute in uninterruptible disk wait and dying to the menu
    bar's 30s watchdog. Two thirds of those files had not changed in a week.

    So prompts are pushed rather than polled: bin/worktime-prompt-mark.py runs
    on Claude Code's UserPromptSubmit and overwrites PROMPT_MARK, and the walk
    collapses into that one path. This is exact, not an approximation of the
    walk -- there is no scan window for a prompt to land inside, and a resumed
    old session is caught like any other, which a cheaper walk pruned by
    directory mtime could not manage (appending to a transcript does not
    change its directory's mtime).

    It also recomputes less. The walk changed on every transcript write, which
    an active session causes about six times a minute; only the user's prompts
    are evidence anyone is at the desk, and those are what the mark records.

    Slack is the exception and has to be handled by time rather than by file.
    A message sent from the phone changes nothing on this disk, so the
    fingerprint alone would never notice it and the cache would go stale
    forever. Folding the TTL bucket in forces a genuine recompute every
    SLACK_TTL_SEC, which is the refresh cadence Slack already had.

    Since sends stopped counting as presence that bucket no longer moves the
    dot -- it now only keeps the tooltip's message list current, which is
    still worth a recompute every four minutes. The focus log is an ordinary
    file and needs no such trick.
    """
    parts = [prompt_mark_stamp()]
    # Chrome's History is in here so a PR opened in the browser moves the dot
    # without waiting for something else to happen. It is the one input that
    # changes on its own while the person is doing nothing this probe can
    # otherwise see, which is the whole reason the live read exists.
    for p in (MARKS, APPROVALS, NOTES, MODEFILE, CAL_FILE, IDLE_CLAIMS,
              os.path.join(SLACK_DIR, f"{day}.json"),
              os.path.join(FOCUS_DIR, f"{day}.jsonl"),
              chrome_history_path() or "chrome-history-absent",
              os.path.join(ACTIVITY_DIR, f"{day}.md")):
        try:
            st = os.stat(p)
            parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{p}:absent")
    if day == now_local().strftime("%Y-%m-%d"):
        parts.append(f"slack-window:{int(time.time()) // SLACK_TTL_SEC}")
    return hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()


def live_activity(day: str) -> tuple[datetime | None, list[int], list[int],
                                     list[dict]]:
    """Last event, mark-closing stamps, event stamps and the recent list,
    memoised on the fingerprint.

    The two stamp lists answer two different questions and must not be
    conflated: `stamps` is prompts and focus, exactly what closes an open mark,
    while `ev_stamps` is every event, which is the run of work the unfocused
    ramp measures.

    Returns only the things status() needs. Everything time-dependent -- how
    long the silence has run, whether a mark is still open right now -- is
    recomputed by the caller from these, so a cache hit still produces a
    verdict that moves with the clock.

    The activity list rides along here rather than being built in status()
    because it is a function of exactly the same inputs as `last`: it can only
    change when the fingerprint does, so caching it beside them means a poll
    during genuine silence still costs nothing but a walk of stat() calls.

    What is cached is the whole day's rows, not the ten the menu lists. Both
    views the menu can show are cut from them: the raw list is the top ten, and
    the sessions are the same rows grouped -- grouped in status() rather than
    here, because that grouping reads the snapshot and status() is where the
    snapshot is brought up to date.
    """
    try:
        c = json.load(open(STATUS_CACHE))
    except (OSError, ValueError):
        c = {}
    hit = c.get("day") == day and c.get("v") == STATUS_CACHE_V

    # The floor is what makes this affordable during real work, and the
    # fingerprint is what makes it responsive during quiet. An active Claude
    # session appends to its transcript every few seconds, so the fingerprint
    # alone misses on nearly every poll and a 5s dot would re-derive the whole
    # day ~12x a minute. Rate-limiting the expensive path bounds that, at the
    # cost of the verdict trailing reality by at most MIN_RECOMPUTE_SEC --
    # which is invisible, because a stretch of work in progress is already
    # green and stays green.
    if hit and time.time() - c.get("at", 0) < MIN_RECOMPUTE_SEC:
        return ((datetime.fromisoformat(c["last"]) if c["last"] else None),
                c["stamps"], c["ev_stamps"], c["acts"])

    fp = activity_fingerprint(day)
    if hit and c.get("fp") == fp:
        last = datetime.fromisoformat(c["last"]) if c["last"] else None
        return last, c["stamps"], c["ev_stamps"], c["acts"]

    events = events_for(day)
    # Prompts and focus only, matching what marks_for() closes an open mark on.
    # Approvals are deliberately excluded there and must stay excluded here.
    stamps = sorted([t.hour * 60 + t.minute for t in prompts_for(day)]
                    + [t.hour * 60 + t.minute for t in focus_for(day)])
    # Every event, which is a different question and belongs to the ramp.
    # `last` is drawn from `events`, so the bout the unfocused cutoff is
    # measured over has to be drawn from `events` too. Measuring the silence
    # against one set and the bout that earns the cutoff against a narrower one
    # is what made a browsing-only minute read as "4m since last activity, 2m
    # left of 5m": the browse refreshed the clock, could not open a bout of its
    # own, and the ramp went on quoting the width an hour-old prompt run had
    # earned. check() has always chained the full event list here; this is
    # status() being brought into line with it.
    ev_stamps = sorted({t.hour * 60 + t.minute for t in events})
    last = events[-1] if events else None
    acts = activity_rows(day)
    # Per-pid, because this is now written by whichever process polls first and
    # there are several: the menu bar every 5s, the cron check, and any CLI run.
    # A shared fixed name meant two of them raced on the same path -- the first
    # to finish renamed it away and the second died on os.replace with
    # FileNotFoundError, which surfaced as the dot dropping out mid-poll.
    tmp = f"{STATUS_CACHE}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"v": STATUS_CACHE_V, "fp": fp, "day": day, "stamps": stamps,
                   "ev_stamps": ev_stamps,
                   "at": time.time(), "acts": acts,
                   "last": last.isoformat() if last else None}, fh)
    os.replace(tmp, STATUS_CACHE)
    return last, stamps, ev_stamps, acts


def status() -> dict:
    """Current state, cheap enough for a menu bar to poll every minute.

    Deliberately NOT a call to check(): check() advances the cursor and appends
    a label row, so polling it from the menu bar would corrupt the very record
    the dashboard audits. This only reads.
    """
    now = now_local()
    # Here and not only in check(), because this is the call that actually
    # runs: the bar polls `status` every minute and nothing schedules `check`
    # any more. With the trigger in check() alone the refresh never fired
    # outside a hand-run probe, and the dump went stale again by mid-afternoon.
    # It writes the calendar dump, not the cursor or the label log, so this
    # stays read-only with respect to the record check() owns.
    refresh_calendar_if_stale(now)
    day = now.strftime("%Y-%m-%d")
    now_m = now.hour * 60 + now.minute
    last, stamps, ev_stamps, all_acts = live_activity(day)

    # Folded in here and nowhere else, for the reason given on
    # last_focus_input(): live_activity()'s `last` comes from focus_for(),
    # which floors to the minute and needs a closing sample, so it can report
    # ~90s of quiet while somebody is actively typing in Slack. This is the
    # same shape as the raw prompt timestamps it sits beside -- a point act at
    # second resolution -- and like them it moves only the dot.
    touched = last_focus_input(day)

    # Sends are not in events_for() -- focus superseded them -- but the live
    # dot is the one consumer focus cannot fully serve: a message typed into a
    # window that has been frontmost for a while is a keystroke like any other,
    # while a send made in the seconds after switching apps can land between
    # focus samples. Live only, for the same reason as everything else here.
    sent = last_slack_send(day)
    if sent and (touched is None or sent > touched):
        touched = sent

    if touched and (last is None or touched > last):
        last = touched
        # Into the event stamps, not the mark-closing ones: this moved `last`,
        # so it has to move the bout the cutoff is measured over, but a Slack
        # send is not one of the two signals marks_for() closes an open mark on.
        t_m = touched.hour * 60 + touched.minute
        if t_m not in ev_stamps:
            ev_stamps = sorted(ev_stamps + [t_m])

    quiet_sec = (now - last).total_seconds() if last else None
    quiet = quiet_sec / 60 if quiet_sec is not None else None

    # What the run in progress has earned, not the flat GAP_AFTER: in unfocused
    # mode a lone prompt lapses after a minute and only a session that has been
    # going a while holds the dot green for the full five.
    cutoff_sec = live_cutoff(day, [m * 60 for m in ev_stamps])

    open_mark = next((m for m in marks_for(day, stamps)
                      if m["open"] and m["start"] <= now_m <= m["end"]), None)
    ends = read_session_ends(day)
    ended_at = max(ends) if ends else None
    ended_ts = read_session_end_ts(day, ended_at) if ended_at is not None else None
    in_meeting = covered_by_meeting(now, [
        m for m in (calendar_events(day) or [])
        if m.get("counts", True)])
    if open_mark:
        state, why = "marked", open_mark["note"] or "marked as working"
    elif in_meeting:
        state, why = "working", f"in {in_meeting.get('title') or 'meeting'}"
    elif ended_at is not None and (last is None or (
            last.timestamp() <= ended_ts if ended_ts is not None
            else last.hour * 60 + last.minute <= ended_at)):
        # Below the mark and the meeting, above the cutoff. A declaration ends
        # the run it was made in, but it is not a lock on the rest of the day:
        # starting a new mark, or a meeting beginning, speaks for the minute it
        # covers, and prompting again is activity after the end and passes on
        # through to the cutoff below. What it does override is the run it
        # closed -- without this the dot stayed green for the whole of the
        # cutoff after the click, which is what made End Session look like a
        # button that did nothing.
        #
        # The comparison needs the click's exact second, not just its minute:
        # floored to the minute, ten seconds of Slack right after the click
        # landed in the same minute as end_min and read as "before or at" it,
        # so the dot stayed idle through activity that came after the
        # declaration -- the false negative this branch exists to avoid.
        state, why = "idle", f"session ended {hhmm_of(ended_at)}"
    elif quiet is not None and quiet * 60 <= cutoff_sec:
        state, why = "working", f"{quiet:.0f}m since last activity"
    else:
        state = "idle"
        why = f"quiet {quiet:.0f}m" if quiet is not None else "nothing today"

    # Whether the day's newest period is still running, which is what the menu
    # means by "now" and by the bold row and by "still being summarized". The
    # same lapse test the header above just applied, deliberately reused rather
    # than re-derived: the two halves of one menu describing the same moment
    # have to agree, and the way to guarantee that is to ask once. A mark or a
    # meeting holds the DOT green without any event behind it, so neither is
    # enough on its own to call a period of recorded work still open.
    live = quiet_sec is not None and quiet_sec <= cutoff_sec

    # The period list, the day total and the focus figure all come from the
    # snapshot, and until now only check() ever rewrote it -- so the header line
    # above (derived live, every poll) could read "0m since last activity" while
    # the newest period below it ended twenty minutes ago. The two halves of the
    # same menu disagreed, and the half that was right looked like the broken
    # one. Rebuilding here when the inputs have moved keeps them consistent.
    #
    # Gated on the fingerprint, not the clock: a poll during genuine silence
    # matches and costs a few stats, so idle polling is as cheap as it ever was.
    # This still does not call check() -- the cursor and the label log are its
    # alone, and this remains read-only with respect to both.
    worked_sec = 0
    periods = []
    sessions = []
    focus_pct = None
    link_from = None
    path = snapshot_path(day)
    # Taken before the events are read, and handed to the writer, so the
    # snapshot is signed with the state of the inputs its periods were actually
    # built from. Reversing those two is what let a rebuild publish a
    # fingerprint that already covered an event its periods did not.
    fp_now = activity_fingerprint(day)
    if not os.path.exists(path) or json.load(open(path)).get("fp") != fp_now:
        write_vault_snapshot(day, events_for(day), fp_now)
    if os.path.exists(path):
        snap = json.load(open(path))
        worked_sec = snap["work_sec"]
        worked = snap.get("worked", [])
        ds, de = snap.get("day_start"), snap.get("day_end")
        if ds is not None and de is not None and de > ds:
            focus_pct = round(100 * worked_sec / ((de - ds) * 60))
        # Newest first, so the menu bar can show the last 3 without slicing
        # from the wrong end. The last entry here is the day's most recent
        # period, open or closed -- whichever it is, it's what "recent
        # activity" means.
        for i, w in enumerate(worked):
            periods.append({
                "start": w["start"], "end": w["end"], "len_sec": w["len_sec"],
                "n_prompts": w.get("n_prompts", 0),
                "n_slack": w.get("n_slack", 0),
                "what": w.get("what", ""),
                "current": live and i == len(worked) - 1,
            })
        periods.reverse()
        # Grouped here, off the snapshot that was just brought up to date --
        # the whole point of the sessions view is that its divisions are the
        # period list's divisions, so it has to read the same copy of them.
        sessions = group_sessions(all_acts, worked, live=live)
        # Off the same periods the list above was drawn from, so the item can
        # only offer a minute the person can see on screen. None greys it out.
        link_from = link_anchor(worked, now_m, cutoff_sec,
                                open_mark["start"] if open_mark else None)

    # Read from the same snapshot the menu bar's period list came from, not
    # recomputed here -- this is the on-disk record of the last completed
    # `check()`, and this call is deliberately read-only (see docstring).
    return {"state": state, "why": why, "worked_sec": worked_sec,
            "at": now.strftime("%H:%M"),
            "quiet_since": last.strftime("%H:%M") if last else None,
            # Precise seconds, not the rounded "why" text -- the menu bar uses
            # this to blink the dot as a period nears GAP_AFTER, and a minute-
            # rounded value would make the blink window jump instead of count
            # down smoothly.
            "quiet_sec": int(quiet_sec) if quiet_sec is not None else None,
            # The live cutoff, so the blink counts down to the moment this
            # particular run actually lapses rather than to a fixed five
            # minutes it may never be granted.
            "gap_after_sec": cutoff_sec,
            "mode": mode_now(),
            "focus_pct": focus_pct,
            # The minute "Link with last session" would claim from, as HH:MM,
            # or null when there is nothing to link to. The menu greys the item
            # out on null rather than offering a click that does nothing, and
            # names the minute in the title so the claim is legible before it
            # is made rather than only afterwards in the period list.
            "link_from": hhmm_of(link_from) if link_from is not None else None,
            "periods": periods,
            # Newest first, same as `periods`. The periods say how the day was
            # divided up; this says what the divisions were made out of.
            "activities": all_acts[:ACTIVITY_LIST_N],
            # The same evidence, folded back into those divisions. The menu
            # shows one or the other, never both at once.
            "sessions": sessions,
            # Null while the desktop export is current. Shown as a quiet row
            # under the status line; it never changes `state`.
            "feed_notice": activity_feed_notice(now)}


def backfill(days: int) -> None:
    """Write a snapshot for each of the last N days the dashboard can page to.

    The arrows are only as useful as the history behind them, and prompt
    timestamps for past days are already on disk -- the snapshots simply were
    never written, because the probe only ever knew about today.
    """
    today = now_local().date()
    for i in range(days):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        events = events_for(day)
        if not events:
            print(f"{day}  --")
            continue
        write_vault_snapshot(day, events)
        snap = json.load(open(snapshot_path(day)))
        print(f"{day}  {len(snap['worked']):3d} periods  "
              f"{len(snap['gaps']):3d} gaps  "
              f"{snap['work_sec'] // 3600}h{snap['work_sec'] // 60 % 60:02d}m worked")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        check()
    elif cmd == "label":
        label(sys.argv[2] if len(sys.argv) > 2 else "working",
              " ".join(sys.argv[3:]))
    elif cmd == "report":
        report()
    elif cmd == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    elif cmd == "mark":
        # A leading HH:MM or HH:MM-HH:MM is a time; anything else is the note,
        # so `mark reading a PR` and `mark 06:55-07:00 reading a PR` both work.
        rest = sys.argv[2:]
        spec = ""
        if rest and re.fullmatch(r"\d{1,2}:\d{2}(-\d{1,2}:\d{2})?", rest[0]):
            spec, rest = rest[0], rest[1:]
        rec = add_mark(spec, " ".join(rest))
        day = rec["day"]
        write_vault_snapshot(day, events_for(day))
        span = next((m for m in marks_for(day) if m["start"] == rec["start"]), None)
        print(json.dumps({
            "marked": hhmm_of(rec["start"]),
            "until": hhmm_of(span["end"]) if span else None,
            "open": rec["end"] is None,
            "note": rec["note"],
        }))
    elif cmd == "unmark":
        # Bare `unmark` ends the shift now; `unmark last` ends it at the last
        # thing the tracker actually saw. See close_open_marks for which
        # caller wants which.
        day = now_local().strftime("%Y-%m-%d")
        events = events_for(day)
        when = None
        if len(sys.argv) > 2 and sys.argv[2] == "last":
            when = last_entry_end(events)
        closed = close_open_marks(when)
        write_vault_snapshot(day, events)
        print(json.dumps({
            "closed": [{"start": hhmm_of(r["start"]), "end": hhmm_of(r["end"]),
                        "note": r["note"]} for r in closed],
        }))
    elif cmd == "end_session":
        print(json.dumps(end_session(
            at_last=len(sys.argv) > 2 and sys.argv[2] == "last")))
    elif cmd == "link_last":
        print(json.dumps(link_last_session()))
    elif cmd == "mode":
        # Bare `mode` reads, `mode <name>` sets. Setting rebuilds the snapshot
        # so the dashboard and the menu redraw under the new rule at once
        # instead of at the next cron check.
        if len(sys.argv) > 2:
            set_mode(sys.argv[2])
            day = now_local().strftime("%Y-%m-%d")
            write_vault_snapshot(day, events_for(day))
        print(json.dumps({"mode": mode_now()}))
    elif cmd == "meeting_end":
        # Declare the current meeting over at this moment. The calendar file
        # won't update -- it reflects what was scheduled, not what happened --
        # so without this the dot stays green until the scheduled end time even
        # when the meeting finished early. Writes a cut record for today; any
        # meeting whose scheduled end is past this minute is treated as having
        # ended here instead.
        # Rebuilds the snapshot the way `mode` does: the cut changes the day
        # total as well as the dot, so leaving it until the next 20-minute
        # check would show a dashboard that still counts the part of the
        # meeting that did not happen.
        cut = now_local().hour * 60 + now_local().minute
        cuts = append_meeting_cut(cut)
        day = now_local().strftime("%Y-%m-%d")
        write_vault_snapshot(day, events_for(day))
        print(json.dumps({"cut_min": cut, "cuts": cuts,
                          "at": now_local().strftime("%H:%M")}))
    elif cmd == "note":
        # Rebuilds the snapshot the way `mark` and `mode` do. The entry is a
        # claim about the minute it was typed in, so waiting for the next
        # 20-minute check would leave the dashboard disagreeing with the menu
        # about a day the person just corrected by hand.
        rec = add_note(" ".join(sys.argv[2:]))
        write_vault_snapshot(rec["day"], events_for(rec["day"]))
        print(json.dumps({"at": rec["t"][:5], "text": rec["text"]}))
    elif cmd == "track":
        # `track 5` claims five minutes; `track 5 split` places whatever will
        # not fit behind the last session in front of it instead. A missing or
        # unreadable count is refused rather than defaulted: the number IS the
        # request here, and guessing one would bank minutes nobody asked for.
        try:
            n = int(sys.argv[2])
        except (IndexError, ValueError):
            print(json.dumps({"tracked": False, "why": "track needs a minute count"}))
        else:
            print(json.dumps(track_back(
                n, sys.argv[3] if len(sys.argv) > 3 else "clip")))
    elif cmd == "status":
        # One small line for the menu bar: what the tracker thinks right now.
        print(json.dumps(status()))
    else:
        print(__doc__)
