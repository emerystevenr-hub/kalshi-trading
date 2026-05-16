#!/bin/bash
# Install the T3c FRED ICSA puller on a 12h schedule (06:00 + 18:00 PT).
# Idempotent — re-running unloads + reloads cleanly.

set -u

PLIST_SRC="$(dirname "$0")/com.terminal3c.claims-data.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.terminal3c.claims-data.plist"
SCRIPT="$HOME/Documents/terminal3c_claims_data.py"
PY="/opt/homebrew/bin/python3"

if [[ ! -f "$SCRIPT" ]]; then
    echo "ERROR: $SCRIPT not found."
    exit 1
fi
if [[ ! -f "$PLIST_SRC" ]]; then
    echo "ERROR: $PLIST_SRC not found."
    exit 1
fi
if [[ ! -x "$PY" ]]; then
    echo "ERROR: $PY not found."
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HOME/Documents/terminal3c_data"

launchctl unload "$PLIST_DST" 2>/dev/null || true

cp "$PLIST_SRC" "$PLIST_DST"
echo "Installed plist → $PLIST_DST"

launchctl load "$PLIST_DST"
echo "Loaded job. Will fire at 06:00 PT and 18:00 PT, plus immediately (RunAtLoad)."

echo
echo "=== launchctl list ==="
launchctl list | grep terminal3c.claims-data || echo "(not yet visible — give it a moment)"

echo
echo "=== first-pull output (sleep 4s for fetch + merge) ==="
sleep 4
tail -10 "$HOME/Documents/terminal3c_data/claims_data.log"

cat <<'EOF'

=========================================================================
T3c FRED ICSA PULLER — INSTALLED

Schedule: twice daily at 06:00 and 18:00 PT.
The 06:00 fire lands ~30-90 min after Thursday's 8:30 ET DOL release —
in time for the model to use the new print before Kalshi's market closes.

The script is idempotent (merge-on-date), so duplicate fires are no-op.

Manual fire:
  launchctl kickstart -k gui/$(id -u)/com.terminal3c.claims-data

Uninstall:
  launchctl unload ~/Library/LaunchAgents/com.terminal3c.claims-data.plist
  rm ~/Library/LaunchAgents/com.terminal3c.claims-data.plist

WHY THIS MATTERS: without this schedule, the May 7 print would never land
in icsa_history.jsonl, so the paper trader's nowcast would stay anchored
to April 25 data on settlement day — wasting any edge from the new print.
=========================================================================
EOF
