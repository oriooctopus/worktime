# worktime

Reconstructs when you were actually working, by fusing signals that each miss
something on their own: Claude Code prompts, calendar events, and classified
browsing.

The problem it solves: a meeting produces no prompts, and reviewing a pull
request for twenty minutes produces neither prompts nor a calendar event. To a
prompt-only probe both look identical to lunch.

## Layout

```
bin/    exporters plus the probe, menu bar app, and the two Claude Code hooks
        (prompt-count.py, worktime-approval.py) it depends on
skills/ worktime-setup (guided config), indicator-dot (current status)
tests/  pytest suites; run with `python3 -m pytest tests`
deploy/ systemd units (Linux) and launchd plists (macOS)
config/ example configs only -- real ones live in ~/.config
```

Code lives here. **Data lives in Obsidian** and is never committed:
`Dashboard/calendar-today.md`, `Dashboard/activity/<date>.md`,
`Dashboard/worktime/<date>.json`.

## The three exporters

**`calendar-export.py`** writes `Dashboard/calendar-today.md`, the file the
probe reads. Pulls the personal account's own calendars plus a work free/busy
share. Work-calendar mirrors of personal events are dropped so each event
appears once. Applies hand corrections from `overrides.json` and merges derived
browsing blocks.

**`chrome-work-blocks.py`** turns Chrome history into work blocks. Each visit
becomes an interval and overlapping intervals are unioned, so a redirect chain
of eight hops in two seconds is one interval rather than eight units of work.

**`activity-export.py`** writes the daily activity table from WhatsApp,
iMessage, calls, Chrome and Claude Code, with cached LLM period summaries.

## Two traps worth knowing

**`visit_duration` is not attention.** It measures time until the tab navigated
away, so a parked tab accrues the full clock -- one tab here recorded 214
minutes. Single-visit dwell is therefore capped (`max_dwell_sec`).

**`total_foreground_duration` is the better signal but does not sync.** Chrome
records real foreground time locally and Chrome Sync strips it: on this setup
all 52,871 synced visits carry `-1` while the 9,741 local ones are populated.
Across local visits only 35% of recorded dwell was ever the focused tab. To use
it, the exporter has to run on the machine doing the browsing.

## Thresholds

In `work-domains.json`. Defaults: `gap_sec` 180 (browsing breaks run 4-7
minutes; a 12-minute threshold borrowed from prompt bouts swallows them all),
`max_dwell_sec` 600, `min_dwell_sec` 30, `min_block_sec` 180.

## Setting it up for a different person

`skills/worktime-setup` is a Claude Code skill that asks plain-language
questions -- what counts as work, what counts as personal, how long a pause is
a break -- and writes both config files. It reads the person's own most-visited
sites first so they pick from a real list rather than inventing one, then shows
them today's tracked stretches and asks whether that matches how their day
actually went.

It deliberately never says `gap_sec`, `dwell` or `blocks` to the user.

## Checking the current state from a terminal

```
dot            # green/amber right now, with what is covering it
dot --at 13:20 # any earlier moment today
```

Install it with
`ln -s "$PWD/skills/indicator-dot/indicator-dot.py" ~/.local/bin/dot`.
Exit code is 0 green, 1 amber, 2 unusable, so it composes in scripts. Colour is
used only when stdout is a terminal; piping or redirecting gives plain text.

## Web view

`web/server.py` serves the same status as a page, with today's timeline:
current dot state, what is covering the current moment, data freshness, and
every tracked stretch. Dependency-free stdlib server on port 8313, installed as
the `worktime-web` systemd unit and bound to 0.0.0.0 for the tailnet.

It imports `indicator-dot.py` rather than reimplementing the rule, so the page,
the `dot` command and the skill cannot drift apart.

## The probe (`bin/worktime-probe.py`)

The probe owns the working/not-working decision. The menu bar app
(`bin/worktime-bar/`) polls `worktime-probe.py status` every five seconds and
draws a coloured dot; it never recomputes state itself.

**Inputs the probe reads:**

- **Claude prompts** — timestamps from `bin/prompt-count.py`, symlinked to
  `~/.claude/hooks/prompt-count.py` (it also feeds the Claude Code statusline,
  which is why it lives at that fixed path rather than being called from
  `bin/` directly). Every prompt sent to Claude Code is evidence of presence,
  including from worktime's own background-job sessions: it walks both the
  normal `~/.claude/projects` and the separate `~/.claude-personal/projects`
  those run under.
- **Slack messages sent** — fetched from the Slack search API (token in
  `~/.slack-mcp-token.json`). Messages received say nothing about presence;
  only sends count.
- **Tool-approval events** — written by `bin/worktime-approval.py`
  (symlinked to `~/.claude/hooks/worktime-approval.py`, the Claude Code
  Notification/PostToolUse hook) to `~/.claude/stats/worktime/approvals.jsonl`.
  Approving a tool use is a human action; unattended agent activity is not.
- **GitHub Chrome visits** — code-review pages from
  `~/Documents/Main/Dashboard/activity/<date>.md` (the Linux box's activity
  export). Reviewing a PR produces no prompts and no Slack messages.
- **Linux desktop prompts** — also from the activity export. A Claude prompt
  on the other machine is evidence of *absence* from Rubrik work, and is
  subtracted from Mac work periods.
- **Manual marks** — written by `worktime-probe.py mark [note]`. The strongest
  signal: a human declaration overrides silence.
- **Calendar events** — from `~/Documents/Main/Dashboard/calendar-today.md`
  (written by `bin/calendar-export.py`). Work-tagged meetings extend presence
  across a quiet stretch.

**How it decides working vs not working:**

A prompt run chains prompts closer together than `GAP_AFTER` (5 minutes in
focused mode) into one work period. The period starts 60 seconds before its
first prompt (80 seconds if it opens a new conversation) and ends 20 seconds
after its last one, floored at one minute total. In unfocused mode the gap
threshold ramps from 1 minute up to 5 over the first 10 minutes of the bout,
so a sparse stream of prompts between meetings doesn't inflate the day.

For the live dot (`status`), the verdict is:

1. **Green** — a prompt or Slack send arrived within the current cutoff, OR a
   manual mark is active right now, OR a work calendar meeting covers the
   current minute.
2. **Blue** — a manual mark is active (shown distinctly so it's clear the green
   is asserted, not derived).
3. **Amber** — none of the above.

**How work vs personal calendar rows are treated:**

Rows tagged `work`, `rubrik`, or with an empty Calendar column count as work
meetings and extend presence. Rows tagged `personal` are visible in the
dashboard tooltip (they explain a quiet stretch) but do not contribute worked
time. A therapy appointment or football fixture is not time on the job.

**Schedule:** `worktime-probe.py check` writes a label record and updates the
Obsidian snapshot every 20 minutes (driven by launchd). The menu bar polls
`worktime-probe.py status` every 5 seconds; the status path is memoised
against a fingerprint of the input files and re-derives state only when
something actually changed.

**Menu bar dot states:**

| Colour | Meaning |
|--------|---------|
| Green  | Working: recent prompt or Slack activity, or a work meeting is live |
| Blue   | Manually marked as working |
| Amber  | Idle: no recent activity and no meeting |
| Red    | Probe failed to run or returned an error |

## Install

Copy `config/*.example.json` to `~/.config/`, symlink `bin/*.py` onto your
PATH, and install the units in `deploy/systemd` (or `deploy/launchd`).
Credentials are read from `~/.claude/tokens.env` and are never stored here.

Two of the `bin/` scripts are Claude Code hooks and must additionally be
symlinked into `~/.claude/hooks/` under their own names, since that fixed
path is hardcoded into `settings.json`'s hook config and (for
`prompt-count.py`) the global statusline:

```
ln -sf "$PWD/bin/prompt-count.py"     ~/.claude/hooks/prompt-count.py
ln -sf "$PWD/bin/worktime-approval.py" ~/.claude/hooks/worktime-approval.py
```
