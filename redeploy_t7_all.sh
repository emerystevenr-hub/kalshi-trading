#!/bin/bash
# redeploy_t7_all.sh — restart all T7 daemons after a terminal kill or reboot.
# Idempotent: kills any existing T7 processes, then starts fresh nohup'd copies.
#
# Daemons started (in order):
#   1. terminal7_kalshi_ws.py            — WS feed, KXNBAGAME + KXNHLGAME
#   2. terminal7_paper_trader.py --live  — sharp-vs-Kalshi delta trader
#   3. terminal7_settlement_reconciler.py — closes settled positions
#   4. terminal7_lines_puller.py         — Odds API NBA + NHL puller
#
# Pre-flight gates:
#   - Kalshi WS auth env vars must be set (or kalshi_secrets.env sourced)
#   - Freshness alarm flag blocks redeploy (data-feed wedge protection)
#   - Macro cap breach is LOGGED but does NOT block T7 redeploy.
#     T7 IS THE CAP-FIX: deploying T7 at $2K active sports drops portfolio
#     macro share from 53.6% → 50.0% exact. Blocking on a cap breach
#     would create a deadlock against the resolution path. Per session 4
#     direction from Steve.
#
# H20 bash-bug fix preserved: EC=$? is captured OUTSIDE the `if !` body.
# Prior pattern (`if ! cmd; then EC=$?; fi`) caught the inverted exit code
# (always 0). Now the test and the capture are decoupled.
#
# Usage: bash ~/Documents/redeploy_t7_all.sh

set -euo pipefail

DOCS="$HOME/Documents"
PYTHON=/opt/homebrew/bin/python3

# Source Kalshi WS auth env vars
if [ -f "$DOCS/kalshi_secrets.env" ]; then
    # shellcheck disable=SC1091
    source "$DOCS/kalshi_secrets.env"
fi

if [ -z "${KALSHI_KEY_ID:-}" ] || [ -z "${KALSHI_PRIVATE_KEY_PATH:-}" ]; then
    echo "ERROR: Kalshi WS auth env vars missing. Source kalshi_secrets.env first."
    exit 1
fi

# Pre-flight: refuse to redeploy if data freshness alarm is active.
if [ -f "$DOCS/freshness_alarm.flag" ]; then
    echo "ERROR: freshness alarm active — fix data feeds before redeploy."
    cat "$DOCS/freshness_alarm.flag"
    exit 1
fi

# Pre-flight: macro cap status — LOG-ONLY for T7.
# C1 fix: temporarily disable `set -e` for the cap check. Without this
# wrapper, set -e (set at top of script) aborts on the cap script's
# non-zero exit BEFORE we capture EC, breaking the warn-only design in
# the exact state T7 deployment is meant to resolve. The H20 fix
# (capturing EC outside `if !`) needed this companion change.
set +e
"$PYTHON" "$DOCS/portfolio_macro_concentration.py" >/dev/null 2>&1
EC=$?
set -e
if [ "$EC" -ge 2 ]; then
    echo "NOTE: macro cap breached (exit=$EC). NOT BLOCKING T7 redeploy:"
    echo "      T7 deployment is the cap-resolution path (53.6% → 50.0%)."
    echo "      Cap status visible via: $PYTHON $DOCS/portfolio_macro_concentration.py"
fi

# Pre-flight: confirm Odds API key file exists. Don't read it (privacy).
if [ ! -f "$DOCS/.odds_api_key" ]; then
    echo "ERROR: ~/Documents/.odds_api_key missing — Odds API puller will fail."
    echo "       Plus tier subscription per spec §9 Q1; one-line key file."
    exit 1
fi

echo "==> Killing any existing T7 daemons"
pkill -f "terminal7_kalshi_ws.py" 2>/dev/null || true
pkill -f "terminal7_kalshi_logger.py" 2>/dev/null || true
pkill -f "terminal7_paper_trader.py" 2>/dev/null || true
pkill -f "terminal7_settlement_reconciler.py" 2>/dev/null || true
pkill -f "terminal7_lines_puller.py" 2>/dev/null || true
sleep 2

# Ensure data dir exists
mkdir -p "$DOCS/terminal7_data"

# macOS-native session detachment: double-fork + setsid + FD redirect
# (mirrors T6's pattern from session 2 lesson #23).
detach() {
    local outfile="$1"; shift
    "$PYTHON" -c '
import os, sys
out = sys.argv[1]
cmd = sys.argv[2:]
if os.fork() > 0:
    os._exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)
fdnull = os.open(os.devnull, os.O_RDONLY)
fdout = os.open(out, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
os.dup2(fdnull, 0); os.dup2(fdout, 1); os.dup2(fdout, 2)
os.close(fdnull); os.close(fdout)
os.execvp(cmd[0], cmd)
' "$outfile" "$@"
}

cd "$DOCS"

echo "==> Starting WS feed (session-detached)"
detach terminal7_data/ws_logger.out caffeinate -is "$PYTHON" terminal7_kalshi_ws.py
echo "    ws feed launched"

echo "==> Starting paper trader (SHADOW mode — writes to ledger; session-detached)"
detach terminal7_data/paper_trader.out caffeinate -is "$PYTHON" terminal7_paper_trader.py --live --interval-sec 1800
echo "    paper trader launched"

echo "==> Starting settlement reconciler (session-detached)"
detach terminal7_data/settlement_reconciler.out caffeinate -is "$PYTHON" terminal7_settlement_reconciler.py --interval-sec 3600
echo "    reconciler launched"

echo "==> Starting Odds API lines puller (session-detached)"
detach terminal7_data/lines_puller.out caffeinate -is "$PYTHON" terminal7_lines_puller.py --interval-sec 900
echo "    lines puller launched"

sleep 3

echo ""
echo "==> Verify (TTY must be ?? — fully session-detached)"
for name in terminal7_kalshi_ws.py terminal7_paper_trader.py terminal7_settlement_reconciler.py terminal7_lines_puller.py; do
    pid=$(pgrep -f "$name" | head -1)
    if [ -n "$pid" ]; then
        tty=$(ps -p "$pid" -o tty= | tr -d ' ')
        if [ "$tty" = "??" ]; then
            echo "    $name pid=$pid TTY=?? OK"
        else
            echo "    WARN: $name pid=$pid TTY=$tty (still attached — setsid failed)"
        fi
    else
        echo "    ERROR: $name not running. Check the corresponding .out file."
    fi
done

echo ""
ps -ax -o pid,tty,etime,command | grep -E "terminal7_" | grep -v grep

echo ""
echo "=================================================="
echo " T7 redeployed. Daemons in their own session."
echo " Closing this terminal will NOT kill them."
echo "=================================================="
