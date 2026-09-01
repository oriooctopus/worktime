---
name: tracker
description: >-
  Shorthand that moves the session into the worktime time-tracker repo
  (~/coding/worktime) and orients for work on the exporters, the dot, the web
  view or the Mac probe. Use when the user invokes /tracker, or asks to change
  the time tracker, the working/not-working dot, the worktime web page, what
  counts as work, or the browsing/calendar/session exporters.
model: inherit  # pure instruction-injection into the current session; pinning would cap the work that follows
user-invocable: true
---

# /tracker — Enter the worktime repo

```
cd ~/coding/worktime
```

**Work on `master` in the main checkout. Do not create a worktree.**

Every live surface symlinks into *this* checkout — the systemd timers, the
`dot` command on PATH, `/indicator-dot` and `/worktime-setup`. A worktree would
edit files nothing is running, and the services would keep executing the old
code until a merge *and* a checkout update. Edits here are live immediately,
which is the point.

Push straight to `master`; this is Oliver's own repo, so no PRs.

## Orientation

- **GitHub:** `oriooctopus/worktime`, private. Publishing needs his explicit OK.
- **Tests:** `python3 -m pytest tests` from the repo root (249 at time of writing).
  A behaviour change ships with its test in the same turn.
- **Live here (Linux box):** `worktime-web.service` on **8313**
  (http://100.103.237.24:8313), plus timers `calendar-export` (15 min),
  `chrome-work-blocks` (2 min), `activity-export` (5 min). Restart with
  `systemctl --user restart <unit>` and verify with `curl`/`dot`, never by
  assuming.
- **Live on the Mac:** `bin/worktime-probe.py` and `bin/worktime-bar/` — the
  probe owns the actual working/not-working decision and the menu bar dot.
  **Neither runs on this box** (the probe needs macOS paths and Python 3.9+).
  Deploying a change to them means `git pull` in the repo on his Mac.
- **Config is never in the repo.** `~/.config/worktime/profile.json`,
  `~/.config/activity-export/work-domains.json`,
  `~/.config/calendar-export/overrides.json`. Only `.example.json` files ship.
- **Data lives in Obsidian**, never committed: `Dashboard/calendar-today.md`,
  `Dashboard/activity/<date>.md`, `Dashboard/worktime/<date>.{json,md}`.

## Traps this repo has already hit

- **Python here is 3.8.** `zoneinfo` needs the `backports.zoneinfo` fallback
  every module in `bin/` already uses.
- **Two machines, two vault paths.** The Mac keeps it under `Documents/Main`,
  this box under `~/obsidian-vault`. Hardcoding either broke the other — use
  `dashboard_dir()`.
- **Only `.md` syncs.** Obsidian Sync skips non-markdown, so anything that must
  cross machines is markdown. That is why the probe writes a `.md` sidecar
  beside its `.json` snapshot.
- **Don't reinvent the session rule.** `merge_spans` in the probe already
  unions prompts, meetings and marks under an adaptive threshold
  (`GAP_AFTER = 5`, ramping from 1 in unfocused mode). Render what it decided;
  a second definition drifts from the one the dot uses.
- **`visit_duration` is not attention** — it is time until the tab navigated
  away, so a parked tab accrues the full clock. `total_foreground_duration` is
  the real signal but Chrome Sync strips it (`-1` on every synced visit).
- **Never publish prompt text** into the vault sidecar; it syncs everywhere.
