#!/usr/bin/env python3
"""Record WHEN a permission prompt was answered -- not when it was raised.

Two hooks, because one is not enough:

  ask   (Notification)  -- Claude Code needs a decision. Writes a pending
                           marker. This timestamp is NOT evidence of presence:
                           the prompt can sit untouched for an hour while
                           nobody is at the desk, and taking it as an event
                           would credit the whole absence as work.

  done  (PostToolUse)   -- a tool finished. If a decision was pending FOR THAT
                           SESSION, the human must have granted it to get here,
                           so this is the moment they were at the keyboard.

The transcript cannot supply this on its own: an approved tool and an
unattended one both land as an identical `tool_result` row, with no field
distinguishing them, so reading approvals out of the transcript would count
autonomous agent activity as human presence.

MATCHING. These hooks are global -- every session, every subagent, every
background job on this machine fires them into one process. A single unkeyed
marker would be consumed by whichever tool happened to finish next, which in
practice is an unrelated auto-approved tool in another window, recording an
approval that never happened. Two things pin it down:

  session_id  -- a marker is only consumable by a `done` from the same
                 session. Within one session Claude is blocked while the
                 prompt is open, so the next tool to complete there really is
                 the approved one.
  tool name   -- when the Notification message names the tool, a `done` for a
                 different tool leaves the marker alone rather than eating it.

The click time is estimated as the tool's finish time minus how long the tool
ran, which is much closer to the moment of the click than the finish alone.

Known gap: a DENIED tool never reaches PostToolUse, so denials go unrecorded.
A denial is a keystroke like any other, but no hook fires on refusal.

Failures are swallowed on purpose: this is instrumentation attached to an
interactive permission prompt, and a bug here must never be able to stop the
user from approving something.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

# realpath, not abspath: this file is reached through the
# ~/.claude/hooks/worktime-approval.py symlink, and abspath would look for the
# shared module in ~/.claude/hooks, where it is not.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktime_common as wc  # noqa: E402

LOCAL = wc.local_tz()
STATE = os.path.expanduser("~/.claude/stats/worktime")
OUT = os.path.join(STATE, "approvals.jsonl")
PENDING_DIR = os.path.join(STATE, "approval-pending")

# Answered after this long, the click is still real but says nothing about the
# stretch before it, so nothing is backdated. Also the age at which an
# abandoned marker (prompt dismissed, session killed) is swept.
STALE_SEC = 6 * 3600

TOOL_IN_MESSAGE = re.compile(r"permission to use (\w+)", re.I)

# Notification does NOT fire only for permission prompts. Claude Code also
# sends an IDLE nudge through it:
#
#   sendIdleNotification: () => xm({ message: "Claude is waiting for your
#                                    input", notificationType: "idle_prompt" })
#     -- claude 2.1.247 bundle
#
# That is the worst possible thing to mistake for an approval: the idle nudge
# fires precisely BECAUSE nobody has touched the keyboard for a while, so
# treating it as a pending decision would plant a marker during an absence and
# let the next unattended tool cash it in as presence -- manufacturing work out
# of exactly the minutes the tracker exists to catch as a gap.
#
# So a marker is planted only for a message that actually asks permission. The
# two permission wordings in that bundle are "Claude needs your permission to
# use <Tool>" and a bare "Claude needs your permission".
PERMISSION_MESSAGE = re.compile(r"needs your permission", re.I)
IDLE_TYPES = {"idle_prompt"}


def marker_for(session: str, agent: str) -> str:
    """Markers are keyed by session AND agent.

    A subagent's PostToolUse carries the PARENT's session_id -- verified by
    logging the raw payloads: a general-purpose subagent's Bash reported the
    same session_id as the main thread, differing only in `agent_type`. So
    session keying alone does not isolate them, and a subagent finishing a tool
    while a permission prompt was open in the main thread would consume the
    marker and record an approval nobody gave.
    """
    key = f"{session or 'unknown'}.{agent or 'claude'}"
    return os.path.join(PENDING_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")


def sweep() -> None:
    now = time.time()
    for f in os.listdir(PENDING_DIR):
        p = os.path.join(PENDING_DIR, f)
        if now - os.path.getmtime(p) > STALE_SEC:
            os.remove(p)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ask"
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}

    try:
        now = datetime.now(timezone.utc).astimezone(LOCAL)
        os.makedirs(PENDING_DIR, exist_ok=True)
        session = payload.get("session_id") or "unknown"
        agent = payload.get("agent_type") or "claude"
        path = marker_for(session, agent)

        if mode == "ask":
            sweep()
            msg = f"{payload.get('message', '')} {payload.get('title', '')}"
            if payload.get("notificationType") in IDLE_TYPES:
                return
            if not PERMISSION_MESSAGE.search(msg):
                return
            m = TOOL_IN_MESSAGE.search(msg)
            with open(path, "w") as fh:
                json.dump({"at": now.isoformat(),
                           "tool": m.group(1) if m else None}, fh)
            return

        if not os.path.exists(path):
            return                      # nothing pending HERE; ran unattended
        with open(path) as fh:
            pend = json.load(fh)
        want = pend.get("tool")
        got = payload.get("tool_name")
        if want and got and want.lower() != got.lower():
            return                      # a different tool; leave it pending

        os.remove(path)
        asked = datetime.fromisoformat(pend["at"])
        waited = (now - asked).total_seconds()
        if waited > STALE_SEC:
            return

        # The click happened when the tool STARTED, not when it finished.
        ran_ms = payload.get("duration_ms") or 0
        clicked = now - timedelta(milliseconds=min(ran_ms, waited * 1000))
        with open(OUT, "a") as fh:
            fh.write(json.dumps({
                "day": clicked.strftime("%Y-%m-%d"),
                "t": clicked.strftime("%H:%M:%S"),
                "tool": got,
                "waited_sec": round(waited),
                "session": session[:8],
                "agent": agent,
            }) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
