---
name: worktime-setup
description: >-
  Set up or change the automatic time tracker — what counts as work, what
  counts as personal, and how long a pause counts as a break. Use when someone
  says "set up the time tracker", "configure my time tracker", "worktime setup",
  "change what counts as work", "it's counting the wrong things as work",
  "it says I was working when I wasn't", "it missed time I was working", or
  asks to adjust the tracker's settings.
model: sonnet
user-invocable: true
disable-model-invocation: true
argument-hint: "[nothing needed — it will ask you questions]"
---

# Time tracker setup

Walks someone through configuring the tracker by asking plain questions. It is
written for a person who knows only that this is an automatic time tracker.

`disable-model-invocation` because it rewrites live config; it should only run
when someone asks for it by name. Sonnet because it is a fixed sequence: ask,
validate, write, preview.

## Rules for how you talk during this

**Never use the internal vocabulary.** Not `gap_sec`, `dwell`, `visit_duration`,
`blocks`, `min_block_sec`, `the probe`, `the export`. Say "break", "how long a
page sat open", "stretch of work". If you catch yourself about to name a config
key, name the behaviour instead.

**One question at a time**, with a recommended default. Every question must be
answerable by someone who has never seen the code. Use AskUserQuestion where
there is a small set of sensible choices.

**Never guess her calendar addresses or timezone.** Ask.

## Step 1 — say what it does, before asking anything

Tell her, in about this much detail and in your own words:

> This works out when you were actually working, so you don't have to track it
> by hand. It looks at two things: your calendar, and which websites you visited
> and when. It reads the site addresses and the times — not what you typed, not
> what any page said, and not the contents of your email or messages. Everything
> stays on your own computer; nothing is uploaded anywhere.
>
> I'll ask you a handful of questions. You can change any answer later by asking
> me to run this again, and nothing is final until the end.

If she seems hesitant about the browsing history part, stop and let her decide.
Do not talk her into it.

## Step 2 — the questions

1. **Personal calendar** — "What email address is your own calendar on?"
2. **Work calendar** — "Do you have a separate work calendar? If so, what's its
   address?" Accept "no".
3. **Timezone** — offer her likely one as the default, confirm it.
3a. **A separate work email** — "Do you have a separate email address you use
   for work?" If no, skip the rest of this question.

   If yes: Gmail, Calendar and Drive all serve every signed-in account from the
   same web addresses, and tell them apart only by an account number in the
   address bar. The first account she added is 0, the second is 1, and so on.
   Don't ask her for a number — she has no reason to know it. Find it:

   ```
   python3 suggest-sites.py 14 --google-accounts
   ```

   It lists each account number she actually uses, with the sites she used it
   for. Chrome does not record which address is which, so have her open
   `https://mail.google.com/mail/u/<number>/` for each candidate and tell you
   which is the work one. Pass that number as `google_work_account`.

   What this buys her: time in her work inbox, work calendar and work Drive
   counts as working, while the same sites under her personal account do not.
   Without it neither counts, because there is no way to tell them apart.
3b. **The company's name** — "What's the company called? I'll count any web
   address with that word in it as work." Default it to the domain of her work
   email if she gave one. This is what catches the internal tools nobody thinks
   to list: the wiki, the ticket tracker, the training site, the login portal.
   Pass it as `work_url_keywords` (a list — she may name more than one).

   Say explicitly that this is the address only: *searching* for the company's
   name does not count as working.
4. **What counts as work** — first run `suggest-sites.py 14` and show her her own
   most-visited sites as a list. Ask which of these mean she's working, and
   whether anything is missing. Never make her invent a list from memory.
5. **What definitely isn't work** — from the same list. Social media, shopping,
   streaming. Explain that anything she doesn't put in either list simply won't
   start or stop anything on its own.
6. **Breaks** — "If you stop for a few minutes, how long a pause should count as
   a break rather than still working?" Default **3 minutes**.
7. **Walking away** — "If a page is left open but you're not touching it, how
   long before I should assume you walked away?" Default **10 minutes**.
8. **Shortest stretch** — "What's the shortest bit of work that should count at
   all?" Default **3 minutes**.

## Step 3 — write it

Pipe one JSON object into `write-config.py` (it validates and writes both config
files, and refuses an empty work list or a site appearing in both lists):

```
echo '{...}' | python3 write-config.py
```

Keys: `timezone`, `personal_account`, `work_calendar_id` (or null),
`chrome_history_path` (or null), `google_work_account` (a number, or null when
she has no separate work email), `work_url_keywords` (or null to keep the
default), `work`, `ignore`, `break_minutes`, `walked_away_minutes`,
`shortest_stretch_minutes`.

If it exits non-zero, tell her what was wrong in plain words and re-ask only
that question.

## Step 4 — show her the result and check it against her memory

Run `bin/chrome-work-blocks.py --dry-run` and show today's stretches as plain
times ("9:40–10:15, 10:22–11:05"). Then ask the question that actually validates
it: **"Does that match how today went?"**

If she says it shows her working when she wasn't, lower the break number. If it
missed real work, raise it, or add a site to the work list. Re-run and show her
again. Repeat until she agrees.

This step is the point of the skill. A config she never sanity-checked against
her own memory of the day is not set up, it is just written.
