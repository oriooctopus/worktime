#!/usr/bin/env python3
"""Leave a request for the desktop in the vault, and optionally wait for it.

    wsl-ask.py status [--wait 600]
    wsl-ask.py heartbeat

The round trip is Obsidian Sync out, the desktop's one-minute timer, and Sync
back, so expect a few minutes. See wsl-inbox.py for the actions and why they
are a fixed list.

Note on `heartbeat`: the desktop timer itself still runs every minute, but it
only REWRITES wsl-outbox/heartbeat.md when the existing one is more than 10
minutes stale (see wsl-inbox.py's HEARTBEAT_FRESHNESS_SECONDS) -- so "alive
Nm ago" can legitimately read up to ~10 minutes even on a perfectly healthy
box, not just when something's actually stuck.
"""
import argparse
import importlib.util
import json
import os
import sys
import time
import uuid
from datetime import datetime

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import worktime_common as wc  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "wsl_inbox", os.path.join(HERE, "wsl-inbox.py"))
wsl_inbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wsl_inbox)


def ask(dashboard, action):
    inbox = os.path.join(dashboard, "wsl-inbox")
    os.makedirs(inbox, exist_ok=True)
    rid = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    wsl_inbox.write_atomic(os.path.join(inbox, f"{rid}.json"),
                           json.dumps({"id": rid, "action": action}))
    return rid


def heartbeat(dashboard):
    path = os.path.join(dashboard, "wsl-outbox", "heartbeat.md")
    if not os.path.exists(path):
        return "no heartbeat yet — wsl-inbox has never run, or Sync never delivered it"
    stamp = next(line.split(":", 1)[1].strip() for line in open(path)
                 if line.startswith("alive_at:"))
    age = (datetime.now().astimezone() - datetime.fromisoformat(stamp)).total_seconds()
    return f"desktop last alive {stamp} ({age / 60:.0f}m ago)"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=sorted(wsl_inbox.ACTIONS) + ["heartbeat"])
    p.add_argument("--wait", type=int, default=0,
                   help="seconds to wait for the reply")
    a = p.parse_args()
    dashboard = wc.dashboard_dir()
    if a.action == "heartbeat":
        print(heartbeat(dashboard))
        return
    rid = ask(dashboard, a.action)
    reply = os.path.join(dashboard, "wsl-outbox", f"{rid}.md")
    print(f"asked {a.action} as {rid}; reply lands at {reply}")
    deadline = time.time() + a.wait
    while time.time() < deadline:
        if os.path.exists(reply):
            print(open(reply).read())
            return
        time.sleep(10)
    if a.wait:
        sys.exit(f"no reply after {a.wait}s — {heartbeat(dashboard)}")


if __name__ == "__main__":
    main()
