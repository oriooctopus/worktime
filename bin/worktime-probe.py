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
"""

from __future__ import annotations  # 3.8 can parse the annotations

import hashlib
import json
import os
import re
import subprocess
import sys
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
LOCAL = ZoneInfo("America/New_York")

# Run helpers under THIS interpreter, never a bare "python3" off PATH. The menu
# bar app is started by launchd, whose PATH is /usr/bin:/bin:/usr/sbin:/sbin,
# so "python3" there resolved to Apple's 3.9 -- which cannot import
# prompt-count.py at all (`str | None` in an annotation is a TypeError at def
# time). Every poll from the menu bar therefore saw zero prompts and reported a
# confident "idle" while the same command run from a shell said "working".
PYTHON = sys.executable

# This file itself is usually reached via the ~/.claude/bin/worktime-probe.py
# symlink (see README), so __file__ resolves through it correctly either way.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_COUNT = os.path.join(ROOT, "bin", "prompt-count.py")


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOCAL)

STATE = os.path.expanduser("~/.claude/stats/worktime")
LABELS = os.path.join(STATE, "labels.jsonl")
CURSOR = os.path.join(STATE, "cursor.json")

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
# focused prompt is eighty seconds. Small enough that it cannot inflate a day
# the way a five-minute credit did -- a hundred lone prompts buy just over two
# hours, and a hundred lone prompts is not a real day.
TAIL_SEC = 20

# And what a period gets BEFORE its first prompt. A prompt is not typed
# instantly -- a long one is a minute of work that left its stamp only at the
# moment it was sent, so crediting from the stamp alone loses the writing.
#
# Clamped where it would eat into a gap: see the note in write_vault_snapshot.
# Without that clamp a 5m20s silence would render as a 4m gap, which is the
# exact contradiction the old five-minute grace period produced -- a dashboard
# showing gaps shorter than the gap threshold it claims to use.
LEAD_SEC = 60

# Extra lead when a period opens on the first prompt of a conversation, on top
# of LEAD_SEC. Starting a conversation costs more than continuing one: there is
# a page to read and a question to frame before anything gets typed.
SESSION_LEAD_SEC = 20

# The least a period can measure, in either mode. A bout whose padding gets
# clamped away -- by a tight silence on both sides, or by an unfocused ramp that
# has not started yet -- still stands for something that really happened, and
# publishing it as 0m loses it entirely.
#
# One minute, not the thirty seconds it reads as: the snapshot publishes
# minute-of-day boundaries and derives every duration by subtracting them, so a
# thirty-second span floors to the same minute at both ends, renders "0m", and
# adds nothing at all to the day's total. A sixty-second floor is the smallest
# credit this resolution can actually carry -- floor(x) and floor(x + 60)
# always differ by exactly one -- so it is the floor the rule has to use.
MIN_PERIOD_SEC = 60

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


def build_bouts(stamps: list[int], firsts: set[int],
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
    full 80s of padding there would show a 4m gap; a hard floor on the start
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
        want = LEAD_SEC + (SESSION_LEAD_SEC if raw[i][0] in firsts else 0)
        # In unfocused mode the lead is earned on the same ramp as the cutoff.
        # A minute of writing credited before the first prompt is a fair reading
        # of a bout that turned into real work and a poor one for a prompt fired
        # off between other things -- and with the full lead always applied, a
        # lone unfocused prompt would still bank 80 seconds, so the mode would
        # hardly move the day's total however short its cutoff got.
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

    Marks and meetings are declared presence and join whatever abuts them,
    under the threshold the span they are joining has earned -- the same rule
    chain_bouts used, so the two cannot disagree.
    """
    merged: list[list[int]] = []
    for s, e in spans:
        thr = gap_sec_for(mode_at(timeline, s),
                          merged[-1][1] - merged[-1][0]) if merged else 0
        if merged and s - merged[-1][1] <= thr:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
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

    A remnant too short to publish a whole minute is dropped rather than
    emitted as a 0m period -- it is residue of a stretch already judged to be
    desktop time, and a zero-length period is exactly what MIN_PERIOD_SEC
    exists to keep out of the snapshot.
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
        out += [p for p in pieces if p[1] // 60 > p[0] // 60]
    return out


def live_cutoff(day: str, stamps_sec: list[int]) -> int:
    """The cutoff the run in progress has earned, for the live verdict.

    Uses mode_now() rather than the mode at the last stamp: switching to
    focused is a statement about what is happening at this moment, and the dot
    should answer to it immediately.
    """
    bouts, _ = chain_bouts(sorted(stamps_sec), mode_timeline(day))
    return gap_sec_for(mode_now(), bouts[-1][1] - bouts[-1][0] if bouts else 0)


def prompts_for(day: str) -> list[datetime]:
    """Merged, sorted prompt datetimes for one local calendar day.

    Merged across sessions on purpose: prompting session A and session B in the
    same ten minutes is one work period, not two. Per-session bouts would
    double-count it and invent gaps that never happened.
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
        return []
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
    return sorted(seen)


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
    while page <= pages and page <= 10:
        q = urllib.parse.urlencode({
            "query": f"from:@{SLACK_USER} after:{shift_day(day, -1)}"
                     f" before:{shift_day(day, 1)}",
            "count": 100, "page": page, "sort": "timestamp",
        })
        req = urllib.request.Request(
            "https://slack.com/api/search.messages?" + q,
            headers={"Authorization": f"Bearer {tok}"})
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
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
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, ValueError):
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


def sec_of(hms: str) -> int:
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


MARKS = os.path.join(STATE, "marks.jsonl")


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
    # Resolution needs the real events, and events_for() consults marks only
    # through the snapshot, never here -- so there is no cycle.
    if stamps is None:
        stamps = sorted(t.hour * 60 + t.minute for t in prompts_for(day))
        stamps += [t.hour * 60 + t.minute for t in focus_for(day)]
        stamps.sort()

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
            # The first real activity strictly after the mark closes it.
            after = [s for s in stamps if s > start]
            end = after[0] if after else (now_m if day == live else start)
            # ...but never longer than MARK_MAX_OPEN_MIN. Without a ceiling an
            # open mark credits an entire absence: one clicked at 14:00 and
            # forgotten ran until 15:30, silently adding 90 minutes, because
            # nothing typed in between could close it. That failure hides
            # itself -- the longer you are away, the more work it invents --
            # which is precisely what this tracker exists to catch. Expiring is
            # the safe direction: re-clicking costs a second, and the minutes
            # after the cap are still recoverable from prompts and Slack.
            end = min(end, start + MARK_MAX_OPEN_MIN)
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
    closed = []
    for r in rows:
        if r.get("day") == day and r.get("end") is None:
            r["end"] = max(end_m, r["start"])
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


def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def session_first_stamps(day: str) -> set[int]:
    """Seconds-of-day at which each conversation's first prompt was sent.

    Read from the same per-session `times` lists prompts_for() flattens, so the
    two can never disagree about which stamp opened a conversation.
    """
    out = set()
    for sess in full_day(day).get("sessions", []):
        times = sess.get("times") or []
        if times:
            out.add(sec_of(min(times)))
    return out


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

# The most time one sample may vouch for. The bar writes every
# FOCUS_HEARTBEAT_SEC (30s), so consecutive rows are normally 30s apart and
# anything materially longer means the log stopped -- sleep, lock, a crash, the
# app not running. Those minutes get credited to nobody, which is the whole
# point: the failure mode this avoids is one sample before lunch claiming the
# hour until the next one.
FOCUS_MAX_GAP_SEC = 90

# Foreground time that is not work, by bundle id. An exclude list rather than
# an allow list on purpose -- on a work machine nearly everything in the
# foreground is the job, and an allow list quietly loses a day's work every
# time a new tool enters the rotation, failing in the direction that looks like
# an ordinary quiet afternoon.
FOCUS_EXCLUDE = {
    "com.apple.TV",
    "com.apple.Music",
    "com.apple.Photos",
    "com.netflix.Netflix",
    "com.spotify.client",
    "com.valvesoftware.steam",
}


def focus_rows(day: str) -> list[dict]:
    """Raw focus samples for one day, in order.

    Written by the menu bar app; see the FocusLog comment in main.swift for why
    it is sampled rather than driven by activation notifications.
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


def focus_for(day: str) -> list[datetime]:
    """Minutes spent attended, at the front of an app that counts as work.

    This is what replaced Slack sends as the evidence that a stretch in Slack
    was work. Sends were a bad proxy in the one direction that mattered:
    reading half an hour of a thread and answering nothing produced no evidence
    whatsoever, so the largest genuinely-working stretches Slack ever generated
    were exactly the ones it reported as gaps.

    Returned as one datetime per covered minute rather than as spans, because
    every other input here is a point event and the period machinery is built
    on chaining point events. A span would need its own path through code that
    already works.

    Credit runs between consecutive samples, and is decided by the LATER
    sample's idle reading: idle is measured backwards from the sample, so the
    row that closes a window is the one that knows whether anybody touched the
    machine during it. Deciding on the earlier row instead would credit the
    first two minutes of every absence, every time.
    """
    rows = focus_rows(day)
    base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    minutes: set[int] = set()
    for a, b in zip(rows, rows[1:]):
        lo, hi = sec_of(a["t"]), sec_of(b["t"])
        if not (0 < hi - lo <= FOCUS_MAX_GAP_SEC):
            continue
        if not a.get("bundle") or a["bundle"] in FOCUS_EXCLUDE:
            continue
        if b.get("idle", 0) > FOCUS_IDLE_SEC:
            continue
        # hi is exclusive: a window ending exactly at 09:05:00 covers no part
        # of 09:05, and crediting it would add a phantom minute to the end of
        # every contiguous run.
        minutes.update(range(lo // 60, (hi - 1) // 60 + 1))
    return [base + timedelta(minutes=m) for m in sorted(minutes)]


def focus_app_by_minute(day: str) -> dict[int, str]:
    """The app that held most of each attended minute, in one pass.

    focus_apps() answers the same question for an arbitrary range, which is the
    right shape for labelling a period (a handful of calls a day) and the wrong
    one for labelling every minute: called in a loop it rescans the whole log
    each time, and the log is ~2,900 rows against ~1,400 minutes. That is four
    million row-visits on a path the menu bar polls every five seconds.
    """
    per: dict[int, dict[str, int]] = {}
    rows = focus_rows(day)
    for a, b in zip(rows, rows[1:]):
        lo, hi = sec_of(a["t"]), sec_of(b["t"])
        if not (0 < hi - lo <= FOCUS_MAX_GAP_SEC):
            continue
        if not a.get("bundle") or a["bundle"] in FOCUS_EXCLUDE:
            continue
        if b.get("idle", 0) > FOCUS_IDLE_SEC:
            continue
        name = a.get("app") or a["bundle"]
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
    rows = focus_rows(day)
    for a, b in zip(rows, rows[1:]):
        alo, ahi = sec_of(a["t"]), sec_of(b["t"])
        if not (0 < ahi - alo <= FOCUS_MAX_GAP_SEC):
            continue
        if not a.get("bundle") or a["bundle"] in FOCUS_EXCLUDE:
            continue
        if b.get("idle", 0) > FOCUS_IDLE_SEC:
            continue
        span = min(ahi, hi) - max(alo, lo)
        if span <= 0:
            continue
        name = a.get("app") or a["bundle"]
        seen[name] = seen.get(name, 0) + span
    return [k for k, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


# The WSL box's activity export -- see the header comment at the top of
# Dashboard/Vault Dashboard.md for the full inventory of what it writes.
ACTIVITY_DIR = os.path.expanduser("~/Documents/Main/Dashboard/activity")

CHROME_ROW = re.compile(
    r"^\|\s*(\d{1,2}:\d{2})\s*\|\s*chrome\s*\|\s*visit\s*\|[^|]*\|\s*(.*?)\s*\|\s*$")

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
# they manufactured Rubrik work periods out of a personal login.
GITHUB_AUTH = re.compile(r"/saml/|/login/oauth/|sso\.rubrik\.com", re.I)


def github_rows_for(day: str) -> list[tuple[datetime, str]]:
    """Chrome visits to GitHub code pages, with the page each one landed on.

    Reading a PR or a diff is real work and was previously invisible to this
    probe -- prompts and Slack sends were the only evidence of working, so a
    stretch spent entirely in a browser reviewing code read as a gap. It is
    also the only evidence that stretch produces: a review generates no prompt
    and no Slack message.

    Restricted to GitHub rather than all Chrome activity: the export also
    carries plain browsing (shopping, general search) that is not work, and
    counting every visit would manufacture "working" out of that.
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
        hit = "github.com" in detail.lower() or GITHUB_TITLE.search(detail)
        if not hit or GITHUB_AUTH.search(detail):
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


def events_for(day: str) -> list[datetime]:
    """Everything that proves someone was working.

    Prompts, attended foreground minutes, permission approvals, and github.com
    Chrome visits, merged into one list on purpose. A Slack reply two minutes
    after a prompt continues that work period; treating the streams separately
    would put a gap between them and then count the same stretch twice.

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


def recent_activities(day: str, limit: int = ACTIVITY_LIST_N) -> list[dict]:
    """The most recent work events, newest first, each with what it was.

    The same streams events_for() merges, deliberately: this is meant to be the
    readable form of exactly what the dot's verdict was derived from, so a
    stream that moves the dot but is missing here -- or the reverse -- would
    make the list a second opinion on the day rather than an explanation of it.

    Slack is the one entry that is no longer evidence in its own right, since
    sends stopped counting as presence. It stays because it is not a separate
    claim about the day: a message was typed with Slack in front of you on an
    attended machine, so the minute it names is a minute focus already counted.
    It says what that minute was ABOUT, which is the one thing focus cannot.

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

    for when, detail in github_rows_for(day):
        rows.append((when.strftime("%H:%M:00"), {
            "t": when.strftime("%H:%M"), "kind": "github",
            "what": one_line(detail)}))

    spoken = {k[:5] for k, _ in rows}
    by_minute = focus_app_by_minute(day)
    for when in focus_for(day):
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
    # Collapsed before the limit is applied, so ten rows are ten distinct
    # activities rather than ten samples of however few. The whole day is
    # collapsed rather than stopping at the tenth row: cutting the walk short
    # there would leave that last row's own duplicates uncounted, so it alone
    # would report a smaller number than it should.
    out: list[dict] = []
    for _, a in rows:
        if out and out[-1]["kind"] == a["kind"] and out[-1]["what"] == a["what"]:
            out[-1]["n"] += 1
            continue
        out.append(dict(a, n=1))
    return out[:limit]


# Markdown, not JSON. Obsidian Sync ships .md between devices by default but
# skips every other file type unless "Sync all other file types" is switched
# on, so a .json dump written on another machine silently never arrives -- it
# is not a transport that can be relied on without that setting. A table in a
# note syncs, and stays readable in Obsidian besides.
CAL_FILE = os.path.expanduser("~/Documents/Main/Dashboard/calendar-today.md")
CAL_STALE_HOURS = 6

CAL_ROW = re.compile(
    r"^\|\s*(\d{1,2}:\d{2})\s*\|\s*(\d{1,2}:\d{2})\s*\|\s*(.*?)\s*\|"
    r"(?:\s*([A-Za-z]+)\s*\|)?")
CAL_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
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


def summarize_span(day: str, lo: int, hi: int) -> str:
    """A <=10-word description of one work period, cached on disk.

    Cached by (day, start, end) because the probe reruns every 20 minutes and
    a finished period never changes -- without it each cycle would re-bill the
    same handful of spans forever. The still-open final period is deliberately
    NOT cached: its content grows as the day does, so a cached line would
    freeze at whatever the first prompt happened to be about.
    """
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
VAULT_SNAPSHOT_DIR = os.path.expanduser("~/Documents/Main/Dashboard/worktime")


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


def write_vault_snapshot(day: str, events: list[datetime]) -> None:
    """Publish today's work periods where the Obsidian dashboard can read them.

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
    now_s = (now_local().hour * 3600 + now_local().minute * 60
             + now_local().second)
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
    present = build_bouts(stamps, session_first_stamps(day), tl)

    # Manual marks are presence, unioned in the same way meetings are. They
    # carry no tail or lead: a mark has declared bounds and needs neither.
    present += [[m["start"] * 60, min(m["end"] * 60, now_s)]
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
    present += [[m["start"] * 60, min(effective_meeting_end(m, cuts) * 60, now_s)]
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
    gaps = [{"start": a[1], "end": b[0], "len": b[0] - a[1], "open": False}
            for a, b in zip(merged, merged[1:]) if b[0] > a[1]]

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
            "start": a, "end": b, "len": b - a,
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
        # from showing a period list up to a cron interval out of date.
        "fp": activity_fingerprint(day),
        "gaps": gaps,
        "worked": worked,
        "gap_minutes": sum(g["len"] for g in gaps),
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
        "work_minutes": sum(w["len"] for w in worked),
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

    total = sum(w["len"] for w in worked)
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
        "| Start | End | Mins | Prompts | Slack | Marked | Meeting | What |",
        "|-------|-----|------|---------|-------|--------|---------|------|",
    ]
    for w in worked:
        meeting = "; ".join(
            m.get("title") or "(busy)" for m in w.get("meetings", [])
            if m.get("counts", True)) or ""
        marked = "yes" if w.get("marks") else ""
        what = (w.get("what") or "").replace("|", "\\|")
        lines.append(
            f"| {hhmm(w['start'])} | {hhmm(w['end'])} | {w['len']} | "
            f"{w.get('n_prompts', 0)} | {w.get('n_slack', 0)} | {marked} | "
            f"{meeting.replace('|', '/')} | {what} |")
    if gaps:
        lines += ["", "## Gaps", "",
                  "| Start | End | Mins |", "|-------|-----|------|"]
        for g in gaps:
            lines.append(f"| {hhmm(g['start'])} | {hhmm(g['end'])} | {g['len']} |")
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
STATUS_CACHE_V = 2


def activity_fingerprint(day: str) -> str:
    """A signature of every input the live verdict is derived from.

    Cheap on purpose -- 944 transcripts stat in ~20ms, against ~650ms to
    actually re-derive the day, so a poll that finds this unchanged can skip
    the work entirely. That is what makes a 5-second dot affordable: the
    expensive path runs when something really happened, not on a timer.

    Slack is the exception and has to be handled by time rather than by file.
    A message sent from the phone changes nothing on this disk, so the
    fingerprint alone would never notice it and the cache would go stale
    forever. Folding the TTL bucket in forces a genuine recompute every
    SLACK_TTL_SEC, which is the refresh cadence Slack already had.

    Since sends stopped counting as presence that bucket no longer moves the
    dot -- it now only keeps the tooltip's message list current, which is
    still worth a recompute every four minutes and nothing like often enough
    to matter. The focus log is an ordinary file and needs no such trick,
    though it does mean the fingerprint misses on every heartbeat: a full
    re-derivation twice a minute, against the six a minute an active Claude
    session already causes through its transcript.
    """
    parts = []
    for root, _dirs, files in os.walk(os.path.expanduser("~/.claude/projects")):
        for f in files:
            if f.endswith(".jsonl"):
                p = os.path.join(root, f)
                st = os.stat(p)
                parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
    for p in (MARKS, APPROVALS, MODEFILE, CAL_FILE,
              os.path.join(SLACK_DIR, f"{day}.json"),
              os.path.join(FOCUS_DIR, f"{day}.jsonl"),
              os.path.join(ACTIVITY_DIR, f"{day}.md")):
        try:
            st = os.stat(p)
            parts.append(f"{p}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{p}:absent")
    if day == now_local().strftime("%Y-%m-%d"):
        parts.append(f"slack-window:{int(time.time()) // SLACK_TTL_SEC}")
    return hashlib.sha1("|".join(sorted(parts)).encode()).hexdigest()


def live_activity(day: str) -> tuple[datetime | None, list[int], list[dict]]:
    """Last event, mark-closing stamps and the recent list, memoised on the
    fingerprint.

    Returns only the things status() needs. Everything time-dependent -- how
    long the silence has run, whether a mark is still open right now -- is
    recomputed by the caller from these, so a cache hit still produces a
    verdict that moves with the clock.

    The activity list rides along here rather than being built in status()
    because it is a function of exactly the same inputs as `last`: it can only
    change when the fingerprint does, so caching it beside them means a poll
    during genuine silence still costs nothing but a walk of stat() calls.
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
                c["stamps"], c["acts"])

    fp = activity_fingerprint(day)
    if hit and c.get("fp") == fp:
        last = datetime.fromisoformat(c["last"]) if c["last"] else None
        return last, c["stamps"], c["acts"]

    events = events_for(day)
    # Prompts and focus only, matching what marks_for() closes an open mark on.
    # Approvals are deliberately excluded there and must stay excluded here.
    stamps = sorted([t.hour * 60 + t.minute for t in prompts_for(day)]
                    + [t.hour * 60 + t.minute for t in focus_for(day)])
    last = events[-1] if events else None
    acts = recent_activities(day)
    # Per-pid, because this is now written by whichever process polls first and
    # there are several: the menu bar every 5s, the cron check, and any CLI run.
    # A shared fixed name meant two of them raced on the same path -- the first
    # to finish renamed it away and the second died on os.replace with
    # FileNotFoundError, which surfaced as the dot dropping out mid-poll.
    tmp = f"{STATUS_CACHE}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"v": STATUS_CACHE_V, "fp": fp, "day": day, "stamps": stamps,
                   "at": time.time(), "acts": acts,
                   "last": last.isoformat() if last else None}, fh)
    os.replace(tmp, STATUS_CACHE)
    return last, stamps, acts


def status() -> dict:
    """Current state, cheap enough for a menu bar to poll every minute.

    Deliberately NOT a call to check(): check() advances the cursor and appends
    a label row, so polling it from the menu bar would corrupt the very record
    the dashboard audits. This only reads.
    """
    now = now_local()
    day = now.strftime("%Y-%m-%d")
    now_m = now.hour * 60 + now.minute
    last, stamps, activities = live_activity(day)
    quiet_sec = (now - last).total_seconds() if last else None
    quiet = quiet_sec / 60 if quiet_sec is not None else None

    # What the run in progress has earned, not the flat GAP_AFTER: in unfocused
    # mode a lone prompt lapses after a minute and only a session that has been
    # going a while holds the dot green for the full five.
    cutoff_sec = live_cutoff(day, [m * 60 for m in stamps])

    open_mark = next((m for m in marks_for(day, stamps)
                      if m["open"] and m["start"] <= now_m <= m["end"]), None)
    in_meeting = covered_by_meeting(now, [
        m for m in (calendar_events(day) or [])
        if m.get("counts", True)])
    if open_mark:
        state, why = "marked", open_mark["note"] or "marked as working"
    elif in_meeting:
        state, why = "working", f"in {in_meeting.get('title') or 'meeting'}"
    elif quiet is not None and quiet * 60 <= cutoff_sec:
        state, why = "working", f"{quiet:.0f}m since last activity"
    else:
        state = "idle"
        why = f"quiet {quiet:.0f}m" if quiet is not None else "nothing today"

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
    worked_minutes = 0
    periods = []
    focus_pct = None
    path = snapshot_path(day)
    if not os.path.exists(path) or json.load(open(path)).get("fp") != activity_fingerprint(day):
        write_vault_snapshot(day, events_for(day))
    if os.path.exists(path):
        snap = json.load(open(path))
        worked_minutes = snap.get("work_minutes", 0)
        worked = snap.get("worked", [])
        ds, de = snap.get("day_start"), snap.get("day_end")
        if ds is not None and de is not None and de > ds:
            focus_pct = round(100 * worked_minutes / (de - ds))
        # Newest first, so the menu bar can show the last 3 without slicing
        # from the wrong end. The last entry here is the day's most recent
        # period, open or closed -- whichever it is, it's what "recent
        # activity" means.
        for i, w in enumerate(worked):
            periods.append({
                "start": w["start"], "end": w["end"], "len": w["len"],
                "n_prompts": w.get("n_prompts", 0),
                "n_slack": w.get("n_slack", 0),
                "what": w.get("what", ""),
                "current": i == len(worked) - 1,
            })
        periods.reverse()

    # Read from the same snapshot the menu bar's period list came from, not
    # recomputed here -- this is the on-disk record of the last completed
    # `check()`, and this call is deliberately read-only (see docstring).
    return {"state": state, "why": why, "worked_minutes": worked_minutes,
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
            "periods": periods,
            # Newest first, same as `periods`. The periods say how the day was
            # divided up; this says what the divisions were made out of.
            "activities": activities}


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
              f"{snap['work_minutes'] // 60}h{snap['work_minutes'] % 60:02d}m worked")


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
            # A day with no events yet clamps to each mark's own start inside
            # close_open_marks, which closes it zero-length.
            when = max((e.hour * 60 + e.minute for e in events), default=0)
        closed = close_open_marks(when)
        write_vault_snapshot(day, events)
        print(json.dumps({
            "closed": [{"start": hhmm_of(r["start"]), "end": hhmm_of(r["end"]),
                        "note": r["note"]} for r in closed],
        }))
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
    elif cmd == "status":
        # One small line for the menu bar: what the tracker thinks right now.
        print(json.dumps(status()))
    else:
        print(__doc__)
