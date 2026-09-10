#!/bin/bash
# Refresh <dashboard>/calendar-today.md without any Google credentials on this
# machine.
#
# calendar-export.py talks to Google directly, which needs an OAuth client and
# a refresh token in ~/.claude/tokens.env. Those went missing, and re-minting
# them needs a browser consent screen nothing here can drive -- so the export
# silently stopped, the snapshot went three days stale, and the probe spent
# those days unable to tell a meeting from an empty chair. A 15-minute Zoom
# call on 2026-09-09 came back as four disconnected slivers of foreground.
#
# So the calendar is read the way the export brief always assumed it could be:
# by a Claude client that already holds a Calendar connection. Headless Claude
# Code reads the events; this script does everything else. The model's entire
# job is to transcribe what the connector returned into a fixed flat schema --
# it does no filtering, no merging and no formatting, all of which stay in
# calendar-export.py where they are tested. A model that invents a meeting can
# still cost the day an hour, which is why the prompt says to omit rather than
# guess and why --from-json refuses anything it cannot parse.
#
# Exit non-zero on any failure, leaving the previous file untouched: the probe
# already treats a snapshot over CAL_STALE_HOURS old as no calendar at all,
# which is the right degraded state. A half-written or invented one is not.
set -uo pipefail

# Resolved through symlinks, not just made absolute: this script is reached as
# ~/.claude/bin/calendar-refresh.sh, and calendar-export.py sits beside the real
# file in the repo, not beside the link. macOS ships bash 3.2 and a readlink
# with no -f, so python3 -- already a hard dependency below -- does it.
BIN="$(python3 -c 'import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "${BASH_SOURCE[0]}")"
CLAUDE="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

# The Calendar connection is scoped to a project directory, so where this runs
# decides whether it can read a calendar at all: from $HOME the same prompt
# comes back "I don't have access to Google Calendar tools in this
# environment". The probe launches this as a child and hands down whatever
# working directory the bar app happened to have, which is not the repo -- so
# the directory is pinned here rather than inherited.
cd "$BIN/.." || exit 1

# The profile, too, is pinned rather than inherited. The Google Calendar
# connector is on the personal profile only -- the default ~/.claude profile
# answers "I have eventkit-calendar, but no Google Calendar integration" -- and
# the bar app that starts this is launched by launchd, whose environment has no
# CLAUDE_CONFIG_DIR at all. So every refresh the probe launched ran against the
# default profile and failed, while the same script run from a terminal
# (where the variable is set) worked, which is why it looked fine by hand.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude-personal}"
TODAY="$(date +%Y-%m-%d)"
OUT="$(mktemp -t calendar-refresh)"
trap 'rm -f "$OUT" "$OUT.json" "$OUT.err"' EXIT

read -r -d '' PROMPT <<EOF
Use the Google Calendar tools to read every calendar's events for $TODAY
(local time), then output the result as JSON.

Steps:
1. Call list_calendars to get every calendar.
2. For each calendar, call list_events for $TODAY with startTime
   ${TODAY}T00:00:00 and endTime $(date -v+1d +%Y-%m-%d)T00:00:00. Each
   response carries an accessRole field -- record it.

Output ONLY a JSON object, no prose and no code fence:

{"date": "$TODAY",
 "calendars": [
   {"id": "<calendar id>", "accessRole": "<owner|writer|reader|freeBusyReader>",
    "events": [
      {"start": "<ISO 8601 with offset>", "end": "<ISO 8601 with offset>",
       "summary": "<title, or null if the calendar exposes none>",
       "status": "<confirmed|tentative|cancelled>",
       "transparency": "<transparent, or null>",
       "allDay": false,
       "myResponse": "<your own responseStatus, or null>"}
    ]}
 ]}

Rules:
- Copy start, end and summary EXACTLY as the tool returned them. Do not round
  times, translate time zones, or tidy titles.
- Include every calendar, even ones with no events (use an empty list).
- For an all-day event set allDay true and give start/end as bare YYYY-MM-DD.
- If a field is absent from the tool's response, use null. Never guess a value,
  and never invent an event: a meeting that is not real costs the day an hour
  of working time it did not have.
EOF

if ! "$CLAUDE" -p --model claude-haiku-4-5-20251001 --output-format text \
     "$PROMPT" >"$OUT" 2>"$OUT.err"; then
  # Both streams: `claude -p` reports its own failures -- "Not logged in ·
  # Please run /login" among them -- on stdout, so stderr alone came back empty
  # and the one line that named the problem was deleted by the trap.
  echo "calendar-refresh: claude failed: $(tail -c 500 "$OUT") $(tail -c 500 "$OUT.err")" >&2
  exit 1
fi

# The prompt asks for bare JSON, but a fence is the single most likely way for
# a model to disobey it and the cheapest to absorb. Anything else is a failure.
python3 - "$OUT" "$OUT.json" <<'PY' || exit 1
import json, re, sys
raw = open(sys.argv[1]).read()
m = re.search(r"\{.*\}", raw, re.S)
if not m:
    print(f"calendar-refresh: no JSON in model output: {raw[:300]!r}", file=sys.stderr)
    sys.exit(1)
try:
    doc = json.loads(m.group(0))
except ValueError as e:
    print(f"calendar-refresh: model output is not JSON: {e}", file=sys.stderr)
    sys.exit(1)
json.dump(doc, open(sys.argv[2], "w"))
PY

exec python3 "$BIN/calendar-export.py" --from-json "$OUT.json"
