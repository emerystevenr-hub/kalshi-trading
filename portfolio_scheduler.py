"""Portfolio Scheduler — single supervisor for every periodic job.

Born 2026-05-08 to end the parade of broken LaunchAgents, ad-hoc nohup loops,
caffeinate wrappers, and silently-stale pullers. ONE process owns scheduling
for every periodic puller in the portfolio. New jobs go in one config file.
Status is one command. Recovery is one restart.

Design:
  - Reads scheduler_jobs.json: a flat list of {name, command, interval_sec,
    enabled} entries.
  - Loop every CHECK_INTERVAL_SEC. For each enabled job, if `now -
    last_run_ts >= interval_sec`, run it.
  - Each job runs synchronously in a subprocess with a per-job log file at
    scheduler_logs/<name>.log. stdout+stderr both go there.
  - State (last_run_ts, last_exit_code, last_error) persisted to
    scheduler_state.json after every job completion. Survives restart.
  - On startup: do NOT auto-fire missed jobs. Skip to next scheduled tick.
    Prevents thundering-herd on restart.
  - Heartbeat: every loop, refresh scheduler_status.json with full job
    table. portfolio_freshness_watchdog.py and the status script read it.
  - SIGTERM: drain in-flight job, persist state, exit clean.

Usage:
    nohup /opt/homebrew/bin/python3 ~/Documents/portfolio_scheduler.py \\
        > ~/Documents/scheduler.out 2>&1 &
    echo $! > ~/Documents/scheduler.pid

Status:
    bash ~/Documents/portfolio_status.sh

Add a job:
    edit ~/Documents/scheduler_jobs.json, then:
    kill -HUP $(cat ~/Documents/scheduler.pid)   # reload config without restart
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

DOCS = Path.home() / "Documents"
JOBS_FILE = DOCS / "scheduler_jobs.json"
STATE_FILE = DOCS / "scheduler_state.json"
STATUS_FILE = DOCS / "scheduler_status.json"
LOG_DIR = DOCS / "scheduler_logs"
PID_FILE = DOCS / "scheduler.pid"

CHECK_INTERVAL_SEC = 30
SHUTDOWN = False
RELOAD_CONFIG = False


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def now_ts() -> float:
    return time.time()


def slog(msg: str) -> None:
    print(f"[{now_utc()}] [scheduler] {msg}", flush=True)


def load_jobs() -> List[dict]:
    if not JOBS_FILE.exists():
        slog(f"WARN: {JOBS_FILE} missing — no jobs configured")
        return []
    try:
        data = json.loads(JOBS_FILE.read_text())
    except json.JSONDecodeError as e:
        slog(f"ERROR: jobs config invalid JSON: {e}")
        return []
    jobs = data.get("jobs", [])
    valid = []
    for j in jobs:
        if not all(k in j for k in ("name", "command", "interval_sec")):
            slog(f"WARN: job {j!r} missing required keys — skipping")
            continue
        if not j.get("enabled", True):
            continue
        valid.append(j)
    return valid


def load_state() -> Dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        slog("WARN: state file corrupt, starting fresh")
        return {}


def save_state(state: Dict[str, dict]) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def write_status(jobs: List[dict], state: Dict[str, dict]) -> None:
    rows = []
    for j in jobs:
        s = state.get(j["name"], {})
        last_ts = s.get("last_run_ts")
        rows.append({
            "name": j["name"],
            "command": j["command"],
            "interval_sec": j["interval_sec"],
            "last_run_ts": last_ts,
            "last_run_iso": (datetime.fromtimestamp(last_ts, tz=timezone.utc)
                             .strftime("%Y-%m-%d %H:%M:%S UTC")
                             if last_ts else None),
            "last_exit_code": s.get("last_exit_code"),
            "last_duration_sec": s.get("last_duration_sec"),
            "last_error": s.get("last_error"),
            "next_run_ts": (last_ts + j["interval_sec"]) if last_ts else now_ts(),
        })
    status = {
        "scheduler_pid": os.getpid(),
        "scheduler_uptime_sec": now_ts() - START_TS,
        "now_utc": now_utc(),
        "jobs": rows,
    }
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2))
    tmp.replace(STATUS_FILE)


def run_job(job: dict) -> tuple[int, float, Optional[str]]:
    """Execute one job. Returns (exit_code, duration_sec, error_msg_or_None)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{job['name']}.log"
    cmd = os.path.expanduser(job["command"])
    started = now_ts()
    header = f"\n[{now_utc()}] === scheduler firing: {job['name']} ===\n[cmd] {cmd}\n"
    try:
        with open(log_path, "a") as f:
            f.write(header)
            f.flush()
            proc = subprocess.run(
                cmd,
                shell=True,
                stdout=f,
                stderr=subprocess.STDOUT,
                cwd=str(DOCS),
                timeout=job.get("timeout_sec", 600),
            )
        return proc.returncode, now_ts() - started, None
    except subprocess.TimeoutExpired:
        return 124, now_ts() - started, "timeout"
    except Exception as e:
        return -1, now_ts() - started, str(e)


def handle_sigterm(signum, frame):
    global SHUTDOWN
    slog(f"received signal {signum} — shutting down")
    SHUTDOWN = True


def handle_sighup(signum, frame):
    global RELOAD_CONFIG
    slog("received SIGHUP — will reload config on next loop")
    RELOAD_CONFIG = True


START_TS = now_ts()


def main() -> int:
    PID_FILE.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGHUP, handle_sighup)

    slog(f"starting (pid={os.getpid()})")
    jobs = load_jobs()
    state = load_state()
    slog(f"loaded {len(jobs)} jobs, {len(state)} known last-run records")

    # Don't auto-fire on startup. Anchor "last run" to now for any unknown
    # job — the next legitimate fire is interval_sec from now.
    anchor_ts = now_ts()
    for j in jobs:
        if j["name"] not in state:
            state[j["name"]] = {"last_run_ts": anchor_ts}
    save_state(state)
    write_status(jobs, state)

    global RELOAD_CONFIG
    while not SHUTDOWN:
        if RELOAD_CONFIG:
            jobs = load_jobs()
            slog(f"reloaded config: {len(jobs)} jobs")
            RELOAD_CONFIG = False

        for j in jobs:
            if SHUTDOWN:
                break
            # Per-job exception fence: a bad config row, a corrupt state
            # write, or a transient ENOSPC must not take down the supervisor.
            # run_job() already has its own try/except for the subprocess;
            # this fence wraps state-mutation and logging too. Fixed
            # 2026-05-09 (audit H-INFRA-4).
            try:
                s = state.get(j["name"], {})
                last_ts = s.get("last_run_ts", 0)
                if now_ts() - last_ts < j["interval_sec"]:
                    continue
                slog(f"fire {j['name']}")
                code, duration, err = run_job(j)
                state[j["name"]] = {
                    "last_run_ts": now_ts(),
                    "last_exit_code": code,
                    "last_duration_sec": round(duration, 2),
                    "last_error": err,
                }
                save_state(state)
                slog(f"done  {j['name']}: exit={code} dur={duration:.1f}s "
                     f"{'err='+err if err else ''}")
            except Exception as e:
                slog(f"[fatal-protect] job={j.get('name','?')} "
                     f"raised {type(e).__name__}: {e} — continuing")
                continue

        write_status(jobs, state)
        # Sleep in 1-second slices so SIGTERM and SIGHUP are responsive.
        for _ in range(CHECK_INTERVAL_SEC):
            if SHUTDOWN or RELOAD_CONFIG:
                break
            time.sleep(1)

    slog("exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
