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

> This is a short survey about your setup — about 20 questions, five or ten
> minutes. Nothing gets installed and nothing changes on your computer.
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

Then explain how it decides you're working, because several questions later on
make no sense without it and people answer them wrong by guessing:

> The way it works is worth thirty seconds, because a few of the questions
> depend on it.
>
> It doesn't watch a clock running. It notices *moments* — you sent a message
> to Claude Code, you opened a work page, you switched to Slack. Each moment
> on its own is just an instant, so around each one it holds the clock open
> for a few minutes afterwards. Moments close together join into one stretch,
> and that stretch is what gets counted as working.
>
> So a busy hour of jumping between things comes back as a solid hour, because
> the moments keep landing before the previous one runs out. And a gap only
> becomes a gap when nothing happens for long enough that the last moment
> expires.
>
> The catch, and this is what a couple of questions are about: reading one long
> page is a single moment. You switch to the article once and then you're just
> reading. Nothing else happens, so after a few minutes it looks the same as
> an empty room, and you can lose forty minutes of real work that way. There's
> a setting that fixes it — that's why I'll ask what your browsing is like.

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

- **If "self-employed" → skip A2, C2, C4, G1, F1's work/personal distinction,
  and don't ask about a work email or work calendar anywhere.** Ask C3 as the
  full answer instead of a supplement to a company-name rule. A2 and G1 both
  ask what an IT department allows, and there isn't one; asking anyway reads
  as not having listened to the answer they just gave. Record A2 as "their own
  machine" and G1 as "yes" without asking either.
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

**A3. Do you do any of your work on a device other than that computer?**
Count anything where real work happens, not just where the typing happens —
a second laptop or desktop, but also reading on a tablet, or a serious stretch
of email and messages on your phone.
Just the one computer / a laptop and a desktop / a personal machine and a
work-issued one / a phone or tablet as well / more than that.

- **If a phone or tablet is named → ask A3b.**
- **If only ever the one computer → skip A4.** Otherwise ask A4 about the
  other *computers* only; a phone doesn't sync anything the tracker can use.

**A3b.** *(only if a phone or tablet was named)* **Roughly how much of your
working day happens on it?** Minutes here and there / a real chunk most days,
half an hour or more / some days it's most of my work.

*(Why: nothing here can see a phone or a tablet, and nothing is going to.
Work done on one is invisible — it shows up as a gap, so an hour of email on
the sofa reads as an hour off. That's fine when it's a few minutes, and it
quietly makes the totals wrong when it's habitual. Record the answer plainly
in the file; if it's "most of my work", say so in the overall verdict rather
than burying it, because no setting fixes it.)*

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

This is the most important answer in section C, and the hardest to give from
memory — people name three sites and forget the ten they actually use. So
offer to look first, and only fall back to asking them to remember.

**Offer it like this:**

> Rather than have you try to remember, I can look at which sites you've
> actually spent time on recently and read you back the top twenty or so, and
> you just tell me which ones are work. It's the same list your browser's
> history page shows you. Want me to?

- **If yes → read the history and build the list.** Chrome keeps it in a
  SQLite file which it holds a lock on, so copy it first and query the copy.

  **Find the profile by scanning; never hardcode `Default`.** Plenty of
  machines have no `Default` directory at all — the Mac this was built on has
  only `Profile 2` — and a hardcoded path there reads an empty history
  forever while looking exactly like somebody who does not browse. Take the
  most recently written one:

  ```bash
  ls -t ~/Library/Application\ Support/Google/Chrome/*/History | head -1
  ```

  On Linux that root is `~/.config/google-chrome/` instead. Copy the file it
  names to a scratch path, then:

  ```sql
  SELECT substr(substr(url, instr(url,'://')+3), 1,
                instr(substr(url, instr(url,'://')+3)||'/','/')-1) AS d,
         sum(visit_count) v
    FROM urls
   WHERE last_visit_time > (strftime('%s','now','-30 days')+11644473600)*1000000
   GROUP BY d HAVING v > 4 ORDER BY v DESC LIMIT 40;
  ```

  The window is thirty days and the unit is visits rather than distinct pages,
  so a site they open constantly outranks one they once crawled through. The
  epoch offset is Chrome's: it counts microseconds from 1601, not 1970. Delete
  the copy when you're done with it.

  Collapse what you get to bare domains, drop the obvious noise (search
  engines, the new-tab page, anything with one or two visits), and present the
  top fifteen to twenty as a numbered list. Then ask them to sort it: **which
  of these mean you're working?** Let them answer by number, in a bundle, or
  with "all of these except 4 and 9".

  Read the file, and do not do anything else with it. No sampling of page
  titles, no times of day, no reading what they searched for. It is a list of
  domains for them to sort and nothing else, and the file goes to `/tmp` and
  is deleted in the same command.

- **If they'd rather not, or it fails, or they don't use Chrome → just ask.**
  Free text. Names are fine — "Substack", "Google Docs", "our Jira" — no need
  for exact addresses. Push gently for a real list rather than two examples.

- **Either way, ask the follow-up the history cannot answer:** *is there
  anything you'd count as work that you haven't been to recently?* A quarterly
  invoicing tool won't show up in a fortnight of history and would go missing
  from the list forever.

For somebody self-employed this list is the whole of how the tracker tells
work from everything else — there's no company-name shortcut behind it — so
spend the extra minute here.

**C4.** *(skip if self-employed)* **Do you have separate work and personal
accounts signed into the same Google account switcher?** So work Gmail and
personal Gmail both open in the same browser. Yes / no / I don't use Google
for work.

**C5.** Point back at what you explained at the start, then ask:

> Remember how it counts moments, and how sitting still on one long page
> stops producing them? **Think about the last hour of real work you did in
> the browser. Was it lots of little moves — opening things, switching tabs,
> clicking through — or was most of it spent on one page, reading?**

Lots of quick moves / mostly one page at a time, twenty minutes or more /
genuinely both, depending on the day.

*(Ask this one carefully; it's the question most likely to be answered wrong
by reflex, because everybody feels like they jump around. Anchoring it to a
specific remembered hour is what gets a real answer — "how do you browse" in
the abstract gets "oh, all over the place" from people who in fact read one
document all afternoon. If they say "both", ask which one the *good* days
look like, and record that.)*

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

**D3.** Name the app they gave in D2 and ask it concretely:

> **If I counted every minute [Obsidian] is open in front of you as working
> time, would that be about right — or would it sweep in a lot of personal
> time too?** Some people keep everything in one place: work notes next to
> the shopping list and the holiday planning.

About right, it's basically all work / it'd sweep in a lot of personal
things / it's mixed, but work and personal are in separate vaults or
workspaces.

*(Ask it this way round — as "would counting it be right" rather than "is
your personal life in there" — because that is literally the decision being
made, and people answer it accurately. Asked the abstract way it gets a
puzzled "…sort of?", which decides nothing.)*

*(Why it matters: a notes app in front of you is either the strongest signal
there is that you're working, or no signal at all, and which one depends
entirely on this answer. Notes apps are left out by default because the vault
this was built against held meeting notes and a grocery list in the same
window, so being in front couldn't say which. Their own app, or their own
vault, and that objection doesn't apply and it can be counted. Record which
of the three, because turning it on is a deliberate setting.)*

**D4. Is there anything else you use for both work and personal things in the
same window?** A browser, a chat app. Yes (which) / no.

### E. The always-on machine

**E1.** Explain before asking, because "always-on machine" means nothing to
most people and the honest answer for nearly everybody is no:

> Some of the work happens on a schedule rather than while you're at the
> keyboard — every few minutes something totals up the day so far and writes
> it down. That's what keeps yesterday's numbers there when you come back to
> them.
>
> That can just run on your laptop, and for most people it does. The only
> catch is that a laptop sleeps: shut the lid at six and nothing runs again
> until you open it. It catches up when you do, so nothing is lost — it just
> means the day's file is stale while the machine is asleep. If some other
> computer happens to be sitting there switched on all the time, that job can
> live there instead and the numbers stay current.
>
> **So: is there another computer in your life that's just on all day — a
> desktop you don't shut down, a home server, a Raspberry Pi, a cloud box you
> rent?** Most people don't, and that's completely fine.

Yes / no / I have one but it isn't reliably on.

- **If "no" or "not reliably on" → skip E2 and E3, and don't dwell on it.**
  Record in the file that the scheduled jobs run on their main machine and
  pause while it's asleep. This is a supported arrangement and the common one,
  not a shortcoming — do not write it up as a gap.

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

### G. Permissions

*(There is no privacy question here on purpose. What the tracker reads is
already said plainly in the opening, which is where it belongs — a survey
that stops to ask permission again mid-way starts to sound like it expects to
be refused. If they raise a concern themselves, answer it straight, record it
in the file, and carry on.)*

**G1.** *(skip if self-employed)* **On a work machine, are you allowed to
install things and grant permissions without asking IT?**
Yes / I'd have to ask / no.

### H. What they actually want

**H1. How precise does it need to be?**
To the nearest 15 minutes is fine / within about 5 minutes / I need it
close to exact.

**H2.** The question behind this one is whether they'd ever be able to tell
that it was wrong:

> **Say it told you that yesterday you worked five hours and forty minutes.
> Is there anything you could check that against — an invoice, a timesheet, a
> note you keep — or would you just have to take its word for it?**

I could check it against something (what) / I'd have a rough sense and would
notice if it were badly off / I'd have no idea, I'd just believe it.

*(Asked instead of "how do you track your time today", which gets "I don't"
from most people and settles nothing. This version is the useful one: a
number nobody can check is a number that gets trusted while it's wrong, and
if they have something to compare against then the first week has an obvious
shape — run both, see whether they agree. Note what they'd compare against,
by name.)*

**H3. Is there anything about how your day goes that you think would
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
**Completed:** fully / stopped early at <question>

## Answers

### A. The machine
- **Main computer:** ...
- **Employer-managed:** ... *(skipped — self-employed, it's their own machine)*
- **Other devices used for work:** ...
- **How much work happens on a phone or tablet:** ... *(skipped — none named)*
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

> <verbatim answer to H3, and to C3 if given>
```

**Fill in "What would work" yourself** from their answers, using this map:

| Signal | Needs | Dies without it |
|---|---|---|
| Claude Code prompts | Claude Code, any OS | B1 is "don't use it" |
| Which app is in front | macOS, and the menu bar app installed | A1 not macOS |
| Menu bar dot, shortcuts, call detection | macOS, permission to install | A1 not macOS, or G1 is "no" |
| Browsing | Chrome or another Chromium browser | C1 is Safari/Firefox |
| Company-name rule for internal tools | An employer, and tools on company-named addresses | A0 is self-employed, or C2 is "mostly third-party" |
| Telling work Google from personal | Two Google accounts in one browser | C4 is "no" — then neither counts |
| Calendar | Google Calendar | F1 is Outlook or none |
| Meetings as evidence | Meetings actually on the calendar | F2 is "most are unplanned" |
| Anything at all done on a phone or tablet | — | Nothing can see them; A3b is how much this costs |

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
- Precision needed (H1 "close to exact") while the signals are thin — the
  tracker will produce a number, and that number will be wrong in ways they
  can't see. That's worse than no tracker, and it should be said. It is worse
  again when H2 says they'd have nothing to check it against, because then
  nothing will ever tell them it's wrong.
- A3b is "most of my work" — a large part of their day happens somewhere no
  setting can reach, and the totals will read low every single day. Say it in
  the verdict, not in a footnote.

## Step 3 — hand it back

Tell them the file path and that they should send that file back to whoever
asked them to run this. Offer to read it out if they'd like to check what's
in it before sending — some of these answers are about their employer and
they're entitled to see exactly what they're passing on.

Don't offer to set the tracker up. That's a different conversation and it
starts after somebody has read this.
