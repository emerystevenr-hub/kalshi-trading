#!/bin/bash
# redeploy_entropy_phase1.sh — activate entropy collapse detector + restart traders.
#
# What this does:
#   1. SIGHUP the scheduler so it picks up the new entropy_collapse_detector job
#   2. Restart T3b paper trader (picks up entropy gate)
#   3. Restart T3c paper trader (picks up entropy gate)
#   4. Restart T6 (kalshi WS + paper trader + reconciler — picks up entropy gate + KL log)
#
# T1 trader and T3a are NOT touched. T1 has no entropy gate. T3a doesn't trade.

set -euo pipefail

DOCS="$HOME/Documents"
PYTHON=/opt/homebrew/bin/python3

# Pre-flight: refuse to redeploy if macro concentration cap is breached
# OR if freshness alarm is active. Exit codes: 0=under, 1=at-cap, 2=over.
# Fixed 2026-05-09 (audit H-INFRA-2).
if ! "$PYTHON" "$DOCS/portfolio_macro_concentration.py" >/dev/null 2>&1; then
    EC=$?
    if [ "$EC" -ge 2 ]; then
        echo "ERROR: macro concentration cap breached (exit=$EC). Resolve before redeploy."
        exit 1
    fi
fi
if [ -f "$DOCS/freshness_alarm.flag" ]; then
    echo "ERROR: freshness alarm active — fix data feeds before redeploy."
    cat "$DOCS/freshness_alarm.flag"
    exit 1
fi

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

echo "==> SIGHUP scheduler (loads new entropy_collapse_detector job)"
if [ -f "$DOCS/scheduler.pid" ]; then
    SPID=$(cat "$DOCS/scheduler.pid")
    if kill -HUP "$SPID" 2>/dev/null; then
        echo "    scheduler pid=$SPID notified"
    else
        echo "    WARN: scheduler pid $SPID not running. Restart manually."
    fi
fi

echo "==> Restarting T3b paper trader (session-detached)"
pkill -f "terminal3b_paper_trader.py" 2>/dev/null || true
sleep 2
cd "$DOCS"
detach terminal3b_data/paper_trader.out caffeinate -is "$PYTHON" terminal3b_paper_trader.py --interval-sec 1800
echo "    launched"

echo "==> Restarting T3c paper trader (session-detached)"
pkill -f "terminal3c_paper_trader.py" 2>/dev/null || true
sleep 2
detach terminal3c_data/paper_trader.out caffeinate -is "$PYTHON" terminal3c_paper_trader.py --interval-sec 1800
echo "    launched"

echo "==> Restarting T6 (full bundle)"
bash "$DOCS/redeploy_t6_all.sh"

echo ""
echo "==> Verify (TTY must be ?? for all)"
sleep 2
ps -ax -o pid,tty,etime,command | grep -E "(terminal3[bc]_paper_trader|terminal6_mlb_)" | grep -v grep
echo ""
echo "=================================================="
echo " Entropy Phase 1 deployed."
echo " - Detector runs every 5 min via scheduler"
echo " - Traders block fading-collapse entries when watch alert is active"
echo " - KL divergence logged on every T6 signal"
echo " - Daily calibration task runs 8:09 AM, ramps to Phase 2 enable at n>=15"
echo "=================================================="
