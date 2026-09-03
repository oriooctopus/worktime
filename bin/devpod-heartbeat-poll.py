#!/usr/bin/env python3
"""Mirror the devpod prompt-heartbeat gist to a local file for the probe.

devpod-prompt-heartbeat.py (the pod-side hook) pushes {hostname: last-prompt-
time} to a private gist within seconds of a prompt. worktime-probe.py's live
dot polls every 5 seconds and cannot hit the network on every poll -- that
would hammer GitHub's API continuously and defeats the whole cache-fingerprint
design the probe already leans on for every other external signal (Slack,
calendar, Chrome). So, same pattern as those: this fetches the gist on its own
cheap schedule and writes it locally; the probe only ever reads the local
copy.

This is a live-dot freshness signal only. Day-total prompt counts still come
solely from devpod-prompt-sync.py's file mirror, so nothing here can cause a
prompt to be counted twice once the mirror also picks it up.
"""
import json
import os
import shutil
import subprocess

GIST_ID = "d9c0fa29aff1cc99deae920a907ea31b"
GIST_FILE = "heartbeat.json"
LOCAL_PATH = os.path.expanduser("~/.claude/stats/worktime/devpod-heartbeat.json")
# `gh` only exists inside the sdmain buildenv on this Mac -- there is no
# system-wide install -- so it has to be found there explicitly rather than
# relying on PATH, which a launchd job's minimal environment doesn't have.
GH_BIN = shutil.which("gh") or os.path.expanduser(
    "~/Documents/coding/sdmain/polaris/.buildenv/bin/gh"
)


def main():
    result = subprocess.run(
        [GH_BIN, "gist", "view", GIST_ID, "-f", GIST_FILE],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        return  # best-effort: leave the last-known-good copy in place
    try:
        json.loads(result.stdout)  # validate before overwriting a good copy
    except json.JSONDecodeError:
        return
    os.makedirs(os.path.dirname(LOCAL_PATH), exist_ok=True)
    tmp = LOCAL_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(result.stdout)
    os.replace(tmp, LOCAL_PATH)


if __name__ == "__main__":
    main()
