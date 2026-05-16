#!/bin/bash
# Single status command for the entire portfolio.
# Shows: scheduler health, all scheduled jobs, freshness watchdog state,
# and the live nohup daemons (kalshi loggers, paper traders, reconcilers).

set -u

DOCS="$HOME/Documents"
echo "===================================================================="
echo " PORTFOLIO STATUS — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "===================================================================="
echo

echo "--- Scheduler ---"
if [ -f "$DOCS/scheduler.pid" ]; then
    PID=$(cat "$DOCS/scheduler.pid")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "scheduler running (pid=$PID, uptime=$(ps -o etime= -p $PID | xargs))"
    else
        echo "scheduler PID file points to $PID but process is dead"
    fi
else
    echo "scheduler not started (no pid file)"
fi
echo

echo "--- Scheduled jobs (from scheduler_status.json) ---"
if [ -f "$DOCS/scheduler_status.json" ]; then
    /opt/homebrew/bin/python3 - <<'PY'
import json, time
from pathlib import Path
s = json.loads(Path("~/Documents/scheduler_status.json").expanduser().read_text())
print(f"{'job':<28} {'last run':<24} {'exit':>4}  {'next in':>10}")
print("-" * 74)
now = time.time()
for j in s["jobs"]:
    exit_code = j["last_exit_code"]
    if exit_code is None:
        # Anchored at startup but never actually run yet
        last = "(pending first run)"
        exit_str = "—"
    else:
        last = j["last_run_iso"] or "never"
        exit_str = str(exit_code)
    next_in = j["next_run_ts"] - now if j["next_run_ts"] else 0
    if next_in < 0:
        nxt = "OVERDUE"
    elif next_in < 60:
        nxt = f"{int(next_in)}s"
    elif next_in < 3600:
        nxt = f"{int(next_in/60)}m"
    else:
        nxt = f"{next_in/3600:.1f}h"
    print(f"{j['name']:<28} {last:<24} {exit_str:>4}  {nxt:>10}")
PY
else
    echo "(no scheduler_status.json yet)"
fi
echo

echo "--- Freshness watchdog ---"
/opt/homebrew/bin/python3 "$DOCS/portfolio_freshness_watchdog.py" --quiet
if [ -f "$DOCS/freshness_alarm.flag" ]; then
    echo "ALARM ACTIVE:"
    cat "$DOCS/freshness_alarm.flag"
else
    echo "all data paths fresh"
fi
echo

echo "--- Macro concentration cap (50% hard limit) ---"
/opt/homebrew/bin/python3 "$DOCS/portfolio_macro_concentration.py" 2>&1 | tail -n +2
# Capture exit code BEFORE pipeline rather than $? (which would be tail's).
# Exit codes: 0=under, 1=at-cap, 2=over-cap. Write a flag file analogous
# to freshness_alarm.flag so traders/redeploy scripts can refuse to deploy
# new macro engines while breached. Fixed 2026-05-09 (audit H-INFRA-3).
MACRO_EXIT=${PIPESTATUS[0]}
MACRO_FLAG="$DOCS/macro_cap_alarm.flag"
if [ "$MACRO_EXIT" -ge 2 ]; then
    echo "MACRO CAP BREACHED — exit=$MACRO_EXIT (>50% deployed in macro engines)"
    date -u "+%Y-%m-%dT%H:%M:%SZ macro cap exit=$MACRO_EXIT" > "$MACRO_FLAG"
elif [ "$MACRO_EXIT" -eq 1 ]; then
    echo "MACRO CAP AT 50% EXACTLY — no new macro deployment permitted"
    date -u "+%Y-%m-%dT%H:%M:%SZ macro cap exit=$MACRO_EXIT" > "$MACRO_FLAG"
else
    rm -f "$MACRO_FLAG" 2>/dev/null || true
fi
echo

echo "--- Live daemons (nohup) ---"
ps -ax -o pid,etime,command | grep -E "(terminal[123].*paper_trader|terminal[123].*kalshi_logger|terminal[123].*settlement_reconciler|terminal[123].*model_pullers|portfolio_scheduler)" | grep -v grep || echo "(no matching daemons found)"
echo

echo "--- LaunchAgents (legacy — to be migrated into scheduler) ---"
launchctl list | grep -E "terminal[123]|portfolio|t3[bc]|t2\." | awk '{printf "  %-40s pid=%-8s exit=%s\n", $3, $1, $2}' || echo "(none loaded)"
echo "===================================================================="
