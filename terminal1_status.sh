#!/bin/bash
# Terminal 1 Status Dashboard.
# Shows live pipeline state — processes, last snap, data files, record counts.
# Run directly (one-shot), or use `watch` to auto-refresh:
#     watch -n 30 ~/Documents/terminal1_status.sh

DATA=~/Documents/terminal1_data
LOG=~/Documents/terminal1_logger.log

echo "========================================================================"
echo "  TERMINAL 1 STATUS  — $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "========================================================================"

echo ""
echo "--- PROCESSES ---"
PROCS=$(ps aux | grep terminal1_kalshi_logger | grep -v grep)
if [ -z "$PROCS" ]; then
    echo "  [DEAD] kalshi logger is NOT running"
else
    echo "$PROCS" | awk '{printf "  [ALIVE] PID=%s  started=%s  cmd=%s %s\n", $2, $9, $(NF-1), $NF}'
fi

echo ""
echo "--- LAST 3 SNAPS ---"
if [ -f "$LOG" ]; then
    grep 'snap #' "$LOG" | tail -3 | sed 's/^/  /'
else
    echo "  (no log file yet)"
fi

echo ""
echo "--- KALSHI MARKET DATA ---"
if ls $DATA/kalshi_*.jsonl 2>/dev/null > /dev/null; then
    for f in $DATA/kalshi_*.jsonl; do
        n=$(wc -l < "$f" | tr -d ' ')
        sz=$(ls -lh "$f" | awk '{print $5}')
        printf "  %-40s  %6s records  %6s\n" "$(basename $f)" "$n" "$sz"
    done
else
    echo "  (no kalshi files yet)"
fi

echo ""
echo "--- FORECAST MODEL DATA ---"
if ls $DATA/forecasts_*.jsonl 2>/dev/null > /dev/null; then
    for f in $DATA/forecasts_*.jsonl; do
        n=$(wc -l < "$f" | tr -d ' ')
        sz=$(ls -lh "$f" | awk '{print $5}')
        printf "  %-40s  %6s records  %6s\n" "$(basename $f)" "$n" "$sz"
    done
else
    echo "  (no forecast files yet)"
fi

echo ""
echo "--- NWS ACTUALS ---"
if ls $DATA/nws_actuals_*.jsonl 2>/dev/null > /dev/null; then
    for f in $DATA/nws_actuals_*.jsonl; do
        n=$(wc -l < "$f" | tr -d ' ')
        printf "  %-40s  %6s records\n" "$(basename $f)" "$n"
    done
else
    echo "  (no actuals yet)"
fi

echo ""
echo "--- TOTAL DATA SIZE ---"
du -sh $DATA 2>/dev/null | awk '{print "  " $0}'
echo ""
