#!/usr/bin/env python3
"""Report what the menu bar dot would show, without touching the Mac.

This reads the same file the probe reads (`calendar-today.md`) and applies the
same rule the probe is believed to apply: a row tagged `work` covering now means
working. It is a mirror, not the probe itself -- until worktime-probe.py is in
this repo, a disagreement between this and the real dot means this file's
assumption is wrong, not the dot.

Prints one status line plus context. Exit 0 working, 1 not working, 2 unusable.
"""
import argparse, os, re, sys
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo

CALENDAR = os.path.expanduser("~/obsidian-vault/Dashboard/calendar-today.md")
# The probe refuses the file once it is this old; mirror that rather than
# reporting a confident status from data the probe would have thrown away.
MAX_AGE_HOURS = 6
ROW = re.compile(r"^\|\s*(\d{2}:\d{2})\s*\|\s*(\d{2}:\d{2})\s*\|\s*(.*?)\s*\|\s*(\w+)\s*\|\s*$")


def parse(text):
    """Return ([(start, end, label, source)], generated_str_or_None)."""
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
            if r[3] == "work" and minutes(r[0]) <= now_min < minutes(r[1])]
    return sorted(hits, key=lambda r: minutes(r[1]) - minutes(r[0]), reverse=True)


def previous(rows, now_min):
    past = [r for r in rows if r[3] == "work" and minutes(r[1]) <= now_min]
    return max(past, key=lambda r: minutes(r[1])) if past else None


def upcoming(rows, now_min):
    fut = [r for r in rows if r[3] == "work" and minutes(r[0]) > now_min]
    return min(fut, key=lambda r: minutes(r[0])) if fut else None


def report(text, now, age_hours=None):
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", default=CALENDAR)
    p.add_argument("--at", help="HH:MM to evaluate instead of now")
    a = p.parse_args()
    if not os.path.exists(a.file):
        print(f"UNKNOWN - no calendar file at {a.file}", file=sys.stderr)
        sys.exit(2)
    text = open(a.file).read()
    tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    if a.at:
        h, m = a.at.split(":")
        now = now.replace(hour=int(h), minute=int(m))
    age = (datetime.now().timestamp() - os.path.getmtime(a.file)) / 3600
    code, lines = report(text, now, age)
    print("\n".join(lines))
    sys.exit(code)


if __name__ == "__main__":
    main()
