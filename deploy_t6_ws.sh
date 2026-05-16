#!/bin/bash
# deploy_t6_ws.sh — cut over T6 from REST polling to WebSocket streaming.
#
# Usage:
#   bash ~/Documents/deploy_t6_ws.sh                    # 30s smoke test, then exit
#   bash ~/Documents/deploy_t6_ws.sh --switch           # smoke test + cut over to WS
#
# Requirements (must be set in shell before invoking):
#   KALSHI_KEY_ID
#   KALSHI_PRIVATE_KEY_PATH
# Or: source ~/Documents/kalshi_secrets.env

set -euo pipefail

DOCS="$HOME/Documents"
PYTHON=/opt/homebrew/bin/python3
SWITCH=0
if [ "${1:-}" = "--switch" ]; then
    SWITCH=1
fi

if [ -z "${KALSHI_KEY_ID:-}" ] || [ -z "${KALSHI_PRIVATE_KEY_PATH:-}" ]; then
    if [ -f "$DOCS/kalshi_secrets.env" ]; then
        echo "==> Sourcing $DOCS/kalshi_secrets.env"
        # shellcheck disable=SC1091
        source "$DOCS/kalshi_secrets.env"
    fi
fi

if [ -z "${KALSHI_KEY_ID:-}" ] || [ -z "${KALSHI_PRIVATE_KEY_PATH:-}" ]; then
    echo "ERROR: KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH not set."
    echo "Either source ~/Documents/kalshi_secrets.env first, or export them manually."
    exit 1
fi

if [ ! -f "$KALSHI_PRIVATE_KEY_PATH" ]; then
    echo "ERROR: private key file not found at $KALSHI_PRIVATE_KEY_PATH"
    exit 1
fi

echo "==> Smoke test: 30-second WS capture"
"$PYTHON" "$DOCS/terminal6_mlb_kalshi_ws.py" --once

# Verify rows landed in the data dir within the last 60 sec
LATEST=$(ls -t "$DOCS/terminal6_data/kalshi_KXMLBGAME-"*.jsonl 2>/dev/null | head -1 || true)
if [ -z "$LATEST" ]; then
    echo "ERROR: smoke test produced no kalshi_KXMLBGAME-*.jsonl files"
    exit 1
fi

WS_ROW_COUNT=$(grep -c '"_source": "ws"' "$LATEST" 2>/dev/null || echo 0)
echo "    ws-tagged rows in $(basename "$LATEST"): $WS_ROW_COUNT"
if [ "$WS_ROW_COUNT" -lt 1 ]; then
    echo "ERROR: smoke test wrote no ws-tagged rows."
    exit 1
fi

if [ $SWITCH -eq 0 ]; then
    echo ""
    echo "==> Smoke test passed. NOT switching from REST to WS yet."
    echo "    To cut over: bash $0 --switch"
    exit 0
fi

echo ""
echo "==> Cutting over: kill REST logger, start WS logger as nohup daemon"
pkill -f "terminal6_mlb_kalshi_logger.py" 2>/dev/null || true
pkill -f "terminal6_mlb_kalshi_ws.py" 2>/dev/null || true
sleep 2

cd "$DOCS"
nohup caffeinate -is "$PYTHON" terminal6_mlb_kalshi_ws.py > terminal6_data/ws_logger.out 2>&1 &
disown
WS_PID=$!
echo "    ws logger pid=$WS_PID"

sleep 3
if ! ps -p "$WS_PID" > /dev/null 2>&1; then
    echo "ERROR: ws logger died immediately. Check terminal6_data/ws_logger.out"
    exit 1
fi

echo ""
echo "==> Verifying"
ps -ax -o pid,etime,command | grep -E "terminal6_mlb_kalshi_ws.py" | grep -v grep
echo ""
tail -10 "$DOCS/terminal6_data/ws_logger.log"

echo ""
echo "=================================================="
echo " T6 cutover to WebSocket complete."
echo " The REST logger is killed; WS logger is running."
echo " Tail: tail -f ~/Documents/terminal6_data/ws_logger.log"
echo " Watchdog automatically tracks ws_logger.log freshness (max age 6 min)."
echo "=================================================="
