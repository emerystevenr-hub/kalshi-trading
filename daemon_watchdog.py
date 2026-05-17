"""Daemon Watchdog — auto-restart silent engine daemons (session 7).

Solves "Steve has to manually run redeploy every time a WS connection
silently drops." For each active engine in engines.json that declares
both a `monitoring.ws_logger_path` and a `monitoring.redeploy_script`,
this watchdog:

  1. Checks ws_logger.log mtime against the ws threshold in
     monitoring.checks (max_age_h on the ws_logger row). Default 0.1h
     (6 min) if no row found.
  2. If stale: bash the engine's redeploy_script, log the restart.
  3. Tracks restart history per engine in state file.
  4. Backoff: 3 restarts in BACKOFF_WINDOW_SEC (1h) → STOP restarting
     AND fire an ALERT email (via daily_status_report) with the
     watchdog context: which engine, restart timestamps, last 20 lines
     of the silent ws_logger.log. Alert throttled to one per hour.

Designed to run via portfolio_scheduler (interval-based, 5 min):

    scheduler_jobs.json:
        {
          "name": "daemon_watchdog",
          "command": "/opt/homebrew/bin/python3 ~/Documents/daemon_watchdog.py --once",
          "interval_sec": 300,
          "timeout_sec": 90,
          "enabled": true
        }

Also runnable by hand:

    python3 ~/Documents/daemon_watchdog.py --once               # do real work
    python3 ~/Documents/daemon_watchdog.py --once --dry-run     # report only
    python3 ~/Documents/daemon_watchdog.py --reset-state        # wipe backoff

SOURCE OF TRUTH: engines.json. To opt an engine out of auto-restart,
remove or null out its `monitoring.redeploy_script`. To change the
staleness threshold, edit `monitoring.checks[<ws row>].max_age_h`.
No code edit required for either.

State file: ~/Documents/scheduler_logs/daemon_watchdog_state.json
Restart audit log: ~/Documents/scheduler_logs/daemon_watchdog_restarts.log
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DOCS = Path.home() / "Documents"
sys.path.insert(0, str(DOCS))

ENGINES_JSON = DOCS / "shadow_pnl" / "engines.json"
STATE_FILE = DOCS / "scheduler_logs" / "daemon_watchdog_state.json"
RESTART_LOG = DOCS / "scheduler_logs" / "daemon_watchdog_restarts.log"

BACKOFF_WINDOW_SEC = 3600            # 1 hour
BACKOFF_MAX_RESTARTS = 3             # 3 restarts in window → backoff
ALERT_THROTTLE_SEC = 3600            # don't re-alert within 1h
DEFAULT_WS_MAX_AGE_H = 0.1           # 6 min if engines.json doesn't specify
RESTART_SUBPROCESS_TIMEOUT_SEC = 60  # bash redeploy must finish in 60s


# ─────────────────────────────────────────────────────────────────────
# IO helpers
# ─────────────────────────────────────────────────────────────────────

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _log(msg: str) -> None:
    """Print to stdout (captured by scheduler_logs/daemon_watchdog.log)."""
    print(f"[{_now_utc_iso()}] {msg}", flush=True)


def _append_restart_audit(line: str) -> None:
    RESTART_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RESTART_LOG, "a") as f:
        f.write(f"[{_now_utc_iso()}] {line}\n")


def _load_engines() -> Dict[str, dict]:
    if not ENGINES_JSON.exists():
        raise FileNotFoundError(f"missing {ENGINES_JSON}")
    return json.loads(ENGINES_JSON.read_text())


def _load_state() -> Dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: Dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def _file_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except (OSError, FileNotFoundError):
        return None


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds/60)}m"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


# ─────────────────────────────────────────────────────────────────────
# Per-engine evaluation
# ─────────────────────────────────────────────────────────────────────

def _ws_threshold_sec(meta: dict) -> float:
    """Find the ws_logger row in monitoring.checks; return its
    max_age_h converted to seconds. Fall back to default."""
    mon = meta.get("monitoring") or {}
    ws_path = mon.get("ws_logger_path") or ""
    for c in mon.get("checks") or []:
        if c.get("path") == ws_path:
            try:
                return float(c["max_age_h"]) * 3600.0
            except (KeyError, TypeError, ValueError):
                continue
    return DEFAULT_WS_MAX_AGE_H * 3600.0


def _watchable_engines(engines: Dict[str, dict]) -> List[Tuple[str, dict]]:
    """Filter to engines that are active, have a ws_logger_path, and have a
    redeploy_script. Order alphabetically for deterministic logs."""
    out = []
    for eid, meta in engines.items():
        if not isinstance(meta, dict) or not meta.get("active"):
            continue
        mon = meta.get("monitoring") or {}
        if not mon.get("ws_logger_path") or not mon.get("redeploy_script"):
            continue
        out.append((eid, meta))
    return sorted(out, key=lambda p: p[0])


def _prune_history(history: List[str]) -> List[str]:
    """Drop restart timestamps older than BACKOFF_WINDOW_SEC."""
    cutoff = _now_utc_ts() - BACKOFF_WINDOW_SEC
    kept = []
    for iso in history:
        try:
            ts = datetime.fromisoformat(iso).timestamp()
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            kept.append(iso)
    return kept


# ─────────────────────────────────────────────────────────────────────
# Restart action
# ─────────────────────────────────────────────────────────────────────

def _run_redeploy(script_path: Path, dry_run: bool) -> Tuple[bool, str]:
    """Run `bash <script_path>`. Return (success, stdout+stderr tail)."""
    if dry_run:
        return True, "(dry-run — would have run script)"
    try:
        result = subprocess.run(
            ["bash", str(script_path)],
            cwd=str(DOCS),
            capture_output=True,
            text=True,
            timeout=RESTART_SUBPROCESS_TIMEOUT_SEC,
        )
        tail = (result.stdout or "") + (result.stderr or "")
        tail = tail[-2000:]  # cap captured output
        return (result.returncode == 0), tail
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {RESTART_SUBPROCESS_TIMEOUT_SEC}s"
    except OSError as e:
        return False, f"OSError running script: {e}"


# ─────────────────────────────────────────────────────────────────────
# Backoff alert (via daily_status_report)
# ─────────────────────────────────────────────────────────────────────

def _send_backoff_alert(engine: str, meta: dict, history: List[str],
                        ws_log_path: Path, dry_run: bool) -> Tuple[bool, str]:
    """Compose an ALERT email with watchdog context + standard daily report.
    Return (sent, message)."""
    import shadow_dashboard  # noqa: E402
    import daily_status_report  # noqa: E402

    state = shadow_dashboard.gather_state()
    _subject_normal, body_normal = daily_status_report.render_report(state)

    # Tail of the silent ws_logger.log
    ws_tail = ""
    try:
        with open(ws_log_path) as f:
            ws_tail = "".join(f.readlines()[-20:])
    except OSError:
        ws_tail = "(could not read ws_logger.log)"

    banner = []
    banner.append("=" * 64)
    banner.append("DAEMON WATCHDOG BACKOFF — auto-restart abandoned")
    banner.append("=" * 64)
    banner.append(f"Engine:           {engine}")
    banner.append(f"Engine name:      {meta.get('name','')}")
    banner.append(f"Restarts in past hour: {len(history)} "
                  f"(threshold {BACKOFF_MAX_RESTARTS})")
    banner.append("Restart timestamps (UTC):")
    for iso in history:
        banner.append(f"  - {iso}")
    banner.append("")
    banner.append(f"Last 20 lines of {ws_log_path.name}:")
    banner.append("-" * 64)
    banner.append(ws_tail.rstrip())
    banner.append("-" * 64)
    banner.append("")
    banner.append("ACTION REQUIRED: WS feed is not recovering after auto-restart.")
    banner.append("Likely causes:")
    banner.append("  - Kalshi WS auth expired (rotate KALSHI_PRIVATE_KEY)")
    banner.append("  - Kalshi-side outage (check status.kalshi.com)")
    banner.append("  - Network/DNS issue on Mac")
    banner.append("")
    banner.append("Watchdog has stopped restarting this engine. Resume manually:")
    banner.append(f"  bash ~/Documents/{meta['monitoring']['redeploy_script']}")
    banner.append("  python3 ~/Documents/daemon_watchdog.py --reset-state")
    banner.append("=" * 64)
    banner.append("")

    body = "\n".join(banner) + body_normal

    date_str = datetime.now(daily_status_report.PT).strftime("%Y-%m-%d")
    subject = f"Kalshi Trading — DAEMON WATCHDOG BACKOFF {engine} — {date_str}"

    if dry_run:
        return True, f"(dry-run — would have sent: {subject!r}, body {len(body)} chars)"

    try:
        env = daily_status_report._load_smtp_env()
        daily_status_report.send_email(subject, body, env)
        return True, f"alert sent: {subject}"
    except FileNotFoundError as e:
        return False, f"cannot send alert — gmail_smtp.env missing: {e}"
    except Exception as e:  # noqa: BLE001 — log the type
        return False, f"cannot send alert — {type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────────────────
# Main per-tick logic
# ─────────────────────────────────────────────────────────────────────

def tick(dry_run: bool) -> int:
    """One scheduler tick. Returns non-zero only on hard errors."""
    try:
        engines = _load_engines()
    except FileNotFoundError as e:
        _log(f"ERROR {e}")
        return 1

    state = _load_state()
    watchable = _watchable_engines(engines)
    if not watchable:
        _log("no watchable engines (no active engine declares ws_logger_path "
             "+ redeploy_script)")
        return 0

    now_ts = _now_utc_ts()
    state_changed = False

    for eid, meta in watchable:
        engine_state = state.setdefault(eid, {"restart_history": [], "last_alert_ts": None})
        engine_state["restart_history"] = _prune_history(engine_state.get("restart_history") or [])

        ws_path = DOCS / meta["monitoring"]["ws_logger_path"]
        threshold_sec = _ws_threshold_sec(meta)

        mt = _file_mtime(ws_path)
        if mt is None:
            _log(f"{eid} ws_logger missing at {ws_path.name} — staleness "
                 f"check FAIL-OPEN (no restart, not stale by definition)")
            continue
        age = now_ts - mt
        if age <= threshold_sec:
            _log(f"{eid} OK ws_logger age={_fmt_age(age)} "
                 f"(threshold {_fmt_age(threshold_sec)})")
            continue

        # Stale path
        history = engine_state["restart_history"]
        reason = (f"ws_logger stale: age={_fmt_age(age)} "
                  f"threshold={_fmt_age(threshold_sec)}")

        if len(history) >= BACKOFF_MAX_RESTARTS:
            # Backoff active — do NOT restart, maybe alert
            last_alert_iso = engine_state.get("last_alert_ts")
            should_alert = True
            if last_alert_iso:
                try:
                    last_alert_ts = datetime.fromisoformat(last_alert_iso).timestamp()
                    should_alert = (now_ts - last_alert_ts) > ALERT_THROTTLE_SEC
                except (TypeError, ValueError):
                    should_alert = True

            _log(f"{eid} BACKOFF active ({len(history)} restarts in past 1h) — "
                 f"NOT restarting. {reason}")

            if should_alert:
                sent, msg = _send_backoff_alert(eid, meta, history, ws_path, dry_run)
                _log(f"{eid} backoff alert: {msg}")
                if sent:
                    engine_state["last_alert_ts"] = _now_utc_iso()
                    state_changed = True
            else:
                _log(f"{eid} alert throttled — last sent {last_alert_iso}, "
                     f"throttle window {ALERT_THROTTLE_SEC}s")
            continue

        # Within budget — restart
        script_path = DOCS / meta["monitoring"]["redeploy_script"]
        if not script_path.exists():
            _log(f"{eid} CANNOT RESTART — redeploy_script not found at "
                 f"{script_path}. Update engines.json.")
            continue

        _log(f"{eid} RESTART  reason: {reason}  via: {script_path.name}")
        if not dry_run:
            _append_restart_audit(f"{eid} reason={reason} script={script_path.name}")
        ok, tail = _run_redeploy(script_path, dry_run)
        _log(f"{eid} restart {'OK' if ok else 'FAILED'}  output_tail:\n{tail[-500:]}")
        if not dry_run:
            _append_restart_audit(f"{eid} {'OK' if ok else 'FAILED'} tail_chars={len(tail)}")

        # Count this attempt regardless of OK/FAIL — failed restarts also
        # consume backoff budget (otherwise we'd loop forever on broken auth).
        # Only persist when not in dry-run.
        engine_state["restart_history"].append(_now_utc_iso())
        state_changed = True

    if state_changed and not dry_run:
        _save_state(state)
    return 0


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Run one tick and exit (scheduler-friendly).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what WOULD happen; do not run redeploy, do not send email, do not update state.")
    ap.add_argument("--reset-state", action="store_true",
                    help="Clear the restart history and last_alert_ts for all engines, then exit.")
    args = ap.parse_args()

    if args.reset_state:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print(f"reset: removed {STATE_FILE}")
        else:
            print("reset: state file did not exist")
        return 0

    if not args.once and not args.dry_run:
        ap.error("must pass --once (scheduler) or --dry-run (testing)")

    return tick(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
