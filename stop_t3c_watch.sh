#!/bin/bash
# Stop T3c watch daemons cleanly (SIGTERM → finishes current iteration → exits).

for proc in terminal3c_kalshi_logger.py terminal3c_paper_trader.py; do
    pids=$(pgrep -f "$proc" || true)
    if [[ -z "$pids" ]]; then
        echo "$proc: not running"
        continue
    fi
    for pid in $pids; do
        echo "$proc: SIGTERM → PID $pid"
        kill -TERM "$pid" 2>/dev/null || true
    done
done

sleep 2
echo
echo "=== status ==="
for proc in terminal3c_kalshi_logger.py terminal3c_paper_trader.py; do
    if pgrep -f "$proc" >/dev/null; then
        echo "$proc: STILL RUNNING ($(pgrep -f $proc))"
    else
        echo "$proc: stopped"
    fi
done
