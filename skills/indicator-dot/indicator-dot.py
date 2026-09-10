#!/usr/bin/env python3
"""Report what the menu bar dot would show.

Two paths:

- Current time (default): calls ``worktime-probe.py status`` for the live
  verdict. The probe combines prompt activity, Slack messages, manual marks,
  and calendar events; this is the authoritative answer. A working session
  with no calendar meeting still shows GREEN here, matching the real dot.

- Historical time (--at HH:MM): reads the probe's daily snapshot JSON if
  available, and falls back to the calendar-only rule (a work-tagged row
  covering the requested time means working). The fallback does not reflect
  prompt activity or manual marks -- use it only when the snapshot is absent.

Prints one status line plus context. Exit 0 working, 1 not working, 2 unusable.
Colour only when stdout is a terminal; piping gives plain text.
"""
import argparse, json, os, re, subprocess, sys
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo

# realpath, not abspath: this file is normally reached through a symlink, and
# abspath would look for the shared module beside the symlink.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "bin"))
import worktime_common as wc  # noqa: E402

# The probe that owns the working/not-working decision.
PROBE = os.path.expanduser("~/.claude/bin/worktime-probe.py")

# Where the vault lives is a fact about the machine, not about this script: it
# is under Documents/Main on the Mac and ~/obsidian-vault on the Linux box.
# This used to resolve it independently, and swallowed an unreadable profile so
# a mis-configured dashboard_dir rendered a confident status from the wrong
# directory rather than saying so.
dashboard_dir = wc.dashboard_dir


DASHBOARD = dashboard_dir()
# Where the probe writes daily snapshots (used for --at historical lookups).
SNAPSHOT_DIR = os.path.join(DASHBOARD, "worktime")
# Calendar file the probe reads for meeting-based presence.
CALENDAR = os.path.join(DASHBOARD, "calendar-today.md")
# The probe refuses this file once it is this old; mirror that cutoff.
MAX_AGE_HOURS = 6

# Tags the probe counts as work meetings. Empty string covers exporters that
# predated the Calendar column; "rubrik" is a legacy tag from the work account.
WORK_TAGS = {"work", "rubrik", ""}

# Match optional trailing tag (\w* not \w+ so empty-tag rows parse too).
ROW = re.compile(r"^\|\s*(\d{2}:\d{2})\s*\|\s*(\d{2}:\d{2})\s*\|\s*(.*?)\s*\|\s*(\w*)\s*\|\s*$")


# ---------------------------------------------------------------------------
# Probe path (authoritative, current-time only)
# ---------------------------------------------------------------------------

def probe_status(python=None):
    """Call worktime-probe.py status and return parsed JSON, or None on failure."""
    if not os.path.exists(PROBE):
        return None
    py = python or sys.executable
    try:
        r = subprocess.run([py, PROBE, "status"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def report_probe(status):
    """Format output from probe status JSON. Returns (code, lines)."""
    state = status.get("state", "unknown")
    why = status.get("why", "")
    mode = status.get("mode", "focused")
    focus_pct = status.get("focus_pct")

    if state in ("working", "marked"):
        colour = "BLUE" if state == "marked" else "GREEN"
        lines = [f"{colour} - {why}"]
        worked = status["worked_sec"] // 60
        if worked:
            h, m = divmod(worked, 60)
            summary = f"{h}h {m}m worked today, {mode} mode"
            if focus_pct is not None:
                summary += f", {focus_pct}% focus"
            lines.append(f"  {summary}")
        for p in status.get("periods", [])[:3]:
            s, e = p["start"], p["end"]
            what = p.get("what") or ("in progress" if p.get("current") else "")
            n = p["len_sec"]
            took = f"{n}s" if n < 60 else f"{n // 60}m"
            lines.append(f"  {s//60:02d}:{s%60:02d}–{e//60:02d}:{e%60:02d} · "
                         f"{took}  {what}".rstrip())
        return 0, lines
    elif state == "idle":
        lines = [f"AMBER - {why}"]
        qs = status.get("quiet_since")
        if qs:
            lines.append(f"  quiet since {qs}")
        return 1, lines
    else:
        return 2, [f"UNKNOWN - probe returned state={state!r}"]


# ---------------------------------------------------------------------------
# Snapshot path (historical --at lookups)
# ---------------------------------------------------------------------------

def snapshot_report(day_str, at_min):
    """Find the probe's verdict for a past time from its saved snapshot.

    Returns (code, lines) or None if no snapshot exists for the day.
    """
    path = os.path.join(SNAPSHOT_DIR, f"{day_str}.json")
    if not os.path.exists(path):
        return None
    try:
        snap = json.load(open(path))
    except (OSError, ValueError):
        return None
    for w in snap.get("worked", []):
        if w["start"] <= at_min < w["end"]:
            s, e = w["start"], w["end"]
            what = w.get("what", "")
            lines = [f"GREEN - working: {s//60:02d}:{s%60:02d}–{e//60:02d}:{e%60:02d}"
                     + (f", {what}" if what else "")]
            return 0, lines
    return 1, [f"AMBER - not a worked period at {at_min//60:02d}:{at_min%60:02d}"]


# ---------------------------------------------------------------------------
# Calendar fallback path (when probe or snapshot unavailable)
# ---------------------------------------------------------------------------

def parse(text):
    """Return ([(start, end, label, tag)], generated_str_or_None)."""
    rows, generated = [], None
    for line in text.splitlines():
        m = ROW.match(line)
        if m and m.group(1) != "Start":
            rows.append(m.groups())
        elif line.startswith("generated:"):
            generated = line.split(":", 1)[1].strip()
    return rows, generated


def minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def covering(rows, now_min):
    """Work-tagged rows covering now, longest first."""
    hits = [r for r in rows
            if r[3] in WORK_TAGS and minutes(r[0]) <= now_min < minutes(r[1])]
    return sorted(hits, key=lambda r: minutes(r[1]) - minutes(r[0]), reverse=True)


def previous(rows, now_min):
    past = [r for r in rows if r[3] in WORK_TAGS and minutes(r[1]) <= now_min]
    return max(past, key=lambda r: minutes(r[1])) if past else None


def upcoming(rows, now_min):
    fut = [r for r in rows if r[3] in WORK_TAGS and minutes(r[0]) > now_min]
    return min(fut, key=lambda r: minutes(r[0])) if fut else None


def report(text, now, age_hours=None):
    """Calendar-based status. Used for --at historical queries and as fallback.

    This is an approximation: it only sees calendar events, not prompt
    activity or manual marks. Use report_probe() for the authoritative answer.
    """
    rows, generated = parse(text)
    out = []
    if not rows:
        return 2, ["UNKNOWN - no rows in calendar-today.md"]
    now_min = now.hour * 60 + now.minute
    hits = covering(rows, now_min)
    if hits:
        start, end, label, _ = hits[0]
        left = minutes(end) - now_min
        out.append(f"GREEN - working: {label} ({start}-{end}), {left} min left")
        for r in hits[1:]:
            out.append(f"  also covering now: {r[2]} ({r[0]}-{r[1]})")
        code = 0
    else:
        out.append(f"AMBER - not marked as working at {now.strftime('%H:%M')}")
        p = previous(rows, now_min)
        if p:
            out.append(f"  last: {p[2]} ended {p[1]} ({now_min - minutes(p[1])} min ago)")
        n = upcoming(rows, now_min)
        if n:
            out.append(f"  next: {n[2]} starts {n[0]} (in {minutes(n[0]) - now_min} min)")
        code = 1
    if age_hours is not None:
        if age_hours > MAX_AGE_HOURS:
            out.append(f"  STALE: data {age_hours:.1f}h old - the probe would reject this")
            code = 2
        else:
            out.append(f"  data {age_hours * 60:.0f} min old (generated {generated})")
    return code, out


# ---------------------------------------------------------------------------
# Terminal colour
# ---------------------------------------------------------------------------

COLOURS = {"GREEN": "\033[32m", "BLUE": "\033[34m", "AMBER": "\033[33m",
           "STALE": "\033[31m", "UNKNOWN": "\033[31m"}
RESET = "\033[0m"
DIM = "\033[2m"


def colourise(lines):
    out = []
    for i, line in enumerate(lines):
        key = next((k for k in COLOURS if line.lstrip().startswith(k)), None)
        if key:
            glyph = "*" if key in ("STALE", "UNKNOWN") else "●"
            out.append(f"{COLOURS[key]}{glyph} {line}{RESET}")
        elif i:
            out.append(f"{DIM}{line}{RESET}")
        else:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", default=CALENDAR,
                   help="Override the calendar file (for testing).")
    p.add_argument("--at", help="HH:MM to evaluate instead of now.")
    p.add_argument("--no-probe", action="store_true",
                   help="Skip the probe and use the calendar-only path.")
    a = p.parse_args()

    tz = wc.local_tz()
    now = datetime.now(tz)

    if a.at:
        # Historical query: try snapshot, fall back to calendar.
        h, m = a.at.split(":")
        now = now.replace(hour=int(h), minute=int(m))
        day_str = now.strftime("%Y-%m-%d")
        at_min = int(h) * 60 + int(m)
        snap = snapshot_report(day_str, at_min)
        if snap is not None:
            code, lines = snap
            if sys.stdout.isatty():
                lines = colourise(lines)
            print("\n".join(lines))
            sys.exit(code)
        # Fall through to calendar.
    elif not a.no_probe:
        # Current-time: ask the probe.
        status = probe_status()
        if status is not None:
            code, lines = report_probe(status)
            if sys.stdout.isatty():
                lines = colourise(lines)
            print("\n".join(lines))
            sys.exit(code)
        # Fall through to calendar if probe unavailable.

    # Calendar fallback.
    cal_file = a.file
    if not os.path.exists(cal_file):
        print(f"UNKNOWN - no calendar file at {cal_file}", file=sys.stderr)
        sys.exit(2)
    text = open(cal_file).read()
    age = (datetime.now().timestamp() - os.path.getmtime(cal_file)) / 3600
    code, lines = report(text, now, age)
    if sys.stdout.isatty():
        lines = colourise(lines)
    print("\n".join(lines))
    sys.exit(code)


if __name__ == "__main__":
    main()
