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
skills/ worktime-setup (guided config), worktime-fit-survey (would it work
        for you at all), indicator-dot (current status)
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

Obsidian is left out despite being where the notes are, because the vault is
one app over two lives: the same window holds meeting notes and the grocery
list, and being frontmost cannot say which. Chrome has that problem too and
answers it per page rather than per app, but a note has no URL to test on, so
none of it counts.

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

## Two settings for a different kind of day

Both are off by default, both live in `profile.json`, and both exist because
the defaults above are claims about one person rather than about work.

**`long_read`.** Credit is the activation — the moment somebody reached for an
app — and that is sound here only because this is a day of ~569 switches, tight
enough for `GAP_AFTER` to chain them into periods. Somebody who opens one
article and reads it for forty minutes produces a single switch, and the
thirty-nine minutes after it are indistinguishable from an empty room. Enabled,
a stay keeps emitting events every `stride_sec` (240, deliberately under the
five-minute cutoff so they chain).

This is not the span model returning. That model credited the gap between two
samples because the samples existed, and samples arrive whether or not anybody
is in the chair — which is how an untouched Slack window billed forty-one
minutes. Every event here stands on its own row's `idle` reading instead, the
one number that climbs when the room empties. Scroll counts as input under the
`~0` wildcard, so a reader resets it on every flick of the wheel while a parked
tab crosses `max_idle_sec` (300) and stops earning. That distinction is
unavailable from history, which records navigations and so cannot see either.

**`focus_extra_apps`.** Adds bundle ids to `FOCUS_INCLUDE`. Obsidian is absent
from that list because *this* vault holds meeting notes and a grocery list in
one window — a fact about the vault, not about note-taking. A vault that is
only ever the job is the case the exclusion deletes, and this is how it says so.
It only ever adds; nothing can be removed through it, so a misconfiguration
cannot silently drop Slack.

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

Three rules in `profile.json` classify sites the `work` list would have to
enumerate one at a time:

- **`work_url_keywords`** (default `["rubrik"]`) — any address containing one
  of these words is work, which covers the wiki, the ticket tracker, the
  training site and the SSO portal without naming them. Matched against the
  address only, never the query string, so googling the company's name is not
  work.
- **`work_localhost_ports`** (default `[3000, 3001, 3002, 3003, 3004, 3005]`)
  — a dev server on one of these ports is the work app itself. Nothing in
  `localhost:3000/dashboard` says so: no company name, no account, so the port
  is the only evidence. Ports rather than all of `localhost`, because a
  personal side project served from its own port is not the job.
- **`google_work_account`** (default unset) — the account number in Google
  URLs (`drive.google.com/drive/u/1/home` is account `1`). Set it when there is
  a separate work Google account: Gmail, Calendar, Drive and Docs under that
  number count as work, while the same sites under the personal account do
  not. With one Google account there is nothing to tell apart, so leave it
  unset.

A third rule needs no configuration: `myworkday.com` is work for everyone,
whoever they work for. Workday serves every customer from the same host and
puts the tenant somewhere in the path, so whether the employer's name appears
at all is an accident of the page — and the export row for a task with a long
title truncates the address away entirely, leaving only the `- Workday` suffix
the title carries, which the probe matches too.

## Thresholds

In `work-domains.json`. Defaults: `gap_sec` 180 (browsing breaks run 4-7
minutes; a 12-minute threshold borrowed from prompt bouts swallows them all),
`max_dwell_sec` 600, `min_dwell_sec` 30, `min_block_sec` 180.

## Before setting it up for a different person

`skills/worktime-fit-survey` runs first, and answers a cheaper question than
the setup skill does: would any of this survive on their machine at all.

Only one signal here has no dependency — Claude Code prompts, which need
nothing installed and work on any OS. Everything else is conditional. Seeing
which app is in front, the menu bar dot, the shortcuts and call detection are
macOS-only. Browsing reads Chrome's own history database, so Safari and
Firefox produce nothing. The calendar path is Google's. Someone on Windows
who uses Claude Code twice a week has, in practice, no tracker — and that is
worth finding out in five minutes rather than after an afternoon of setup.

The survey asks about fifteen questions with skip logic, never installs or
changes anything, and writes one markdown file the person sends back. It is
deliberately willing to conclude "this won't work for you".

The second machine is the question people expect to fail on and mostly don't.
There is no heartbeat and no network call between the two boxes: one writes a
file, sync carries it, the other reads it. Without a second machine the same
jobs run on the main one on a timer, and the only thing lost is that they
don't run while it's asleep.

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
focused mode) into one work period. The period starts 20 seconds before its
first prompt and ends 20 seconds after its last one, floored at one minute
total. In unfocused mode the gap
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
the list. Each session row opens a panel beside the menu (hover the `‹`) listing its own events — up to
twenty of them, in the same three columns the raw list uses, ending in a line
saying how many the cap left out. Grouping is what makes the day readable and
also what puts the evidence out of reach; the panel is the only place one
session's rows can be read, since toggling back gives the newest ten events of
the whole day rather than of that stretch.

It is a panel the app places (`bin/worktime-bar/SessionPopover.swift`) rather
than a real submenu, for one reason: AppKit puts a submenu flush against its
parent and offers no offset, and the two windows meeting at a seam read as one
wide menu whose halves obeyed different rules. The cost is that it can't be
walked with the arrow keys — nothing in it is a target, so what's lost is
moving a selection through items that were never selectable.

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

**⌘E** makes either of them from anywhere: one press ends at this minute, two
within `END_DOUBLE_PRESS_SEC` end at the last entry. One press is End Now
because that is the common case — the ending you mean most of the time is the
minute you are in — and because it is the recoverable order. A single press
landing End Now claims a few minutes too many, which shows in the period list
and can be walked back; a single press that silently ended the day half an hour
ago deletes work nothing in the interface would show.

So the single press cannot act until the double press has been ruled out, the
same shape as ⌥W. Here the reason is sharper: acting at once and then again on
the second press would declare the day over twice, at two minutes, and the
second declaration cannot undo the first — `split_at_session_ends` cuts at
every minute in the file, so the stray End Now would go on breaking the period
at a minute nobody chose. The wait is 0.5s rather than ⌥W's 0.33 because that
key files a minute and this one ends the day.

Either ending posts a banner naming the minute *and* the rule
(`Session ended at 13:05 — the last entry.`). The dot going out looks the same
whichever minute it ended at, so the minute is the only thing that distinguishes
them — and a press meant for now that reads as a double press is invisible
without it. The minute is read back out of the probe rather than computed for
the banner, since `last_entry_end` is the only thing that knows what "the last
entry" resolved to.

The menu carries the chord on **End Now**, because that is what one press does;
the other row names the double press in its title, as a menu cannot express one
— the same reason "Track time…" carries no key equivalent for ⌥W twice. Being a
Carbon hot key, ⌘E is consumed before the frontmost app sees it — Finder's Eject
and "Use Selection for Find" lose it while the bar is running.

Either closes an open mark and cuts a meeting that would otherwise run past
that minute, in one step. A cut written when no meeting is running is inert:
`effective_meeting_end` only applies a cut to the meeting it landed inside.

And either records the minute itself, in `session-end.json`, which for a long
time it did not — so on the ordinary afternoon the item exists for, with nothing
marked and nothing scheduled, both of the steps above were inert and the click
changed nothing anybody could see. The dot stayed green, because prompting was
what was holding it green, and the period ran straight on through the minute the
day had just been declared over at: the menu showed `08:34–08:46 · now` on a
session ended at 08:40. The declaration is a record in its own right now, and it
does two things nothing else could:

- **It breaks the period there.** `split_at_session_ends` cuts the merged spans
  at the declared minute, after the merge so the rejoining cannot undo it. A
  split, not a truncation — minutes worked after the declaration are still work,
  and the day total is unchanged by clicking it. The piece after a break is
  dropped when no event falls inside it, because that piece is the `TAIL_SEC`
  buffer and publishing it would leave a phantom period made of nothing.
- **It puts the dot out.** `status` reads it below the mark and the meeting and
  above the quiet cutoff: a declaration ends the run it was made in, but it is
  not a lock on the rest of the day. Starting a mark, a meeting beginning, or
  simply prompting again all speak for themselves afterwards.

**Link with Last Session** is the opposite statement: not that a stretch is
over, but that it never stopped. A step away the tracker saw nothing in — a
corridor conversation, a whiteboard, a call taken on the phone — comes back as
two sessions with a hole between them, and nothing in the menu could say the
hole was work. `mark` starts at the current minute, so it could claim from the
click forward but never the stretch already behind it, which is the only part
that needs claiming.

So the mark this writes starts in the past, at the minute the last session
ended, and is left open — the stretch it rejoined is still going, which is why
the row was clicked. It closes the way any mark does: the next prompt resolves
it, ⌘⌥S stops it, End Session ends it.

Which period counts as "the last session" depends on whether one is live.
Prompting right now means the newest period *is* the current one, so the claim
reaches past it to the period before and the two merge; with nothing live the
newest period is itself the last one and the claim carries it to now. That
decision is `link_anchor`, and the row's title names the minute it returns
(`Link with Last Session (from 11:40)`) — the same value `status` publishes as
`link_from`, so the menu cannot advertise one minute and bank another. It greys
out when there is nothing to link to, which is the ordinary state of the first
session of the morning.

A mark already running is not, by itself, one of those states. It used to be —
any open mark refused the click, on the reasoning that an open mark holds the
day open so there is no gap left to bridge. That holds only for a mark that
began before the last session ended. Mark as working on sitting back down and
*then* reach for the link, which is the obvious order to do those two things
in, and the mark starts after the anchor: it holds the stretch in front of it
and leaves the gap behind it exactly as unclaimed as if no mark were running.
The item was offered anyway, named a minute, and banked nothing. Now the span
written in that case is closed rather than open, ending where the running mark
begins — it fills the hole and nothing more, and the day stays open on the mark
that was already open instead of on a second one beside it.

The 30-minute ceiling on an open mark is measured from the minute the mark was
**made**, not the minute it starts. The two are the same for a mark claiming
time from the click forward, so this is invisible in the ordinary case — but a
backdated mark (a link, or `mark HH:MM`) measured from its start spends the
allowance on minutes that had already elapsed before it existed: a 35-minute
gap linked at 12:15 would reach only 12:10, and one backdated an hour would
expire before it was written.

## ⌥W, once and twice

**⌥W** logs an entry: one keypress saying this minute was worked, for work this
machine has no way to see. Nothing opens and nothing is asked, which is the
whole value of it — a dialog that took the caret to ask what you were doing
would interrupt the work it is trying to record. It writes a point event to
`notes.jsonl`, and the ordinary chaining rule joins it to whatever is around it.

**⌥W twice** opens the one window this app has, because the other case needs a
number. A phone call that just ended has a length you already know and minutes
that are already behind you, and the single press cannot say either. The panel
asks how many minutes, and what to do about work already counted inside them:

| | claims |
|---|---|
| **Stop at the last tracked session** | only the minutes between that session and now |
| **Split — put the rest before it** | those, plus the remainder in the free minutes before it |

Five minutes asked for with a Slack message sent two minutes ago banks two
under the first rule and 2 + 3 under the second — the same total, placed where
there was actually a hole to put it in. The first is the default because it is
the reading that cannot overstate the day, and the banner names what landed
(`Tracked 2m of 5m`) because the difference between the two rules is invisible
in the period list afterwards.

Neither rule ever claims a minute that is already counted. Periods are unioned,
so an overlapping claim would not lengthen the day — it would silently shorten
the claim, and the minutes that went missing would be exactly the ones being
recorded. `split` walks back over as many periods as it needs to place the
remainder, not just the first; stopping at the second would drop the rest with
nothing said about it.

The spans are written as ordinary closed marks, so nothing downstream needs to
know the command exists. Closed, not open, because the stretch is over — that
is why it needed claiming by hand — and an open mark would go on crediting
minutes forward from a call that has already ended.

The cost of the double press is that the single one now waits `DOUBLE_PRESS_SEC`
(a third of a second) to find out whether a second is coming. Acting immediately
and *also* opening the panel would need no delay, but it would file a stray
entry every time somebody meant to open the panel, at a minute they did not mean
to claim and with nothing to distinguish it from a real one.

## Building the menu bar app

`bin/worktime-bar/build.sh` compiles the sources into the bundle launchd runs
and re-signs it, then prints the `launchctl kickstart` line to restart it. The
app is several files now, so `swiftc main.swift` is no longer the whole build.

### A red dot that outlasts one poll is usually a stalled read, not a bug

Red means one thing: the probe did not answer inside `PROBE_TIMEOUT_SEC`. One
way that happens is worth writing down, because nothing about it looks like
permissions from the outside.

What is measured: `open()` on anything under `~/Documents`, from a child of
this app, does not fail -- it BLOCKS. `/tmp` and `~/.claude` opened in 0.00s in
the same process where `~/Documents/...` never returned at all and took the 30s
kill with it. What is inferred: that macOS was withholding a
Files-and-Folders decision, the signature being ad-hoc and every build giving
the app a new identity to decide about. That is the leading suspect and not
more -- it began within minutes of a relaunch and cleared on its own, and a
later rebuild did not reproduce it, so the trigger is not pinned down. Since
the probe itself lives under
`~/Documents` (the `~/.claude/bin` symlink points into this repo) every poll
died in its first `open`, five polls a minute, for as long as the decision was
pending. The bar's log fills with `probe ["status"] exit 15` and nothing else,
because there is no stderr from a process killed before it ran a line.

It clears on its own once the decision lands, and then the same paths open in
0.00s again. If it does not clear, grant the app access to the folder (or Full
Disk Access) in System Settings -> Privacy & Security. If it turns out to
recur per build, a stable signing identity is the fix that would end it, at the
cost of needing one in the keychain.

`exit 15` (SIGTERM, the watchdog) and `exit 1` (a traceback) are worth telling
apart in that log: 15 is a stall like this one, 1 is the probe genuinely
failing and the traceback says where.

The menu now makes that distinction without the log. The bar itself stays a
bare dot -- it is shared with a dozen other icons, and what a failure needs
said is more than a word's worth -- but opening it gives the failing poll's own
account of itself:

```
● URLError: <urlopen error [Errno 8] nodename nor servname provided…
    /Users/oliver/.claude/bin/worktime-probe.py status
    exit 1 after 0.3s
    raised in request.py:1324 do_open
    reached from worktime-probe.py:639 _slack_fetch
    URLError: <urlopen error [Errno 8] nodename
    nor servname provided, or not known>
    3 polls failed since 00:01, last good 23:58
  Copy Probe Diagnostics
  Open Bar Log
```

Two frames rather than one, because they answer different questions: the raise
says what went wrong, and the probe's own frame says which of the five things
it gathers was being gathered at the time -- Slack, above, which is the
difference between "the network" and "the network, and the rest of the day is
still readable". A stall has no frames and says so instead: `killed at the 30s
watchdog — stalled, no traceback`. Times are on the clock rather than in ages,
so the start of a run of failures can be lined up against a sleep, a network
drop or a rebuild. `Copy Probe Diagnostics` puts the whole failure on the
clipboard, since three rows is less than a traceback; `Open Bar Log` opens
`/tmp/worktime-bar.err`, which is where a run of failures is read rather than
the current one. The afternoon that produced the section above was spent
working out from `/tmp` which of the two failures was on the dot, which is
exactly the question the menu now answers on sight.

## Install

Copy `config/*.example.json` to `~/.config/`, symlink `bin/*.py` onto your
PATH, and install the units in `deploy/systemd` (or `deploy/launchd`).
Credentials are read from `~/.claude/tokens.env` and are never stored here.

Three of the `bin/` scripts are Claude Code hooks and must additionally be
symlinked into `~/.claude/hooks/` under their own names, since that fixed
path is hardcoded into `settings.json`'s hook config and (for
`prompt-count.py`) the global statusline:

```
ln -sf "$PWD/bin/prompt-count.py"        ~/.claude/hooks/prompt-count.py
ln -sf "$PWD/bin/worktime-approval.py"   ~/.claude/hooks/worktime-approval.py
ln -sf "$PWD/bin/worktime-prompt-mark.py" ~/.claude/hooks/worktime-prompt-mark.py
```

`worktime-prompt-mark.py` must then be registered as a `UserPromptSubmit`
hook in **every** Claude Code profile on the machine — this one runs three
(`~/.claude`, `~/.claude-personal` for worktime's own background jobs, and
`~/.claude-bench`), and each keeps its own `settings.json`:

```json
"UserPromptSubmit": [
  {"hooks": [{"type": "command",
              "command": "python3 ~/.claude/hooks/worktime-prompt-mark.py"}]}
]
```

The probe refuses to run if any profile it reads transcripts from is missing
that registration. It has to: all the profiles share one mark file, so an
unhooked profile does not break anything visible — its prompts just stop
counting, and the day quietly reads as emptier than it was.

The hook writes two things under `~/.claude/stats/worktime/`:

- `prompt-mark.json` — overwritten every prompt. The probe stats it to decide
  whether the cached day is still good, which is the whole freshness check.
- `prompt-index/<day>.jsonl` — appended every prompt, recording which
  transcript it landed in, plus a `since` file marking the first day the index
  was live. `prompt-count.py` opens the transcripts a day's index names rather
  than walking every transcript on the machine to find them; days at or before
  `since` predate the index and are still walked.

Neither file holds prompt text — the index records only *where* to look, so
every decision about what counts as a human at a keyboard (the `entrypoint`
filter and the rest of `prompts_with_time`) stays in one place.

`bin/done-daily.sh` wants the same treatment for the same reason — its
LaunchAgent hardcodes `$HOME/.claude/bin/done-daily.sh`, so the agent finds
the script through the symlink rather than through this checkout's path:

```
ln -sf "$PWD/bin/done-daily.sh" ~/.claude/bin/done-daily.sh
cp deploy/launchd/com.oullman.done-daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.oullman.done-daily.plist
```

**Grant Full Disk Access to `/bin/zsh` before loading it.** A launchd agent
gets none of the Documents access a terminal session inherits, and both halves
of this job live under `~/Documents`: the symlink above resolves into this
checkout, and the notes it writes go to the Obsidian vault. Without the grant
the agent cannot open the script at all (exit 127), and even reached directly
it could not see the vault — `ls` fails and the `*.md` glob returns zero
against a directory full of notes. That second failure is what made the job
re-run the same day every 30 minutes for a week: `have_note` read a refused
directory as an empty one. `vault_readable()` now refuses the tick and says so
rather than running blind, so the symptom is a quiet log line instead of a
bill, but the grant is what actually makes the job work.

System Settings → Privacy & Security → Full Disk Access → add `/bin/zsh`
(⌘⇧G in the file picker to type the path).
