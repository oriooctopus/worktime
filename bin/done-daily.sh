#!/bin/zsh
# Daily /done runner, invoked by launchd on a plain repeating timer (30 min).
#
# Scheduler history - four separate failures, all diagnosed. Do not undo:
#
#  1. launchd pointed directly at ~/.local/bin/claude. That is a symlink into
#     ~/.local/share/claude/versions/<ver>, which rotates on every auto-update.
#     macOS pins a launch constraint to the resolved binary at registration, so
#     after an update every launch was refused with exit 78 EX_CONFIG *before
#     exec* - zero output. Hence the /bin/zsh wrapper in the plist.
#
#  2. launchd StartCalendarInterval never fired: registered correctly, zero
#     events delivered (UserEventAgent-Aqua). kickstart worked, which is why it
#     hid. Hence StartInterval instead of a wall-clock trigger.
#
#  3. cron's access to ~/Documents is INTERMITTENT - fine at midday, denied
#     ("operation not permitted") across 2026-08-18 18:00-23:00, 27 failures.
#     Consistent with TCC denying background daemons while the session is
#     locked, i.e. exactly during the evening window. Hence launchd, not cron.
#
#  4. The original guard only ever wrote TODAY's note, so a day lost to any of
#     the above was lost permanently (that is how 2026-08-18 vanished). The
#     backfill pass below is the actual self-heal.
#
#  5. Follow-on from (3), and far more expensive: the ~/Documents denial is not
#     intermittent for THIS job, it is total. Probed from a launchd agent on
#     2026-09-08, /bin/zsh cannot read the vault at all - ls fails and the *.md
#     glob returns zero while ~20 notes sit on disk. claude carries its own
#     Documents grant, so it writes the note happily; the shell then cannot see
#     what it just wrote. have_note was the idempotency guard for both passes,
#     so a permanently-false guard meant the 30-min tick re-ran the same day
#     forever: 134 runs and roughly $56 between 09-01 and 09-08, against the 6
#     that were wanted. Hence vault_readable() below, which refuses to run at
#     all rather than mistaking a blind read for a missing note, and the
#     per-day attempt cap that bounds the damage from any future cause.
#     Fixing it for real needs Full Disk Access granted to /bin/zsh; until
#     then this script correctly does nothing and says so.
#
# The log lives outside ~/Documents on purpose: if vault access breaks again,
# the diagnostics must still be writable.
set -u

VAULT="$HOME/Documents/Main/Long term/Achievements"
LOG="$HOME/.local/log/done-daily.log"
STATE="$HOME/.local/state/done-daily"   # outside ~/Documents, same reason as LOG
CLAUDE="$HOME/.local/bin/claude"
LOOKBACK=4               # days back to consider for backfill
MAX_ATTEMPTS=3           # per day, across ticks - bounds the cost of any bad guard

log() { print -r -- "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

mkdir -p "$STATE"

# Can this process actually see the vault? The vault always holds notes, so an
# empty listing means the read was refused, not that the directory is empty -
# and that distinction is the whole bug this guard exists to prevent. Never
# collapse it into "no note found".
vault_readable() {
  emulate -L zsh
  setopt local_options null_glob
  local probe=("$VAULT"/*.md)
  (( ${#probe} ))
}

# Refuse the tick outright rather than run blind. A blind tick cannot tell
# whether its work is already done, so it repeats it every 30 minutes forever.
if ! vault_readable; then
  log "BLOCKED - cannot read $VAULT from this context; no runs attempted. Grant Full Disk Access to /bin/zsh (System Settings > Privacy & Security > Full Disk Access)."
  exit 0
fi

# Attempts are counted per day and persist across ticks, so a day that keeps
# failing for a reason we have not thought of stops after MAX_ATTEMPTS instead
# of billing indefinitely.
attempts_for() { local f="$STATE/$1.attempts"; [[ -f "$f" ]] && cat "$f" || print 0; }
bump_attempts() { local f="$STATE/$1.attempts"; print $(( $(attempts_for "$1") + 1 )) > "$f"; }

exhausted() {
  local n=$(attempts_for "$1")
  (( n >= MAX_ATTEMPTS ))
}

# A day is covered if any note starts with its date. The glob matters: a Friday
# is often covered by a consolidated "<fri> to <sun>.md" under the weekend rule.
have_note() {
  # emulate -L pins zsh options locally: a login rc that sets NO_BARE_GLOB_QUAL
  # or NO_NULL_GLOB would otherwise make this glob error out instead of
  # returning "no match", and the script would die mid-check.
  emulate -L zsh
  setopt local_options null_glob
  local hits=("$VAULT"/"$1"*.md)
  (( ${#hits} ))
}

run_done() {
  local day=$1 arg=$2
  if [[ -n "${DONE_DRY_RUN:-}" ]]; then
    log "DRY RUN - would run /done $arg for $day"
    return 0
  fi
  bump_attempts "$day"
  log "running /done $arg for $day (attempt $(attempts_for "$day") of $MAX_ATTEMPTS)"
  "$CLAUDE" -p "/done $arg" >> "$LOG" 2>&1
  local rc=$?
  if have_note "$day"; then
    log "OK - wrote note for $day (exit $rc)"
  else
    log "FAILED - no note produced for $day (exit $rc)"
  fi
  exit $rc
}

# --- Pass 1: backfill a missed weekday -------------------------------------
# Runs at ANY hour. A day missed because the machine was locked all evening can
# only be recovered later, when the session is unlocked - gating this on 18:00
# would make the recovery unreachable. One day per tick bounds the cost.
for i in {1..$LOOKBACK}; do
  d=$(date -v-${i}d +%Y-%m-%d)
  dow=$(date -j -f %Y-%m-%d $d +%u)
  [[ "$dow" -gt 5 ]] && continue          # weekend: covered by Friday's note
  have_note "$d" && continue
  if exhausted "$d"; then
    log "GIVING UP on $d - $MAX_ATTEMPTS attempts made, still no note. Run /done $d by hand, or clear $STATE/$d.attempts to retry."
    continue
  fi
  run_done "$d" "$d"
done

# --- Pass 2: today ----------------------------------------------------------
STAMP=$(date +%Y-%m-%d)
HOUR=$(date +%-H)
DOW=$(date +%u)

[[ "$DOW" -gt 5 ]] && exit 0                        # Friday's run covers Fri-Sun
[[ "$HOUR" -lt ${DONE_MIN_HOUR:-18} ]] && exit 0    # only after end of day
have_note "$STAMP" && exit 0                        # makes the 30-min tick idempotent

if exhausted "$STAMP"; then
  log "GIVING UP on $STAMP - $MAX_ATTEMPTS attempts made, still no note. Run /done today by hand, or clear $STATE/$STAMP.attempts to retry."
  exit 0
fi

run_done "$STAMP" "today"
