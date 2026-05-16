#!/bin/bash
# Install the portfolio freshness watchdog as a LaunchAgent.
# Runs every 15 minutes. Writes ~/Documents/freshness_alarm.flag
# whenever any data file in the watch list exceeds its expected_max_age_h.
#
# After install, verify with:
#   launchctl list | grep freshness
#   tail -20 ~/Documents/freshness_watchdog.out
#
# To uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.portfolio.freshness-watchdog.plist
#   rm ~/Library/LaunchAgents/com.portfolio.freshness-watchdog.plist

set -euo pipefail

PLIST_NAME="com.portfolio.freshness-watchdog.plist"
SRC="$HOME/Documents/$PLIST_NAME"
DST="$HOME/Library/LaunchAgents/$PLIST_NAME"

if [ ! -f "$SRC" ]; then
    echo "ERROR: source plist not found at $SRC"
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"

# Unload any existing version (ignore failure if not loaded).
launchctl unload "$DST" 2>/dev/null || true

cp "$SRC" "$DST"
launchctl load "$DST"

echo "Installed and loaded $PLIST_NAME"
launchctl list | grep -i freshness || true
echo
echo "Watchdog runs every 15 minutes. Stale data triggers ~/Documents/freshness_alarm.flag"
