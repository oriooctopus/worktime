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

## The live dot reads the same evidence differently

The day's arithmetic and the dot in the menu bar ask different questions of
the same log, and conflating them is what put the dot on "quiet 1m" eighteen
seconds after a message was typed into Slack.

`focus_for()` answers "which minutes were attended". It credits whole minutes,
and it credits the span BETWEEN two samples, so the newest sample is always
uncredited until the next heartbeat closes it. Together that is up to ~90s of
invented silence -- invisible against a five-minute cutoff, fatal against the
one-minute unfocused one.

`last_focus_input()` answers "when was somebody last here". A sample's `idle`
is measured backwards from it, so a row at 09:05:00 reading 45 is the complete
claim "somebody was at this machine at 09:04:15" -- a point in time, at second
resolution, needing no neighbouring row. That is the same shape as a prompt,
and the same arithmetic `idle_stretches()` uses to place the start of an
absence.

`last_slack_send()` is the same move applied to Slack. `search.messages` is
cached for four minutes because a network round trip cannot sit on a
five-second poll, so the desktop client's own console log carries the live
verdict instead -- measured against a day of real sends it lands about a
second BEFORE the API's timestamp. It sees strictly less than the API, and
every omission is wanted: a send from the phone or from a script holding the
same token leaves no line in this Mac's log.

Both are folded into `status()` and nowhere else, the same way the devpod
heartbeat is. They can only ever make the dot fresher -- never a period
longer or a day's total larger.

## Why foreground time replaced Slack sends

Sends were a bad proxy in the one direction that mattered. Reading half an hour
of a thread and answering nothing produced no evidence at all, so the Slack
stretches most likely to be real work were exactly the ones reported as gaps.
Foreground time sees the reading.

It is a more generous signal, so the day gets longer. Two things keep it
honest:

**Idle.** `CGEventSource.secondsSinceLastEventType` gives seconds since the
last input event of ANY kind -- keys, clicks, movement, scroll, gestures --
under the `~0` wildcard, so typing resets it exactly as the mouse does.
`FOCUS_IDLE_SEC` is 120.

This gate is currently OFF (`FOCUS_IDLE_GATES = False`): an app left frontmost
while nobody touches the machine still earns its minutes. It was switched off
along with the subtraction below, because between them idle had two separate
consequences -- time cut from the day, and time that quietly never arrived --
and only the first had a row anywhere explaining it. A minute that was never
credited looked identical to a minute that was never worked. Idle is binary
now: a stretch either is or isn't, and there is one place that says what that
means. The cost, accepted deliberately: a work app parked in the foreground
all afternoon accrues the afternoon.

**A heartbeat.** The sampler writes every 30 seconds, and the probe credits the
stretch between consecutive samples only while they stay under
`FOCUS_MAX_GAP_SEC` (90). Sleep, lock or a crash leaves a hole no sample
vouches for, rather than one row before lunch claiming the afternoon -- the
same trap `visit_duration` falls into below.

`FOCUS_INCLUDE` is an allow list: an app earns credit only by being named in
it. It began as an exclude list, on the theory that nearly everything in the
foreground of a work machine is the job -- true of the apps a person opens,
false of the machine they run on. macOS keeps putting things in front that
nobody chose: a notification alert, the lock screen, the printer dialog that
named a minute of one day "Add Printer". Each was found only after it had
inflated a day, because an exclude list is right only about the apps somebody
thought to name. The allow list's own cost is that a new tool earns nothing
until it is added -- a quieter failure, and one that errs low.

The terminal is left out for a different reason than the streaming apps.
It is not leisure, it is ambiguous: the same window is frontmost whether the
work is on this Mac or on the Linux desktop, and desktop work is subtracted
from Mac periods elsewhere in the probe. Counting its foreground time would
re-add the hours that subtraction exists to remove. Little is lost, because
real terminal work here is Claude Code and the prompts already say so.

Two things this gave up: a message sent from a phone no longer holds the dot
green, and focus is app-level only. Window titles -- which channel, which
document -- would need Accessibility permission, so the tracker stays at app
granularity rather than asking for it.

## Two traps worth knowing

**`visit_duration` is not attention.** It measures time until the tab navigated
away, so a parked tab accrues the full clock -- one tab here recorded 214
minutes. Single-visit dwell is therefore capped (`max_dwell_sec`).

**`total_foreground_duration` is the better signal but does not sync.** Chrome
records real foreground time locally and Chrome Sync strips it: on this setup
all 52,871 synced visits carry `-1` while the 9,741 local ones are populated.
Across local visits only 35% of recorded dwell was ever the focused tab. To use
it, the exporter has to run on the machine doing the browsing.

## Work sites beyond the list

Two rules in `profile.json` classify sites the `work` list would have to
enumerate one at a time:

- **`work_url_keywords`** (default `["rubrik"]`) — any address containing one
  of these words is work, which covers the wiki, the ticket tracker, the
  training site and the SSO portal without naming them. Matched against the
  address only, never the query string, so googling the company's name is not
  work.
- **`google_work_account`** (default unset) — the account number in Google
  URLs (`drive.google.com/drive/u/1/home` is account `1`). Set it when there is
  a separate work Google account: Gmail, Calendar, Drive and Docs under that
  number count as work, while the same sites under the personal account do
  not. With one Google account there is nothing to tell apart, so leave it
  unset.

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
- **Attended foreground time** — which app was frontmost and how long since the
  last mouse or key event, sampled every 5 seconds by the menu bar app into
  `~/.claude/stats/worktime/focus/<date>.jsonl`. This is what counts Slack
  time, and it replaced Slack sends as the presence signal (see below).
- **Slack messages sent** — still fetched from the Slack search API (token in
  `~/.slack-mcp-token.json`), but no longer evidence of presence. They survive
  only to name what a stretch was about in the tooltip and the `What` column.
- **Tool-approval events** — written by `bin/worktime-approval.py`
  (symlinked to `~/.claude/hooks/worktime-approval.py`, the Claude Code
  Notification/PostToolUse hook) to `~/.claude/stats/worktime/approvals.jsonl`.
  Approving a tool use is a human action; unattended agent activity is not.
- **GitHub Chrome visits** — code-review pages, read two ways. This Mac's own
  Chrome history is queried directly, about a minute behind the browser; the
  activity export (`~/Documents/Main/Dashboard/activity/<date>.md`) supplies
  earlier days and the Linux box's synced visits. Where both describe the same
  minute the live row wins. The live read exists because the export is written
  nightly, which made today the one day with no browser evidence at all — a
  morning of code review surfaced tomorrow. Reviewing a PR produces no prompts
  and no Slack messages, so without this it is invisible.
- **Linux desktop prompts** — also from the activity export. A Claude prompt
  on the other machine is evidence of *absence* from Rubrik work, and is
  subtracted from Mac work periods.
- **Manual marks** — written by `worktime-probe.py mark [note]`. The strongest
  signal: a human declaration overrides silence.
- **Calendar events** — from `~/Documents/Main/Dashboard/calendar-today.md`
  (written by `bin/calendar-export.py`). Work-tagged meetings extend presence
  across a quiet stretch.
- **Meeting cuts** — written by `worktime-probe.py meeting_end`. A meeting that
  finished early stops counting from that minute on, both for the live dot and
  for the day's total.
- **End of session** — written by `worktime-probe.py end_session [last]`. Closes
  any open mark *and* cuts any meeting still running, at one minute: this one,
  or (`last`) the last entry the tracker saw. It is the "that was the day"
  statement, and unlike `unmark` it does not assume a mark was ever started.

**How it decides working vs not working:**

A prompt run chains prompts closer together than `GAP_AFTER` (5 minutes in
focused mode) into one work period. The period starts 60 seconds before its
first prompt (80 seconds if it opens a new conversation) and ends 20 seconds
after its last one, floored at one minute total. In unfocused mode the gap
threshold ramps from 1 minute up to 5 over the first 10 minutes of the bout,
so a sparse stream of prompts between meetings doesn't inflate the day.

**Idle time is measured but currently NOT taken back out** (`IDLE_SUBTRACTS =
False` in `bin/worktime-probe.py`). The rule was: any stretch the machine went
untouched for longer than 2 minutes gets subtracted from the periods that span
it, back to the last real key or mouse event, with meetings and manual marks
exempt. It was switched off because HID idle counts only key and mouse events —
reading, a call, a long build and an empty room are indistinguishable to it —
so the cut removed far more time that was genuinely worked than time that
wasn't.

Everything that *observes* the absence still runs, which is the point: the
menu bar still raises its panel in the top right (the same `CountdownPanel` the
meeting-end prompt uses, 10 seconds to answer), pressing **I am here** still
writes a claim to `~/.claude/stats/worktime/idle-claims.jsonl` covering the
silence plus a 20-minute grace window, and Recent activity still shows
`away 3m (still counted)`. None of it changes the total. What it produces is
the evidence — how often the threshold fires, and how often a human says it was
wrong — needed to design a rule worth turning back on. Flip `IDLE_SUBTRACTS` to
`True` to restore the old behaviour; the subtraction code is untouched behind
it.

For the live dot (`status`), the verdict is:

1. **Green** — a prompt arrived or the machine was attended at the front of a
   work app within the current cutoff, OR a manual mark is active right now, OR
   a work calendar meeting covers the current minute.
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
| Green  | Working: recent prompt or attended foreground time, or a work meeting is live |
| Blue   | Manually marked as working |
| Amber  | Idle: no recent activity and no meeting |
| Red    | Probe failed to run or returned an error |

**Recent activity, raw or grouped:** the dropdown lists the evidence the dot's
verdict was derived from — the newest ten events, one line each. **Group into
sessions** (⌘G, remembered across launches) folds the whole day's events into
the work periods they happened in, one two-line row per period: its time range
and length, how many events it held, the tally by kind, and the period's
summary. Same rows, same boundaries as the period list — nothing is regrouped
by a second rule, and events in minutes no period covers keep a row of their
own marked `not counted` rather than disappearing from the tally.

Hovering any row in the raw list brackets every other row from the same
session, so the grouping the sessions view gives is readable without leaving
the list. Each session row opens a submenu (hover the `›`) listing its own events — up to
twenty of them, in the same three columns the raw list uses, ending in a line
saying how many the cap left out. Grouping is what makes the day readable and
also what puts the evidence out of reach; the submenu is the only place one
session's rows can be read, since toggling back gives the newest ten events of
the whole day rather than of that stretch. It opens beside the menu rather than
over it, which a tooltip could not do.

## Ending a meeting when the call ends

A scheduled meeting keeps the dot green until its scheduled end, so a half-hour
slot that broke up after ten minutes hands the day twenty minutes nobody
worked. The menu bar app closes that gap by watching the microphone.

Every two seconds it asks CoreAudio whether any input device is running
(`kAudioDevicePropertyDeviceIsRunningSomewhere`, filtered to devices that
actually have input streams — the built-in speakers report *running* at rest,
and without that filter the answer is permanently yes). This is a property
read, not a capture: no microphone permission, no orange recording dot, and no
knowledge of which app is on the call. Zoom, a browser tab, a phone app
screen-sharing — all the same reading.

The rules around that reading, in `bin/worktime-bar/CallDetector.swift`:

- capture must run **60 seconds** before it counts as a call, so Siri, a
  notification chime or a two-second mic test never end a meeting;
- silence must last **5 seconds** before the call is over. Not for the HAL,
  which clears the flag in about 0.23s, but for device handoff: AirPods dying
  mid-call hands over to the built-in mic with a gap in between.

When a call ends while a **calendar** meeting is live, a panel appears at the
top right — the meeting's name, "Ended — stopping tracking in 10s", and a
**Keep tracking** button. Left alone it runs `worktime-probe.py meeting_end`;
pressed, it does nothing at all. If capture resumes during those ten seconds
the panel withdraws itself, so stepping out to a second call is not a
decision the user has to make.

Only calendar meetings are ever ended this way. A manual mark is a human
declaration and a microphone reading does not get to revoke it; ordinary prompt
activity lapses on its own and needs no help.

It is a panel rather than a notification because a banner dismisses itself after
about five seconds — half the countdown — Focus suppresses delivery and a
meeting is exactly when Focus is on, and `UNUserNotificationCenter` would put a
permission prompt in front of a tracker that currently asks for nothing.

## Starting and ending a shift

**⌘⌥S** starts a shift, or ends the running one — the same action the menu's
**Start working** / **Stop working** row performs, which is why that row shows
the chord. Stopping ends the shift at the *last entry the tracker saw*, not at
the keypress: the chord gets pressed on the way out the door, and the minutes
between the last prompt and the press are the leaving, not the work. That end
carries the same `TAIL_SEC` the period model already adds after a last prompt,
so a stop and a walk-away credit the same final minute.

**End Session** sits below it and is always shown, whether or not a shift was
started — a scheduled meeting or a run of prompting keeps the day open with
nothing marked at all, and there was previously no menu item that meant "that
was the day" in either case. Its submenu names the two minutes rather than
picking one:

| | ends at |
|---|---|
| **End Now** | this minute |
| **End After Last Entry** | the last entry, plus `TAIL_SEC` — same rule as ⌘⌥S |

Either closes an open mark and cuts a meeting that would otherwise run past
that minute, in one step. A cut written when no meeting is running is inert:
`effective_meeting_end` only applies a cut to the meeting it landed inside.

## Building the menu bar app

`bin/worktime-bar/build.sh` compiles the sources into the bundle launchd runs
and re-signs it, then prints the `launchctl kickstart` line to restart it. The
app is several files now, so `swiftc main.swift` is no longer the whole build.

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
