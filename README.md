# worktime

Reconstructs when you were actually working, by fusing signals that each miss
something on their own: Claude Code prompts, calendar events, and classified
browsing.

The problem it solves: a meeting produces no prompts, and reviewing a pull
request for twenty minutes produces neither prompts nor a calendar event. To a
prompt-only probe both look identical to lunch.

## Layout

```
bin/    the three exporters (run on the machine with the data)
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

## Not yet in this repo

`worktime-probe.py` and the menu bar app live on the Mac and are not versioned
anywhere. They hold the actual working/not-working decision, so every statement
here about how a `work`-tagged row is treated is inference from behaviour, not
from reading the code. `skills/indicator-dot` mirrors the assumed rule and says
so when it might be wrong.

## Install

Copy `config/*.example.json` to `~/.config/`, symlink `bin/*.py` onto your
PATH, and install the units in `deploy/systemd` (or `deploy/launchd`).
Credentials are read from `~/.claude/tokens.env` and are never stored here.
