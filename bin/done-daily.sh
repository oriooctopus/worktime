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
# The log lives outside ~/Documents on purpose: if vault access breaks again,
# the diagnostics must still be writable.
set -u

VAULT="$HOME/Documents/Main/Long term/Achievements"
LOG="$HOME/.local/log/done-daily.log"
CLAUDE="$HOME/.local/bin/claude"
LOOKBACK=4               # days back to consider for backfill

log() { print -r -- "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

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
  log "running /done $arg for $day"
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
  run_done "$d" "$d"
done

# --- Pass 2: today ----------------------------------------------------------
STAMP=$(date +%Y-%m-%d)
HOUR=$(date +%-H)
DOW=$(date +%u)

[[ "$DOW" -gt 5 ]] && exit 0                        # Friday's run covers Fri-Sun
[[ "$HOUR" -lt ${DONE_MIN_HOUR:-18} ]] && exit 0    # only after end of day
have_note "$STAMP" && exit 0                        # makes the 30-min tick idempotent

run_done "$STAMP" "today"
