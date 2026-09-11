#!/usr/bin/env python3
"""Answer requests the Mac leaves in the vault, from the desktop's side.

The Mac cannot reach this box, but both see the same Obsidian vault. The Mac
drops <dashboard>/wsl-inbox/<id>.json ({"id", "action"}); this runs on a
systemd timer, performs the action, writes <dashboard>/wsl-outbox/<id>.md and
deletes the request. It also rewrites wsl-outbox/heartbeat.md every run, so the
Mac can tell "this box is up and syncing" apart from "the exporter is dead".

Actions are a fixed list of argv vectors, never a shell string from the file:
anyone who can write to the synced vault could otherwise run code here.

Python 3.8 on this box: no `X | None`, no match.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime

# Throttle for wsl-outbox/heartbeat.md -- this timer runs every 1 minute, but
# the Mac side only needs "is this box still alive" at a much coarser grain.
# Rewriting every single minute would create an Obsidian Sync version every
# minute (1440/day) for a file whose only content is a timestamp; matches the
# same 10-minute freshness window activity-export.py uses for _status.md.
HEARTBEAT_FRESHNESS_SECONDS = 10 * 60
_ALIVE_AT_RE = re.compile(r'^alive_at:\s*(\S+)\s*$', re.MULTILINE)

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktime_common as wc  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
OUTPUT_CAP = 4000
COMMAND_TIMEOUT_SEC = 60

EXPORTER_LOG = ["journalctl", "--user", "-u", "activity-export", "-n", "40",
                "--no-pager"]

ACTIONS = {
    "status": [
        ["systemctl", "--user", "list-timers", "--all", "--no-pager"],
        ["systemctl", "--user", "status", "activity-export.service",
         "--no-pager"],
        EXPORTER_LOG,
        ["pgrep", "-fa", "-i", "obsidian"],
    ],
    "run-exporter": [
        ["systemctl", "--user", "start", "activity-export.service"],
        EXPORTER_LOG,
    ],
    "restart-exporter": [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "activity-export.timer"],
        ["systemctl", "--user", "restart", "activity-export.timer"],
        ["systemctl", "--user", "list-timers", "--all", "--no-pager"],
    ],
    "pull": [
        ["git", "-C", REPO, "pull", "--ff-only"],
        ["git", "-C", REPO, "log", "--oneline", "-3"],
    ],
}


def run_command(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=COMMAND_TIMEOUT_SEC)
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {COMMAND_TIMEOUT_SEC}s"
    return p.returncode, (p.stdout + p.stderr)[-OUTPUT_CAP:]


_DAY_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def newest_exports(dashboard, n=3):
    """Newest N day folders under activity/ (chunked layout, see
    activity-export.py), each shown with its hour-chunk count and the
    latest mtime among its files -- plus _status.md's own mtime, since that
    file (not any one chunk) is the freshness signal a reader should trust."""
    d = os.path.join(dashboard, "activity")
    day_dirs = sorted(
        (x for x in os.listdir(d) if _DAY_DIR_RE.match(x) and os.path.isdir(os.path.join(d, x))),
        reverse=True,
    )
    lines = []
    for day in day_dirs[:n]:
        day_path = os.path.join(d, day)
        entries = os.listdir(day_path)
        hour_count = sum(1 for f in entries if re.match(r'^\d{2}\.md$', f))
        latest = max((os.path.getmtime(os.path.join(day_path, f)) for f in entries), default=None)
        latest_str = f"{datetime.fromtimestamp(latest):%Y-%m-%d %H:%M}" if latest else "(empty)"
        lines.append(f"{day}  {hour_count} hour chunk(s)  {latest_str}")
    status_path = os.path.join(d, "_status.md")
    if os.path.exists(status_path):
        lines.append(
            f"_status.md  {datetime.fromtimestamp(os.path.getmtime(status_path)):%Y-%m-%d %H:%M}"
        )
    return "\n".join(lines)


def answer(request, dashboard, runner):
    action = request["action"]
    lines = [f"# {action} — {request['id']}", "",
             f"answered: {datetime.now().astimezone().isoformat(timespec='seconds')}", ""]
    if action not in ACTIONS:
        lines.append(f"unknown action {action!r}; allowed: {', '.join(sorted(ACTIONS))}")
        return "\n".join(lines) + "\n"
    for argv in ACTIONS[action]:
        code, out = runner(argv)
        lines += [f"## `{' '.join(argv)}` → exit {code}", "", "```", out.rstrip(), "```", ""]
    lines += ["## newest activity exports on this box", "", "```",
              newest_exports(dashboard), "```", ""]
    return "\n".join(lines)


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def maybe_write_heartbeat(outbox, now):
    """Rewrite wsl-outbox/heartbeat.md only if the existing alive_at is more
    than HEARTBEAT_FRESHNESS_SECONDS old (or missing/malformed) -- see the
    module-level comment on HEARTBEAT_FRESHNESS_SECONDS for why this can't
    just write every run. Returns True iff it wrote."""
    path = os.path.join(outbox, "heartbeat.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
        m = _ALIVE_AT_RE.search(existing)
        if m:
            old_dt = datetime.fromisoformat(m.group(1))
            if (now - old_dt).total_seconds() < HEARTBEAT_FRESHNESS_SECONDS:
                return False
    except (OSError, ValueError):
        pass  # missing/malformed -- always write
    write_atomic(path, f"---\nalive_at: {now.isoformat(timespec='seconds')}\n---\n")
    return True


def process(dashboard, runner=run_command, now=None):
    inbox = os.path.join(dashboard, "wsl-inbox")
    outbox = os.path.join(dashboard, "wsl-outbox")
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(outbox, exist_ok=True)
    now = now or datetime.now().astimezone()
    maybe_write_heartbeat(outbox, now)
    answered = []
    for name in sorted(os.listdir(inbox)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(inbox, name)
        with open(path) as fh:
            request = json.load(fh)
        write_atomic(os.path.join(outbox, f"{request['id']}.md"),
                     answer(request, dashboard, runner))
        os.remove(path)
        answered.append(request["id"])
    return answered


if __name__ == "__main__":
    for rid in process(wc.dashboard_dir()):
        print(f"answered {rid}")
