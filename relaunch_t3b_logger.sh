#!/usr/bin/env bash
# Relaunch T3b kalshi logger as a session-detached process.
# Mirrors redeploy_t7_all.sh's detach() pattern: double-fork + setsid + FD redirect.
# Created 2026-05-16 (session 6) after launchd plist install hit exit 78.
# TODO next session: get com.t3b.kalshi-logger.plist working under launchd properly.

set -u
PYTHON=/opt/homebrew/bin/python3
DOCS=/Users/stevenemery/Documents
SCRIPT="$DOCS/terminal3b_kalshi_logger.py"
OUT="$DOCS/terminal3b_data/kalshi_logger.out"

echo "==> Killing any existing T3b kalshi logger processes"
pkill -f terminal3b_kalshi_logger 2>/dev/null || true
sleep 2

mkdir -p "$DOCS/terminal3b_data"

echo "==> Launching session-detached (double-fork + setsid)"
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
' "$OUT" caffeinate -is "$PYTHON" "$SCRIPT" --interval-sec 300

sleep 4

echo
echo "==> Verify (TTY must be ?? — fully session-detached)"
PIDS=$(pgrep -f "terminal3b_kalshi_logger.py")
if [ -z "$PIDS" ]; then
    echo "    FAIL: no terminal3b_kalshi_logger.py process found"
    exit 1
fi
for pid in $PIDS; do
    tty=$(ps -o tty= -p "$pid" | tr -d ' ')
    if [ "$tty" = "??" ]; then
        echo "    pid=$pid TTY=$tty OK"
    else
        echo "    WARN: pid=$pid TTY=$tty (still attached — setsid failed)"
    fi
done

echo
echo "==> Latest log activity"
tail -3 "$DOCS/terminal3b_data/kalshi_logger.log"
