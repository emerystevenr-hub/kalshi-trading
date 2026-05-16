#!/bin/bash
# Terminal 1 — 30-day Model Backfill Launcher.
#
# Kicks off 4 parallel backfill processes (one per model), each:
#   - 30 days of history
#   - 2 cycles/day (00Z + 12Z)
#   - 6 horizons per cycle (12h, 24h, 36h, 48h, 60h, 72h)
#   - Wrapped in nohup + caffeinate
#
# Expected total runtime: 2-3 hours (parallel, network-bound).
#
# Monitor progress:
#   tail -f ~/Documents/terminal1_backfill_{gfs,hrrr,ecmwf_hres,aifs}.log
#   ~/Documents/terminal1_status.sh
#
# Cancel all:
#   pkill -f "terminal1_model_pullers.*--backfill"

set -e

BACKFILL_DAYS=30
HORIZONS="12,24,36,48,60,72"
CYCLES="0,12"

cd ~/Documents

echo "Terminal 1 — 30-day backfill starting at $(date)"
echo "  Models: gfs, hrrr, ecmwf_hres, aifs"
echo "  Days:   $BACKFILL_DAYS"
echo "  Horizons: $HORIZONS hours"
echo "  Cycles: $CYCLES UTC"
echo ""

for MODEL in gfs hrrr ecmwf_hres aifs; do
    LOGFILE=~/Documents/terminal1_backfill_${MODEL}.log
    # Truncate previous run's log so tail only shows current run.
    : > "$LOGFILE"
    nohup caffeinate -is python3 ~/Documents/terminal1_model_pullers.py \
        --model "$MODEL" \
        --backfill-days "$BACKFILL_DAYS" \
        --horizons "$HORIZONS" \
        --cycle-hours "$CYCLES" \
        > "$LOGFILE" 2>&1 &
    PID=$!
    echo "  [launched] $MODEL (PID $PID) → $LOGFILE"
done

echo ""
echo "All 4 backfills running in background. Safe to close this terminal."
echo ""
echo "To monitor:   ~/Documents/terminal1_status.sh"
echo "To stop all:  pkill -f 'terminal1_model_pullers.*--backfill'"
