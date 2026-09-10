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
import subprocess
import sys
from datetime import datetime

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


def newest_exports(dashboard, n=3):
    d = os.path.join(dashboard, "activity")
    names = sorted((x for x in os.listdir(d) if x.endswith(".md")), reverse=True)
    return "\n".join(
        f"{x}  {datetime.fromtimestamp(os.path.getmtime(os.path.join(d, x))):%Y-%m-%d %H:%M}"
        for x in names[:n])


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


def process(dashboard, runner=run_command, now=None):
    inbox = os.path.join(dashboard, "wsl-inbox")
    outbox = os.path.join(dashboard, "wsl-outbox")
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(outbox, exist_ok=True)
    now = now or datetime.now().astimezone()
    write_atomic(os.path.join(outbox, "heartbeat.md"),
                 f"---\nalive_at: {now.isoformat(timespec='seconds')}\n---\n")
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
