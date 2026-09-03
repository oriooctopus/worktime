#!/usr/bin/env python3
"""Mirror every active devpod's Claude Code transcripts onto this machine.

A prompt typed while working on a devpod (bench-pod, or any other) never
touches this Mac's own ~/.claude/projects -- it lands in the pod's own
~/.claude/projects, which prompt-count.py has no way to see. That makes
devpod work invisible to the tracker: a two-hour benchmark session run
entirely over SSH reads as idle.

This copies each active devpod's ~/.claude/projects down to
~/.claude-devpod/<alias>/projects, which worktime_common.devpod_project_roots()
then hands to every consumer that walks PROJECT_ROOTS. Meant to run on a
schedule (see deploy/launchd) rather than triggered by a hook, since nothing
on this Mac runs when a prompt is sent on a devpod.

Destroyed pods are pruned from the mirror so devpod list stays the source of
truth for which pods exist; their already-synced transcripts are untouched by
prompt-count.py's own day-cutoff, so nothing already counted is lost.

Two things keep short sync intervals cheap:

- The pod's $HOME is looked up once per pod incarnation and cached in
  STATE_FILE keyed by order_sid, not re-fetched every run -- a fresh
  `gcloud compute ssh` invocation costs ~3s of its own IAP-tunnel bootstrap
  regardless of connection reuse, so skipping a whole round trip matters more
  than speeding one up.
- `_prime_master` keeps a background SSH ControlMaster warm per pod, at the
  same ControlPath devpod's own tool already sets
  (/tmp/ssh-devpod-%C -- see rk-devpod's _gcloud_ssh_cmd/_gcloud_scp_cmd) but
  never turns on itself. Priming it here lets `devpod ssh`/`devpod scp` calls
  transparently multiplex through the warm connection instead of
  renegotiating SSH auth from scratch, which measured about 2x faster
  (~6s cold vs ~3s warm) -- real, but it does not touch the ~3s gcloud/IAP
  overhead itself, so sub-10s intervals stay tight even warmed.
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktime_common as wc  # noqa: E402

# `gcloud` (and `gh`) only exist inside the sdmain buildenv on this Mac --
# there is no system-wide install -- so a launchd job's minimal PATH can't
# find them, and neither can `devpod` itself, which shells out to `gcloud`
# for every SSH/scp call. Prepending the buildenv's bin dir fixes both.
_BUILDENV_BIN = os.path.expanduser(
    "~/Documents/coding/sdmain/polaris/.buildenv/bin"
)
if os.path.isdir(_BUILDENV_BIN):
    os.environ["PATH"] = _BUILDENV_BIN + os.pathsep + os.environ.get("PATH", "")

DEVPOD_BIN = shutil.which("devpod") or os.path.expanduser("~/.local/bin/devpod")
STATE_FILE = os.path.join(wc.DEVPOD_PROJECTS_ROOT, ".sync-state.json")
CONTROL_PATH = "/tmp/ssh-devpod-%C"
# Comfortably longer than any sane sync interval so the master outlives the
# gap between runs; each run's priming call refreshes it regardless.
CONTROL_PERSIST_SEC = 600


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def active_pods():
    """Full connection info for every devpod Bodega currently knows about,
    fulfilled or not -- an order still being provisioned has nothing to copy
    yet, but a completed one should not be pruned out from under it either."""
    result = subprocess.run(
        [DEVPOD_BIN, "list", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"devpod list failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    # devpod list prints a human status line ("Refreshed from Bodega.") before
    # the JSON array on stdout, so the array has to be located rather than
    # the whole stream parsed.
    start = result.stdout.find("[")
    if start == -1:
        return []
    try:
        pods = json.loads(result.stdout[start:])
    except json.JSONDecodeError:
        return []
    return [p for p in pods if p.get("status") == "fulfilled" and p.get("alias")]


def prime_master(pod):
    """Refresh (or start) a persistent SSH connection to this pod so the
    scp/ssh calls below can multiplex through it. Safe to call every run:
    ControlMaster=auto only starts a new master when none is live yet,
    otherwise this itself just rides the existing one."""
    conn = pod.get("connection") or {}
    if conn.get("transport") != "gcloud":
        return  # unknown transport -- let devpod's own tooling cold-connect
    subprocess.run(
        [
            "gcloud", "compute", "ssh", conn["instance_name"],
            f"--zone={conn['zone']}", f"--project={conn['project']}",
            "--tunnel-through-iap", "--quiet",
            "--ssh-flag=-o", f"--ssh-flag=ControlMaster=auto",
            "--ssh-flag=-o", f"--ssh-flag=ControlPersist={CONTROL_PERSIST_SEC}",
            "--ssh-flag=-o", f"--ssh-flag=ControlPath={CONTROL_PATH}",
            "--", "true",
        ],
        capture_output=True, text=True, timeout=30,
    )


def remote_home(alias, order_sid, state):
    """The pod's real $HOME, e.g. /home/jenkins, cached per order_sid so a
    pod recreated under the same alias doesn't reuse a stale value.

    `devpod scp` shlex.quotes the remote path before handing it to the
    remote shell, so a literal "~" is never expanded there -- it has to be
    resolved to an absolute path instead, and that resolution is the one
    round trip worth not repeating every sync.
    """
    cached = state.get(alias)
    if cached and cached.get("order_sid") == order_sid and cached.get("home"):
        return cached["home"]
    result = subprocess.run(
        [DEVPOD_BIN, "ssh", alias, "--", "echo $HOME"],
        capture_output=True, text=True, timeout=30,
    )
    home = result.stdout.strip() if result.returncode == 0 else None
    if home:
        state[alias] = {"order_sid": order_sid, "home": home}
    return home


def sync_one(pod, state):
    alias = pod["alias"]
    prime_master(pod)
    home = remote_home(alias, pod.get("order_sid"), state)
    if not home:
        print(f"{alias}: could not resolve $HOME, skipping", file=sys.stderr)
        return False
    # `devpod scp -r src dst` extracts src's *contents* into dst (like
    # `tar -C dst -x`), it does not nest a "projects" dir inside dst the way
    # `cp -r` would -- so dst itself has to be named "projects" to match the
    # <alias>/projects layout devpod_project_roots() globs for.
    dest_dir = os.path.join(wc.DEVPOD_PROJECTS_ROOT, alias, "projects")
    os.makedirs(dest_dir, exist_ok=True)
    result = subprocess.run(
        [DEVPOD_BIN, "scp", "-r", f"{alias}:{home}/.claude/projects", dest_dir],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        # Not fatal to the run as a whole -- one unreachable pod (mid-reboot,
        # SSH not up yet) shouldn't stop the others from syncing.
        print(f"{alias}: sync failed: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def prune_stale(aliases, state):
    if os.path.isdir(wc.DEVPOD_PROJECTS_ROOT):
        live = set(aliases)
        for name in os.listdir(wc.DEVPOD_PROJECTS_ROOT):
            path = os.path.join(wc.DEVPOD_PROJECTS_ROOT, name)
            if name not in live and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
    for alias in list(state):
        if alias not in aliases:
            del state[alias]


def main():
    pods = active_pods()
    aliases = [p["alias"] for p in pods]
    os.makedirs(wc.DEVPOD_PROJECTS_ROOT, exist_ok=True)
    state = load_state()
    synced = [p["alias"] for p in pods if sync_one(p, state)]
    prune_stale(aliases, state)
    save_state(state)
    print(f"synced {len(synced)}/{len(aliases)} devpod(s): {', '.join(synced) or '(none active)'}")


if __name__ == "__main__":
    main()
