#!/bin/bash
# redeploy_t6_all.sh — restart all three T6 daemons after a terminal kill or reboot.
# Idempotent: kills any existing T6 processes, then starts fresh nohup'd copies.
#
# Usage: bash ~/Documents/redeploy_t6_all.sh

set -euo pipefail

DOCS="$HOME/Documents"
PYTHON=/opt/homebrew/bin/python3

# Source Kalshi WS auth env vars (needed by ws logger)
if [ -f "$DOCS/kalshi_secrets.env" ]; then
    # shellcheck disable=SC1091
    source "$DOCS/kalshi_secrets.env"
fi

if [ -z "${KALSHI_KEY_ID:-}" ] || [ -z "${KALSHI_PRIVATE_KEY_PATH:-}" ]; then
    echo "ERROR: Kalshi WS auth env vars missing. Source kalshi_secrets.env first."
    exit 1
fi

# Pre-flight: refuse to redeploy if macro concentration cap is breached.
# T6 is non-macro so it doesn't itself contribute to the breach, but
# redeploys are a natural checkpoint to surface portfolio-wide gates.
# Exit codes: 0=under, 1=at-cap, 2=over-cap. Block on 2 only.
# Fixed 2026-05-09 (audit H-INFRA-2).
if ! "$PYTHON" "$DOCS/portfolio_macro_concentration.py" >/dev/null 2>&1; then
    EC=$?
    if [ "$EC" -ge 2 ]; then
        echo "ERROR: macro concentration cap breached (exit=$EC). Resolve before redeploy."
        echo "       run: $PYTHON $DOCS/portfolio_macro_concentration.py"
        exit 1
    fi
fi
# Pre-flight: refuse to redeploy if data freshness alarm is active.
if [ -f "$DOCS/freshness_alarm.flag" ]; then
    echo "ERROR: freshness alarm active — fix data feeds before redeploy."
    cat "$DOCS/freshness_alarm.flag"
    exit 1
fi

echo "==> Killing any existing T6 daemons"
pkill -f "terminal6_mlb_kalshi_ws.py" 2>/dev/null || true
pkill -f "terminal6_mlb_kalshi_logger.py" 2>/dev/null || true
pkill -f "terminal6_mlb_paper_trader.py" 2>/dev/null || true
pkill -f "terminal6_mlb_settlement_reconciler.py" 2>/dev/null || true
sleep 2

# macOS-native session detachment: double-fork + setsid + FD redirect via Python.
# Survives terminal close, SIGHUP, and process-group signals from the controlling tty.
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

echo "==> Starting WS logger (session-detached)"
cd "$DOCS"
detach terminal6_data/ws_logger.out caffeinate -is "$PYTHON" terminal6_mlb_kalshi_ws.py
echo "    ws logger launched"

echo "==> Starting paper trader (SHADOW mode — writes to ledger; session-detached)"
detach terminal6_data/paper_trader.out caffeinate -is "$PYTHON" terminal6_mlb_paper_trader.py --live --interval-sec 1800
echo "    paper trader launched"

echo "==> Starting settlement reconciler (session-detached)"
detach terminal6_data/settlement_reconciler.out caffeinate -is "$PYTHON" terminal6_mlb_settlement_reconciler.py --interval-sec 3600
echo "    reconciler launched"

sleep 3

echo ""
echo "==> Verify (TTY must be ?? — fully session-detached)"
for name in terminal6_mlb_kalshi_ws.py terminal6_mlb_paper_trader.py terminal6_mlb_settlement_reconciler.py; do
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
ps -ax -o pid,tty,etime,command | grep -E "terminal6_mlb_" | grep -v grep

echo ""
echo "=================================================="
echo " T6 redeployed with setsid-f detachment."
echo " Daemons are in their own session. Closing this terminal"
echo " will NOT kill them. No special exit ritual required."
echo "=================================================="
