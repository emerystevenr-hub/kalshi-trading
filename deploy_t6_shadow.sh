#!/bin/bash
# deploy_t6_shadow.sh — flip the T6 paper trader from dry-run to shadow mode.
#
# Shadow mode writes simulated positions to ~/Documents/shadow_pnl/ledger.jsonl
# (no real Kalshi orders). This is what T1/T3b/T3c are doing.
#
# Confusingly, the paper trader's flag for this is --live. Misnomer. There is
# NO real-money path in T6 code yet. --live just enables ShadowLedger writes.
#
# Usage:
#   bash ~/Documents/deploy_t6_shadow.sh

set -euo pipefail

DOCS="$HOME/Documents"
PYTHON=/opt/homebrew/bin/python3

echo "==> Killing the dry-run paper trader"
pkill -f "terminal6_mlb_paper_trader.py" 2>/dev/null || true
sleep 2

echo "==> Starting paper trader in SHADOW mode (writes to ledger.jsonl, no real money)"
cd "$DOCS"
nohup caffeinate -is "$PYTHON" terminal6_mlb_paper_trader.py --live --interval-sec 1800 > terminal6_data/paper_trader.out 2>&1 &
disown
TRADER_PID=$!
echo "    paper trader pid=$TRADER_PID  (mode=shadow, interval=30min)"

sleep 3
if ! ps -p "$TRADER_PID" > /dev/null 2>&1; then
    echo "ERROR: trader died immediately. Check terminal6_data/paper_trader.out"
    exit 1
fi

echo ""
echo "==> Verify"
ps -ax -o pid,etime,command | grep "terminal6_mlb_paper_trader.py" | grep -v grep

echo ""
echo "=================================================="
echo " T6 paper trader running in SHADOW mode."
echo " Ledger writes go to ~/Documents/shadow_pnl/ledger.jsonl"
echo " First trades land on the next 30-min cycle if any signal triggers."
echo " To watch: tail -f ~/Documents/terminal6_data/paper_trader.log"
echo " Real money is NOT enabled — that requires additional code."
echo "=================================================="
