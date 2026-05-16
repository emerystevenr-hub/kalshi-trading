#!/usr/bin/env bash
# Install com.t3b.bls-actuals.plist as a user LaunchAgent.
# Runs ~daily at 07:00 PT to refresh BLS CPI YoY data after the 12:30 UTC
# release window. Idempotent on non-release days.

set -euo pipefail

PLIST_NAME="com.t3b.bls-actuals.plist"
SRC="${HOME}/Documents/${PLIST_NAME}"
DEST_DIR="${HOME}/Library/LaunchAgents"
DEST="${DEST_DIR}/${PLIST_NAME}"
LABEL="com.t3b.bls-actuals"

if [[ ! -f "${SRC}" ]]; then
    echo "ERROR: source plist not found at ${SRC}"
    exit 1
fi

mkdir -p "${DEST_DIR}"

# Unload if already present
if launchctl list | grep -q "${LABEL}"; then
    echo "→ unloading existing ${LABEL}"
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || \
        launchctl unload "${DEST}" 2>/dev/null || true
fi

echo "→ copying ${SRC} → ${DEST}"
cp "${SRC}" "${DEST}"

echo "→ loading ${LABEL}"
launchctl bootstrap "gui/$(id -u)" "${DEST}" 2>/dev/null || \
    launchctl load "${DEST}"

echo
echo "=== status ==="
launchctl list | grep "${LABEL}" || echo "(not yet visible — should appear in a moment)"

cat <<EOF

=========================================================================
INSTALLED.

VERIFY:
  launchctl list | grep com.t3b.bls-actuals
  tail -10 ~/Documents/terminal3b_data/bls_actuals.log

KICK MANUALLY:
  launchctl kickstart -k gui/\$(id -u)/com.t3b.bls-actuals
  tail -5 ~/Documents/terminal3b_data/bls_actuals.log

UNINSTALL:
  launchctl bootout gui/\$(id -u)/com.t3b.bls-actuals 2>/dev/null
  rm ~/Library/LaunchAgents/com.t3b.bls-actuals.plist

NOTE: Full Disk Access for /opt/homebrew/bin/python3 must already be
granted (from the calibration_trend / FRED puller install).
=========================================================================
EOF
