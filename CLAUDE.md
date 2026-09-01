# CLAUDE.md — worktime

This is a personal repo (`oriooctopus/worktime`), usually being worked on by
several Claude Code sessions at once — interactive sessions and background
jobs, sometimes both editing different things in the same hour. Assume that's
happening any time you start work here, and follow the practices below so
sessions don't collide.

## Multi-session ground rules

- **Every task gets its own worktree.** Never `git switch -c` or commit
  directly on `master` in the primary checkout — another session may be
  mid-edit there. Create a worktree and enter it before making any change.
- **Worktrees go outside `.claude/worktrees/`.** That directory gets swept
  and worktrees inside it can be deleted (branch included) when the owning
  session ends — this has already happened to at least one in-progress
  worktree here. Use `~/Documents/coding/worktime-worktrees/<slug>` instead:
  ```
  git worktree add ~/Documents/coding/worktime-worktrees/<slug> -b <slug>
  ```
  then `EnterWorktree(path="~/Documents/coding/worktime-worktrees/<slug>")`.
  Exit with `action: "keep"` — don't remove worktrees other sessions might
  still be using, and don't remove your own unless asked.
- **Check for concurrent work before starting.** `git status --short`,
  `git worktree list`, and `git log --oneline -5` before touching anything —
  if another worktree exists with unpushed or unmerged commits, leave it
  alone; it belongs to a session that may still be running.
- **Pull `master` first.** `git pull --ff-only` in the primary checkout
  before branching, so new work starts from a fresh base and doesn't pick up
  avoidable conflicts with whatever another session just merged.
- **This is a personal repo: push and merge directly, don't leave PRs open.**
  Commit, push the branch, merge to `master`, delete the branch. No waiting
  for review. If a PR gets created anyway, merge it immediately rather than
  leaving it for the user. Force-push, history rewrites, and deleting
  worktrees/branches still need explicit confirmation.
- **Resolve conflicts by hand.** Two sessions touching the probe or the
  dashboard exporters in the same afternoon is normal here — never blindly
  take one side of a merge conflict; read both changes and reconcile them.

## Repo-specific gotchas

- **The two Claude Code hooks this project depends on live in `bin/`, not
  `~/.claude/hooks/`.** `bin/prompt-count.py` and `bin/worktime-approval.py`
  are the real, git-tracked files; `~/.claude/hooks/prompt-count.py` and
  `~/.claude/hooks/worktime-approval.py` are symlinks into this repo (same
  pattern as `~/.claude/bin/worktime-probe.py`). Edit them here, not through
  the symlink target — and if a symlink is ever missing after a fresh
  machine setup, see the Install section in README.md.
- **Two separate Claude Code profiles feed this project.** Interactive
  sessions run under the normal `~/.claude` profile; worktime's own
  background jobs run under a second, separate profile at
  `~/.claude-personal` (its own `projects/`, its own `settings.json`, no
  hooks of its own). `prompt-count.py` walks both
  (`~/.claude/projects` and `~/.claude-personal/projects`) so background-job
  prompts count as presence too — if that stops showing up in tracking,
  check both roots are still in `PROJECT_ROOTS` there.
- **Data lives in Obsidian, never in this repo.**
  `Dashboard/calendar-today.md`, `Dashboard/activity/<date>.md`,
  `Dashboard/worktime/<date>.json` are read/written by the exporters but
  intentionally never committed here.
