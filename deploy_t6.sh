#!/bin/bash
# deploy_t6.sh — bootstrap the T6 MLB engine.
#
# Usage:  bash ~/Documents/deploy_t6.sh YOUR_ODDS_API_KEY
#
# What it does:
#   1. Writes your Odds API key to ~/Documents/.odds_api_key (chmod 600)
#   2. Tests the lines puller; aborts if 401
#   3. Enables the lines puller in scheduler_jobs.json and SIGHUPs the scheduler
#   4. Starts the kalshi logger, paper trader (DRY-RUN), and reconciler as nohup daemons
#   5. Prints portfolio_status.sh

set -euo pipefail

DOCS="$HOME/Documents"
KEY_FILE="$DOCS/.odds_api_key"
JOBS_FILE="$DOCS/scheduler_jobs.json"
PYTHON=/opt/homebrew/bin/python3

if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
    echo "Usage: bash $0 YOUR_ODDS_API_KEY"
    echo "(Get a free key at https://the-odds-api.com/ — sign up, copy from dashboard.)"
    exit 1
fi

API_KEY="$1"

if [ "$API_KEY" = "PASTE_YOUR_KEY_HERE" ] || [ "$API_KEY" = "YOUR_ODDS_API_KEY" ]; then
    echo "ERROR: that's the placeholder, not a real key. Get one at https://the-odds-api.com/"
    exit 1
fi

echo "==> Writing API key to $KEY_FILE"
printf '%s\n' "$API_KEY" > "$KEY_FILE"
chmod 600 "$KEY_FILE"

echo "==> Testing lines puller (single pull)"
if ! "$PYTHON" "$DOCS/terminal6_mlb_lines_puller.py" --once; then
    echo "ERROR: lines puller failed. Check the log at $DOCS/terminal6_data/lines_puller.log"
    exit 1
fi

LATEST_LINES=$(ls -1 "$DOCS/terminal6_data/vegas_lines_"*.jsonl 2>/dev/null | tail -1 || true)
if [ -z "$LATEST_LINES" ]; then
    echo "ERROR: no vegas_lines file produced. API key may be wrong."
    exit 1
fi
LINE_COUNT=$(wc -l < "$LATEST_LINES" | tr -d ' ')
echo "    OK — $LINE_COUNT line(s) in $(basename "$LATEST_LINES")"

echo "==> Enabling t6_mlb_lines_puller in scheduler config"
"$PYTHON" - "$JOBS_FILE" <<'PY'
import json, sys
path = sys.argv[1]
data = json.loads(open(path).read())
for j in data.get("jobs", []):
    if j["name"] == "t6_mlb_lines_puller":
        j["enabled"] = True
        print(f"    Set {j['name']}.enabled = true")
open(path, "w").write(json.dumps(data, indent=2) + "\n")
PY

if [ -f "$DOCS/scheduler.pid" ]; then
    SCHED_PID=$(cat "$DOCS/scheduler.pid")
    if kill -HUP "$SCHED_PID" 2>/dev/null; then
        echo "    SIGHUP sent to scheduler (pid=$SCHED_PID)"
    else
        echo "    WARN: scheduler PID $SCHED_PID not running; will restart"
        nohup "$PYTHON" "$DOCS/portfolio_scheduler.py" > "$DOCS/scheduler.out" 2>&1 &
        disown
    fi
fi

echo "==> Killing any existing T6 daemons (clean restart)"
pkill -f "terminal6_mlb_kalshi_logger.py" 2>/dev/null || true
pkill -f "terminal6_mlb_paper_trader.py" 2>/dev/null || true
pkill -f "terminal6_mlb_settlement_reconciler.py" 2>/dev/null || true
sleep 2

echo "==> Starting T6 nohup daemons"
mkdir -p "$DOCS/terminal6_data"

cd "$DOCS"
nohup caffeinate -is "$PYTHON" terminal6_mlb_kalshi_logger.py > terminal6_data/kalshi_logger.out 2>&1 &
disown
KALSHI_PID=$!
echo "    kalshi logger pid=$KALSHI_PID"

nohup caffeinate -is "$PYTHON" terminal6_mlb_paper_trader.py --interval-sec 1800 > terminal6_data/paper_trader.out 2>&1 &
disown
TRADER_PID=$!
echo "    paper trader pid=$TRADER_PID  (DRY-RUN — flip to --live after 24h of sane signals)"

nohup caffeinate -is "$PYTHON" terminal6_mlb_settlement_reconciler.py --interval-sec 3600 > terminal6_data/settlement_reconciler.out 2>&1 &
disown
RECON_PID=$!
echo "    reconciler pid=$RECON_PID"

sleep 3

echo "==> Verifying processes are alive"
for pid in $KALSHI_PID $TRADER_PID $RECON_PID; do
    if ! ps -p "$pid" > /dev/null 2>&1; then
        echo "    ERROR: pid $pid died immediately. Check the corresponding .out file."
    fi
done

echo "==> Final status"
bash "$DOCS/portfolio_status.sh"

echo ""
echo "=================================================="
echo " T6 deployment complete."
echo " Paper trader is in DRY-RUN."
echo " Tail ~/Documents/terminal6_data/paper_trader.log to watch signals."
echo " After 24h of sane dry-run output, run this to flip to live:"
echo "   pkill -f terminal6_mlb_paper_trader.py"
echo "   cd ~/Documents && nohup caffeinate -is $PYTHON terminal6_mlb_paper_trader.py --live --interval-sec 1800 > terminal6_data/paper_trader.out 2>&1 & disown"
echo "=================================================="
