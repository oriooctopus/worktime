---
name: worktime-fit-survey
description: >-
  Ask someone the questions that decide whether the worktime tracker could
  work on their setup at all, and write their answers to a file they can send
  back. Use when someone says "run the worktime survey", "worktime fit
  survey", "check if worktime would work for me", "I was sent a survey about
  a time tracker", or has been asked to fill one in before trying the tracker.
model: sonnet
user-invocable: true
disable-model-invocation: true
argument-hint: "[nothing needed — it will ask you questions]"
---

# Worktime — would this even work for you?

This is a **fit survey**, not setup. Nothing gets installed and nothing gets
configured. At the end you write one markdown file that the person answering
sends back to whoever asked them to run this.

The tracker works out when someone was actually working by fusing several
weak signals. Each signal has a hard dependency. This survey exists to find
out, before anyone spends an afternoon on it, which signals would exist on
this person's machine and which would be dead on arrival.

## Rules for how you talk during this

**Never use the internal vocabulary.** Not `gap_sec`, `dwell`, `the probe`,
`FOCUS_INCLUDE`, `events_for`, `launchd`. Say "break", "which app is in
front", "the tracker". If you catch yourself about to name a config key or a
file path, name the behaviour instead.

**One question at a time.** Use AskUserQuestion where there is a small set of
sensible answers — most of these have one. Free-text only where the answer is
genuinely open.

**Take "I don't know" for an answer.** Record it as unknown and move on. A
survey that stalls because somebody doesn't know which browser profile they
use is worse than a survey with a gap in it.

**Do not sell it.** If an answer means a big piece of this won't work for
them, say so plainly at the end. The whole point is to find that out cheaply.

**Apply the skip logic.** Several questions are pointless given an earlier
answer. Skipping them is required, not optional — the branches are marked
below. Never ask a question this survey has already ruled out.

## Step 0 — say what this is, before asking anything

Tell them, in about this much detail and in your own words:

> This is a short survey about your setup — about 15 questions, five minutes.
> Nothing gets installed and nothing changes on your computer.
>
> It's for an automatic time tracker that works out when you were actually
> working, so you don't have to log it by hand. It does that by looking at
> things like which app is in front of you, which sites you visited and when,
> what's on your calendar, and when you were typing to Claude Code. It reads
> times and addresses — not what you typed, not the contents of pages, emails
> or messages. Everything would stay on your own computer.
>
> Right now it only really exists for one person's setup. These questions work
> out how much of it would survive on yours. Answering "no" to things is
> useful — that's what the survey is for.
>
> At the end I'll write your answers to a file for you to send back.

## Step 1 — the questions

### A. The machine

**A0. Do you work for a company, or for yourself?**
An employer / self-employed, freelance or my own projects / a mix.

*(This one decides whether a whole group of questions gets asked. Half the
tracker's shortcuts for spotting work — an address with the company's name in
it, a separate work login, a work calendar — only exist for somebody with an
employer. Working for yourself doesn't make this harder, it makes it simpler:
there's no line between two accounts to draw, so the sites you name are the
whole answer.)*

- **If "self-employed" → skip A2, C2, C4, G2, F1's work/personal distinction,
  and don't ask about a work email or work calendar anywhere.** Ask C3 as the
  full answer instead of a supplement to a company-name rule. A2 and G2 both
  ask what an IT department allows, and there isn't one; asking anyway reads
  as not having listened to the answer they just gave. Record A2 as "their own
  machine" and G2 as "yes" without asking either.
- **If "a mix" → ask everything.** Somebody with a client laptop and their own
  has both situations at once, and which machine they mean is exactly what A2
  is for.

**A1. What kind of computer do you do most of your work on?**
macOS / Windows / Linux / a mix.

*(Why it matters: the always-on parts — which app is in front, the menu bar
indicator, the keyboard shortcuts, detecting that you're on a call — are all
Mac-only today. On Windows or Linux the tracker still works, but only from
Claude Code, browsing and calendar.)*

**A2.** *(skip if self-employed)* **Is that computer managed by your
employer?**
Yes with tight restrictions / yes but I can install things / no, it's mine.

*(Why: a locked-down Mac may refuse to run an app that isn't from the App
Store, or block the permission that lets it see which browser tab is open.)*

**A3. Do you work across more than one computer during the day?**
Just the one / a laptop and a desktop / a personal machine and a work-issued
one / more than that.

- **If "just the one" → skip A4.**

**A4.** *(only if more than one)* **When you switch machines, does anything
already sync files between them?** Something like Dropbox, iCloud, Obsidian
Sync, a shared drive, or a git repo you push to. Yes (which one) / no /
don't know.

### B. Claude Code — the load-bearing signal

**B1. How much of your working day involves Claude Code?**
Most of it / a few hours / occasionally / I don't use it.

*(This one matters more than any other question. It's the only signal that
needs nothing special installed and works on any operating system. If the
answer is "occasionally" or "not at all", the tracker is leaning entirely on
browsing and calendar, and this becomes a much weaker tool for you.)*

**B2. Is your work mostly at the keyboard, or is a lot of it meetings, calls
and time away from the desk?**
Mostly keyboard / roughly half and half / mostly meetings and calls.

### C. The browser

**C1. Which browser do you use for work?**
Chrome / Arc, Brave, Edge or another Chromium one / Safari / Firefox / a mix.

- **If Safari or Firefox only → skip C2, C3 and C4.** Say plainly: the
  browsing signal reads Chrome's own history database, so on Safari or
  Firefox there'd be no browsing signal at all.

**C2.** *(skip if self-employed)* **Do your work tools live at web addresses
with your company's name in them?** For example an internal wiki, a ticket
tracker, an SSO login page. Yes / mostly third-party tools under their own
names / a mix.

**C3.** **Which sites would mean, if you're on them, that you're working?**
Free text. Names are fine — "Substack", "Google Docs", "our Jira" — no need
for exact addresses. For somebody self-employed this is the whole of how the
tracker tells work from everything else, so push gently for a real list
rather than two examples.

**C4.** *(skip if self-employed)* **Do you have separate work and personal
accounts signed into the same Google account switcher?** So work Gmail and
personal Gmail both open in the same browser. Yes / no / I don't use Google
for work.

**C5. When you're working in the browser, which is it more like?**
Lots of quick jumping between tabs and pages / long stretches reading one
page — an article, a document, a thread — for twenty minutes or more / both,
depending on the day.

*(Ask this one carefully; it's the question most likely to be answered wrong
by reflex, because everybody feels like they jump around. Ask what the last
genuinely productive hour looked like.)*

*(Why it matters: the tracker normally notices you're working at the moment
you switch to something. That works for somebody who switches hundreds of
times an hour and fails completely for somebody who opens one long article
and reads it — that's one switch, and the next forty minutes look identical
to an empty room. There's a setting for this and it's off by default, so
getting this answer right is what decides whether their day comes back
whole.)*

### D. Desktop apps

**D1. Which of these do you use as an installed desktop app, rather than in a
browser tab?** Multi-select: Slack / Zoom / Teams / a code editor like VS
Code, Cursor or Zed / a meeting notetaker like Granola / GitHub Desktop /
none of these.

- **If A1 was not macOS → ask this anyway but note in the file that it is
  currently informational only**, since seeing which app is in front is
  Mac-only today.

**D2. Do you write or keep notes in a desktop app — Obsidian, Notion, Apple
Notes, something else?** Which one / no.

- **If yes → ask D3. If no → skip D3.**

**D3. Is that same app also where your personal life lives?** Shopping lists,
journal, plans with friends. Yes, it's all one place / no, work notes are
separate / I keep separate vaults or workspaces in the same app.

*(Why both questions: a notes app in front of you is either the strongest
signal there is that you're working, or no signal at all, and which one
depends entirely on this answer. Notes apps are excluded by default because
the one this was built on held meeting notes and a grocery list in the same
window — being in front couldn't say which. If work notes are their own app,
or their own vault, that objection doesn't apply and the app can be counted.
Say which of the two you're recording, because it's a setting somebody has to
turn on deliberately.)*

**D4. Is there anything else you use for both work and personal things in the
same window?** A browser, a chat app. Yes (which) / no.

### E. The always-on machine

**E1. Do you have a second computer that stays on and connected all day —
a desktop, a home server, a cloud dev box — separate from the one you're
working at?**
Yes / no / I have one but it isn't always on.

- **If "no" or "not always on" → skip E2 and E3, and don't dwell on it.**
  Note in the file that the background jobs would have to run on their main
  machine on a timer instead. That is a supported arrangement, just a
  different one, and it means those jobs only run while the machine is awake.

**E2.** *(only if yes)* **What does it run, and could a small script run on it
every few minutes?** Linux / a Mac / Windows / don't know.

**E3.** *(only if yes)* **Do you use Obsidian, with its sync turned on?**
Yes / I use Obsidian without sync / no.

*(Why: today the two machines don't talk to each other directly — one writes
a file, sync carries it to the other. Any file sync would do, but Obsidian's
is the one that's been tried.)*

### F. Calendar

**F1. Where does your work calendar live?**
Google Calendar / Outlook or Microsoft 365 / something else / I don't really
keep one.

- **If "I don't really keep one" → skip F2.**

**F2.** **Do your meetings actually appear on it, or do a lot of them happen
ad hoc?** Almost all are on the calendar / roughly half / most are unplanned.

### G. Comfort and permissions

**G1.** Read this out and get a straight answer:

> To do its job the tracker would look at, all on your own computer: the
> addresses and times of pages you visited, which app is in front of you,
> the times and titles of calendar events, and when you sent a message to
> Claude Code. It never reads what you typed, page contents, message
> contents, or email bodies. Nothing is uploaded.

**Is that something you'd be comfortable running?**
Yes / yes, except the browsing history / yes, but only if I can see and
delete what it stores / no.

- **If "no" → stop the survey here.** Thank them, write the file with what
  you have and mark it as declined. Don't argue and don't ask the rest.
- **If "except the browsing history" → note it and carry on**, but skip C2,
  C3 and C4 if you haven't asked them yet.

**G2.** *(skip if self-employed)* **On a work machine, are you allowed to
install things and grant permissions without asking IT?**
Yes / I'd have to ask / no.

### H. What they actually want

**H1. What would you use the output for?**
Timesheets or billing / knowing where my time actually goes / showing
someone else how much I worked / managing my own focus / just curious.

**H2. How precise does it need to be?**
To the nearest 15 minutes is fine / within about 5 minutes / I need it
close to exact.

**H3. How do you track this today?**
Not at all / a manual note or spreadsheet / a tool like Toggl or Harvest /
my employer's timesheet system.

**H4. Is there anything about how your day goes that you think would
confuse an automatic tracker?** Free text. Encourage a real answer here —
long stretches reading on paper, pair programming on someone else's machine,
shift work, two jobs, heavy phone use. This is the question most likely to
surface the thing nobody thought of.

## Step 2 — write the file

Write the answers to `worktime-fit-<their-first-name>-<YYYY-MM-DD>.md` in
the directory they ran this from. Use this shape:

```markdown
# Worktime fit survey — <name>

**Date:** <YYYY-MM-DD>
**Completed:** fully / stopped early at <question> / declined at G1

## Answers

### A. The machine
- **Main computer:** ...
- **Employer-managed:** ... *(skipped — self-employed, it's their own machine)*
- **More than one machine:** ...
- **File sync between them:** ... *(skipped — one machine)*

### B. Claude Code
...

(one section per group, one bullet per question, in the order asked.
Mark every skipped question explicitly as `*(skipped — <reason>)*` rather
than leaving it out. A missing line is ambiguous; a skipped one is data.)

## What would work on this setup

- **Claude Code prompts:** yes / no — <one line why>
- **Which app is in front:** ...
- **Browsing:** ...
- **Calendar:** ...
- **Menu bar indicator and shortcuts:** ...
- **Background jobs:** on a second machine / on the main machine on a timer

## Settings to turn on for this person

- **Long reading:** on / off — <why, from C5>
- **Count the notes app:** on (<app>) / off — <why, from D2 and D3>

## Overall

<two or three sentences, honest. Which signals survive, which don't, and
whether what's left would actually answer the question this person wants
answered — H1 and H2 are the test, not the count of working signals.>

## In their own words

> <verbatim answer to H4, and to C3 if given>
```

**Fill in "What would work" yourself** from their answers, using this map:

| Signal | Needs | Dies without it |
|---|---|---|
| Claude Code prompts | Claude Code, any OS | B1 is "don't use it" |
| Which app is in front | macOS, and the menu bar app installed | A1 not macOS |
| Menu bar dot, shortcuts, call detection | macOS, permission to install | A1 not macOS, or G2 is "no" |
| Browsing | Chrome or another Chromium browser | C1 is Safari/Firefox, or G1 excluded history |
| Company-name rule for internal tools | An employer, and tools on company-named addresses | A0 is self-employed, or C2 is "mostly third-party" |
| Telling work Google from personal | Two Google accounts in one browser | C4 is "no" — then neither counts |
| Calendar | Google Calendar | F1 is Outlook or none |
| Meetings as evidence | Meetings actually on the calendar | F2 is "most are unplanned" |

**Two settings the answers turn on.** Name them explicitly in the file — they
are off by default, and somebody who needs one and doesn't get it will
conclude the tracker doesn't work rather than that it wasn't switched on:

- **Long reading** (`long_read`) — turn on if C5 is "long stretches". Without
  it, a forty-minute read counts as one moment and the rest is silence.
- **Counting the notes app** (`focus_extra_apps`) — turn on if D2 names an
  app and D3 says work notes are separate from personal ones. If D3 says it's
  all one place, say plainly that it can't be counted and why.

For anybody self-employed, note that the only things separating work from
everything else are the sites they named in C3 and whichever of the two
settings above apply. There is no company name to fall back on, which makes
C3 worth a follow-up if their answer was thin.

**Be blunt in "Overall".** The two failure shapes worth naming out loud:

- Not macOS **and** little Claude Code — almost nothing is left. Say so.
- Precision needed (H2 "close to exact") while the signals are thin — the
  tracker will produce a number, and that number will be wrong in ways they
  can't see. That's worse than no tracker, and it should be said.

## Step 3 — hand it back

Tell them the file path and that they should send that file back to whoever
asked them to run this. Offer to read it out if they'd like to check what's
in it before sending — some of these answers are about their employer and
they're entitled to see exactly what they're passing on.

Don't offer to set the tracker up. That's a different conversation and it
starts after somebody has read this.
