#!/bin/bash
# Start T3c observation daemons:
#   1. Kalshi market logger — 5min snaps to disk (zero-risk, builds time series)
#   2. Paper trader in DRY-RUN mode — 30min cycle, writes signals to log
#
# Both run via `nohup caffeinate -is` so they survive lid-close and logout.
# Use stop_t3c_watch.sh to terminate.

set -u

DOC="$HOME/Documents"
DATA="$DOC/terminal3c_data"
PY="/opt/homebrew/bin/python3"

if [[ ! -x "$PY" ]]; then
    PY="$(which python3)"
fi

mkdir -p "$DATA"

# Guard: only one of each daemon
if pgrep -f "terminal3c_kalshi_logger.py" >/dev/null; then
    echo "WARNING: terminal3c_kalshi_logger.py is already running (PID $(pgrep -f terminal3c_kalshi_logger.py))"
else
    nohup caffeinate -is "$PY" "$DOC/terminal3c_kalshi_logger.py" \
        > "$DATA/kalshi_logger.out" 2>&1 &
    echo "Started kalshi_logger (PID $!) — 5min snap cadence, output → kalshi_logger.out"
fi

if pgrep -f "terminal3c_paper_trader.py" >/dev/null; then
    echo "WARNING: terminal3c_paper_trader.py is already running (PID $(pgrep -f terminal3c_paper_trader.py))"
else
    nohup caffeinate -is "$PY" "$DOC/terminal3c_paper_trader.py" \
        --dry-run --interval-sec 1800 \
        > "$DATA/paper_trader.out" 2>&1 &
    echo "Started paper_trader DRY-RUN (PID $!) — 30min cycle, signals → paper_trader.log"
fi

echo
echo "=== verify ==="
sleep 2
ps aux | grep -E "terminal3c_(kalshi_logger|paper_trader)" | grep -v grep | awk '{print $2"\t"$11"\t"$12"\t"$13"\t"$14"\t"$15"\t"$16}'

echo
echo "=== first signals (will populate after first 30min cycle) ==="
echo "Watch:    tail -f $DATA/paper_trader.log"
echo "Stability:python3 $DOC/terminal3c_stability_check.py"
echo "Stop:     bash $DOC/stop_t3c_watch.sh"
