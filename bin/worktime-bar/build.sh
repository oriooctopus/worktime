#!/bin/bash
# Build the menu bar app into the bundle launchd actually runs.
#
# The app is more than one Swift file now, so "swiftc main.swift" is no longer
# the whole build and the command belonged somewhere other than in somebody's
# shell history. launchd runs the copy inside WorktimeBar.app, so that is the
# copy this writes; the loose binaries beside it are older builds kept only
# because they are still in git.
set -euo pipefail

cd "$(dirname "$0")"
APP="WorktimeBar.app/Contents/MacOS/WorktimeBar"

swiftc -O main.swift ActivitySession.swift CallDetector.swift CountdownPanel.swift IdleWatcher.swift -o "$APP"

# Ad-hoc signature. The bundle carries a _CodeSignature from the previous build
# and an unsigned replacement inside a signed bundle is refused at launch.
codesign --force --sign - WorktimeBar.app

echo "built $APP"
echo "restart it with:"
echo "  launchctl kickstart -k gui/$(id -u)/com.oliver.worktime-bar"
