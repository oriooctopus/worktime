#!/usr/bin/env python3
"""Devpod-side UserPromptSubmit hook: push a live heartbeat to a private gist.

The file mirror (devpod-prompt-sync.py) is accurate but runs on a multi-minute
schedule, since every cycle pays for a fresh gcloud/IAP round trip per pod.
This closes that latency gap for the *live* dot only: the moment a prompt is
submitted on a devpod, this pushes {hostname: now} to a private gist the Mac
polls with a plain HTTPS GET (no gcloud, no IAP, no per-pod SSH).

Deliberately NOT a source of the day's prompt totals -- those still come only
from the file mirror, so a prompt this hook reports can never be double
counted once the mirror also picks it up later.

Uses the pod's own git SSH key (the same one that already clones/pushes
sdmain) via git@gist.github.com -- gists are per-account, not per-repo, so no
new credential is needed here.

History is kept flat with `commit --amend` + force-push: pushing on every
prompt would otherwise leave the gist with one revision per prompt forever.

A hook failure must never surface as a broken prompt, so every step here is
best-effort and every exception is swallowed.
"""
import json
import os
import socket
import subprocess
from datetime import datetime, timezone

GIST_ID = "d9c0fa29aff1cc99deae920a907ea31b"
GIST_REMOTE = f"git@gist.github.com:{GIST_ID}.git"
LOCAL_CLONE = os.path.expanduser("~/.cache/worktime-heartbeat-gist")
HEARTBEAT_FILE = "heartbeat.json"


def run(*args, cwd=None):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=20
    )


def ensure_known_host():
    # gist.github.com is a distinct host for SSH host-key purposes even
    # though it's the same GitHub infra as github.com -- a pod that already
    # trusts github.com (needed for its own git clone/push) still fails
    # strict host-key checking against gist.github.com until this runs once.
    known_hosts = os.path.expanduser("~/.ssh/known_hosts")
    try:
        with open(known_hosts) as f:
            if "gist.github.com" in f.read():
                return
    except OSError:
        pass
    keys = run("ssh-keyscan", "gist.github.com")
    if keys.returncode == 0 and keys.stdout:
        with open(known_hosts, "a") as f:
            f.write(keys.stdout)


def ensure_clone():
    if os.path.isdir(os.path.join(LOCAL_CLONE, ".git")):
        return True
    ensure_known_host()
    os.makedirs(os.path.dirname(LOCAL_CLONE), exist_ok=True)
    if run("git", "clone", GIST_REMOTE, LOCAL_CLONE).returncode != 0:
        return False
    # A devpod's own git identity is unset -- it has never needed one, since
    # its only git activity until now was fetch/checkout against sdmain, not
    # a commit. Scoped to this repo only, not --global, so it can't affect
    # how any real work gets attributed elsewhere on the pod.
    run("git", "config", "user.email", "oliver.ullman@rubrik.com", cwd=LOCAL_CLONE)
    run("git", "config", "user.name", "Oliver Ullman", cwd=LOCAL_CLONE)
    return True


def main():
    if not ensure_clone():
        return
    path = os.path.join(LOCAL_CLONE, HEARTBEAT_FILE)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data[socket.gethostname()] = datetime.now(timezone.utc).isoformat()
    with open(path, "w") as f:
        json.dump(data, f)
    run("git", "add", "-A", cwd=LOCAL_CLONE)
    # --allow-empty-message: the gist's very first commit (made by `gh gist
    # create`) already has an empty message, and --no-edit reuses whatever
    # message is already there -- without this flag git refuses to amend an
    # empty message forward.
    run(
        "git", "commit", "--amend", "--no-edit", "--allow-empty-message",
        cwd=LOCAL_CLONE,
    )
    run("git", "push", "--force", cwd=LOCAL_CLONE)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
