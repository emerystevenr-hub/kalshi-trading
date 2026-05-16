"""Shadow P&L Dashboard — fixed-layout terminal scoreboard (session 7 rewrite).

Single 80x24 screen. Refreshes in place every 30 seconds (configurable).
No scrolling, ever. Archived engines (T1, T2, T4, T5) never appear.

Layout:
  HEADER     portfolio total clean realized | macro cap % | Odds API | UTC
  ENGINES    T3a | T3b | T3c | T6 | T7 — one line each
             columns: mode | clean realized | W/L | open | last fire | gate
  ALERTS     daemon down, 401s, cap breach, freshness alarm, entropy event
             Empty box collapses to a single green "NO ALERTS" line.
  FOOTER     last refresh | next refresh in Xs

T6 P&L, W/L, and gate are sourced from terminal6_milestone_check (clean
numbers only — never raw ledger). T3a's "PAUSED until <date>" line is
driven by the presence of ~/Documents/t3a_disabled_until.flag (auto-cleared
by the Cowork scheduled task t3a-fed-scanner-relaunch-jun10 on 2026-06-10).

Usage:
    python3 ~/Documents/shadow_dashboard.py                # 30s refresh
    python3 ~/Documents/shadow_dashboard.py --refresh-sec 10
    python3 ~/Documents/shadow_dashboard.py --once         # dump JSON, no curses
    python3 ~/Documents/shadow_dashboard.py --plain        # no-curses fallback

Reads (never writes):
  ~/Documents/shadow_pnl/engines.json
  ~/Documents/shadow_pnl/ledger.jsonl
  ~/Documents/scheduler_status.json
  ~/Documents/scheduler.pid
  ~/Documents/freshness_alarm.flag
  ~/Documents/macro_cap_alarm.flag
  ~/Documents/t3a_disabled_until.flag
  ~/Documents/entropy_alerts.jsonl
  ~/Documents/scheduler_logs/t6_mlb_lines_puller.log
  ~/Documents/terminal{3a,3b,6,7}_data/*.log

Imports:
  terminal6_milestone_check  (clean T6 P&L + gate — single source of truth)
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Single source of truth for clean T6 numbers + gate verdict. See
# terminal6_dashboard.py session-7 refactor commentary for the rationale.
from terminal6_milestone_check import (
    load_t6_closed,
    _is_contaminated,
    compute_stats,
    evaluate_gate,
)

DOCS = Path.home() / "Documents"
SHADOW_DIR = DOCS / "shadow_pnl"
LEDGER = SHADOW_DIR / "ledger.jsonl"
ENGINES_JSON = SHADOW_DIR / "engines.json"
SCHED_STATUS = DOCS / "scheduler_status.json"
SCHED_PID = DOCS / "scheduler.pid"
FRESHNESS_FLAG = DOCS / "freshness_alarm.flag"
MACRO_FLAG = DOCS / "macro_cap_alarm.flag"
T3A_PAUSE_FLAG = DOCS / "t3a_disabled_until.flag"
ENTROPY_ALERTS = DOCS / "entropy_alerts.jsonl"
T6_LINES_LOG = DOCS / "scheduler_logs" / "t6_mlb_lines_puller.log"

# Per-engine logger paths (used for "last fire" + daemon-down detection).
# Loggers tick every 5 min; >15 min stale triggers a daemon-down alert.
# Session 7 final: T3b + T3c archived. T3a daemon killed but bankroll placeholder
# retained as the sole macro engine. ENGINE_LOGS keeps T3a's path so the "paused"
# status line still resolves; the daemon-down alert is suppressed via the pause flag.
ENGINE_LOGS = {
    "T3a": DOCS / "terminal3a_data" / "fed_scanner.log",
    "T6":  DOCS / "terminal6_data"  / "kalshi_logger.log",
    "T7":  DOCS / "terminal7_data"  / "kalshi_logger.log",
}

# Engines that ALWAYS render (in order). T1/T2/T3b/T3c/T4/T5 never appear.
# Session 7 archived T3b + T3c with bankroll reallocated to T6 ($13K → $18K).
ACTIVE_ENGINE_ORDER = ["T3a", "T6", "T7"]

DAEMON_STALE_SEC = 15 * 60   # logger silent > 15m = daemon down
ENTROPY_RECENT_SEC = 60 * 60  # entropy event from last hour = alert
ODDS_API_LOW_CREDITS = 50    # < 50 remaining = alert


# ─────────────────────────────────────────────────────────────────────
# Helpers (pure — no curses, no I/O side-effects)
# ─────────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _file_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except (OSError, FileNotFoundError):
        return None


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds/60)}m ago"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h ago"
    return f"{seconds/86400:.1f}d ago"


def _proc_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────
# Per-engine stats from the shadow_pnl ledger
# ─────────────────────────────────────────────────────────────────────

def _engine_ledger_stats(ledger: List[dict], engine: str) -> Tuple[float, int, int, int]:
    """Return (realized_pnl, wins, losses, open_count) for one engine
    using raw ledger only. T6 is overridden by milestone_check in the caller."""
    opens: Dict[str, dict] = {}
    closed_pids: set = set()
    pnl = 0.0
    wins = losses = 0
    for r in ledger:
        if r.get("engine") != engine:
            continue
        t = r.get("type")
        pid = r.get("position_id")
        if t == "open" and pid:
            opens[pid] = r
        elif t == "close" and pid:
            closed_pids.add(pid)
            v = float(r.get("realized_pnl_usd") or 0)
            pnl += v
            if v > 0:
                wins += 1
            elif v < 0:
                losses += 1
    open_count = sum(1 for pid in opens if pid not in closed_pids)
    return round(pnl, 2), wins, losses, open_count


def _t6_clean_stats() -> Tuple[float, int, int, int, dict]:
    """Clean T6 numbers + gate, sourced from terminal6_milestone_check.
    Returns (clean_total_pnl, wins, losses, open_count, gate_dict)."""
    all_closes = load_t6_closed()
    clean = [c for c in all_closes if not _is_contaminated(c)]
    stats = compute_stats(clean)
    gate = evaluate_gate(stats)
    # Open positions: from ledger — count T6 opens with no matching close.
    open_pids: Dict[str, bool] = {}
    closed_pids: set = set()
    for r in _read_jsonl(LEDGER):
        if r.get("engine") != "T6":
            continue
        pid = r.get("position_id")
        if not pid:
            continue
        if r.get("type") == "open":
            open_pids[pid] = True
        elif r.get("type") == "close":
            closed_pids.add(pid)
    open_count = sum(1 for pid in open_pids if pid not in closed_pids)
    return round(stats["total_pnl"], 2), stats["wins"], stats["losses"], open_count, gate


# ─────────────────────────────────────────────────────────────────────
# Macro cap %
# ─────────────────────────────────────────────────────────────────────

def _macro_cap_pct(engines: dict) -> Optional[float]:
    """% of TOTAL deployed bankroll allocated to macro-category engines.
    Returns None if engines.json has no usable bankroll fields."""
    macro = 0.0
    total = 0.0
    for eid, meta in engines.items():
        if not meta.get("active"):
            continue
        bk = float(meta.get("bankroll_usd") or 0)
        if bk <= 0:
            continue
        total += bk
        if meta.get("category") == "macro":
            macro += bk
    if total <= 0:
        return None
    return 100.0 * macro / total


# ─────────────────────────────────────────────────────────────────────
# Odds API credits — tail t6 lines puller log for "remaining=N"
# ─────────────────────────────────────────────────────────────────────

_REMAIN_RE = re.compile(r"remaining=(\d+)")


def _odds_api_credits() -> Optional[int]:
    if not T6_LINES_LOG.exists():
        return None
    try:
        with open(T6_LINES_LOG) as f:
            # Tail the last ~16KB and scan for the last remaining=N
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 16384))
            blob = f.read()
    except OSError:
        return None
    last = None
    for m in _REMAIN_RE.finditer(blob):
        last = int(m.group(1))
    return last


# ─────────────────────────────────────────────────────────────────────
# Last-fire per engine
# ─────────────────────────────────────────────────────────────────────

def _scheduler_last_run(sched_status: dict, job_name: str) -> Optional[float]:
    for j in sched_status.get("jobs", []):
        if j.get("name") == job_name:
            return j.get("last_run_ts")
    return None


def _last_fire_age(engine: str, sched_status: dict) -> Optional[float]:
    """Seconds since the engine's most recent activity, or None if unknown.
    Priority: continuous logger mtime > scheduler job last_run_ts."""
    log = ENGINE_LOGS.get(engine)
    if log:
        mt = _file_mtime(log)
        if mt is not None:
            return time.time() - mt
    # Fall back to scheduler job for engines without a continuous logger.
    job_map = {
        "T3c": "t3c_claims_data",
        "T6":  "t6_mlb_lines_puller",  # only used if T6 logger file missing
    }
    job = job_map.get(engine)
    if job:
        ts = _scheduler_last_run(sched_status, job)
        if ts:
            return time.time() - ts
    return None


# ─────────────────────────────────────────────────────────────────────
# Alerts
# ─────────────────────────────────────────────────────────────────────

def _build_alerts(state: dict) -> List[Tuple[str, str]]:
    """Return list of (severity, message). severity in {'red','yellow'}.
    Order is fixed-priority: cap > freshness > daemon-down > odds > entropy > scheduler."""
    alerts: List[Tuple[str, str]] = []

    # Cap breach
    if MACRO_FLAG.exists():
        try:
            msg = MACRO_FLAG.read_text().strip().splitlines()[0]
        except OSError:
            msg = "macro cap breached"
        alerts.append(("red", f"MACRO CAP: {msg[:60]}"))

    # Freshness
    if FRESHNESS_FLAG.exists():
        try:
            msg = FRESHNESS_FLAG.read_text().strip().splitlines()[0]
        except OSError:
            msg = "freshness alarm raised"
        alerts.append(("red", f"FRESHNESS: {msg[:60]}"))

    # Scheduler process down
    if SCHED_PID.exists():
        try:
            pid = int(SCHED_PID.read_text().strip())
            if not _proc_alive(pid):
                alerts.append(("red", f"SCHEDULER DOWN: pid {pid} not running"))
        except (ValueError, OSError):
            pass

    # Daemon down (logger silent > 15 min). T3a skipped if paused.
    paused_engines = {"T3a"} if T3A_PAUSE_FLAG.exists() else set()
    for eng in ACTIVE_ENGINE_ORDER:
        if eng in paused_engines:
            continue
        log = ENGINE_LOGS.get(eng)
        if not log:
            continue
        mt = _file_mtime(log)
        if mt is None:
            # Logger file never existed — only alert if engine is active in engines.json
            if state["engines_meta"].get(eng, {}).get("active"):
                alerts.append(("yellow", f"{eng} LOGGER: no log file at {log.name}"))
            continue
        age = time.time() - mt
        if age > DAEMON_STALE_SEC:
            alerts.append(("red", f"{eng} DAEMON DOWN: logger silent {_fmt_age(age)}"))

    # Odds API low credits
    credits = state.get("odds_api_credits")
    if credits is not None and credits < ODDS_API_LOW_CREDITS:
        alerts.append(("red", f"ODDS API: only {credits} credits remaining"))

    # Entropy event in last hour with non-noise level
    for rec in reversed(_read_jsonl(ENTROPY_ALERTS)[-200:]):
        if rec.get("alert_level") in (None, "noise"):
            continue
        ts_str = rec.get("ts") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except (TypeError, ValueError):
            continue
        if age > ENTROPY_RECENT_SEC:
            break  # records are time-ordered; older ones irrelevant
        alerts.append(("yellow", f"ENTROPY: {rec.get('engine')} {rec.get('ticker','?')[:30]} "
                                 f"z={rec.get('z_score',0):.1f} {_fmt_age(age)}"))
        break  # only show the most recent

    # Scheduler job non-zero exit
    for j in state.get("scheduler_status", {}).get("jobs", []):
        ec = j.get("last_exit_code")
        if ec is not None and ec != 0:
            alerts.append(("yellow",
                           f"SCHEDULER JOB: {j['name']} last exit={ec}"))

    return alerts[:10]  # hard cap so alert box can't grow past the screen


# ─────────────────────────────────────────────────────────────────────
# State assembly
# ─────────────────────────────────────────────────────────────────────

def gather_state() -> dict:
    engines_meta = _read_json(ENGINES_JSON)
    ledger = _read_jsonl(LEDGER)
    sched = _read_json(SCHED_STATUS)
    credits = _odds_api_credits()
    cap_pct = _macro_cap_pct(engines_meta)

    # Per-engine rows
    per_engine: Dict[str, dict] = {}
    portfolio_realized = 0.0
    for eid in ACTIVE_ENGINE_ORDER:
        meta = engines_meta.get(eid, {})
        if eid == "T6":
            pnl, wins, losses, open_count, gate = _t6_clean_stats()
            gate_str = f"{gate['gate']} n={wins+losses}/300"
        else:
            pnl, wins, losses, open_count = _engine_ledger_stats(ledger, eid)
            gate_str = "active" if meta.get("active") else "idle"

        # Mode override: T3a paused flag wins.
        if eid == "T3a" and T3A_PAUSE_FLAG.exists():
            mode = "paused"
            try:
                until = T3A_PAUSE_FLAG.read_text().splitlines()[0].strip()
                # Pretty-print "2026-06-10T16:00:00Z" → "2026-06-10"
                gate_str = f"PAUSED until {until[:10]}"
            except OSError:
                gate_str = "PAUSED"
        else:
            mode = meta.get("mode") or ("active" if meta.get("active") else "idle")

        age_sec = _last_fire_age(eid, sched)
        per_engine[eid] = {
            "mode": (mode or "?")[:8],
            "realized": pnl,
            "wins": wins,
            "losses": losses,
            "open": open_count,
            "last_fire": _fmt_age(age_sec) if age_sec is not None else "—",
            "last_fire_sec": age_sec,
            "gate": gate_str[:24],
        }
        portfolio_realized += pnl

    state = {
        "now_utc": datetime.now(timezone.utc),
        "portfolio_realized": round(portfolio_realized, 2),
        "macro_cap_pct": cap_pct,
        "odds_api_credits": credits,
        "engines_meta": engines_meta,
        "scheduler_status": sched,
        "per_engine": per_engine,
    }
    state["alerts"] = _build_alerts(state)
    return state


# ─────────────────────────────────────────────────────────────────────
# Curses render
# ─────────────────────────────────────────────────────────────────────

# Color pair indexes
C_DEFAULT = 0
C_GREEN = 1
C_RED = 2
C_YELLOW = 3
C_CYAN = 4
C_DIM = 5


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_GREEN,  curses.COLOR_GREEN,  -1)
    curses.init_pair(C_RED,    curses.COLOR_RED,    -1)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_CYAN,   curses.COLOR_CYAN,   -1)
    curses.init_pair(C_DIM,    curses.COLOR_WHITE,  -1)


def _pnl_attr(v: float) -> int:
    if v > 0:
        return curses.color_pair(C_GREEN)
    if v < 0:
        return curses.color_pair(C_RED)
    return curses.A_NORMAL


def _safe_addstr(win, row, col, text, attr=0) -> None:
    """addstr that silently clips at the right edge instead of raising."""
    h, w = win.getmaxyx()
    if row < 0 or row >= h or col < 0 or col >= w:
        return
    try:
        win.addnstr(row, col, text, max(0, w - col - 1), attr)
    except curses.error:
        pass


def render(stdscr, state: dict, refresh_sec: int, next_in: int) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    # Layout assumes 80x24 minimum; gracefully bail if too small.
    if h < 16 or w < 78:
        _safe_addstr(stdscr, 0, 0,
                     f"terminal too small: need ≥80x24, got {w}x{h}",
                     curses.color_pair(C_RED))
        stdscr.refresh()
        return

    # ── HEADER ──────────────────────────────────────────────────────
    now_s = state["now_utc"].strftime("%Y-%m-%d %H:%M:%S UTC")
    realized = state["portfolio_realized"]
    cap = state["macro_cap_pct"]
    credits = state["odds_api_credits"]

    cap_str = f"{cap:.1f}%" if cap is not None else "?"
    credits_str = f"{credits}" if credits is not None else "?"

    _safe_addstr(stdscr, 0, 0, " " * (w - 1), curses.A_REVERSE)
    _safe_addstr(stdscr, 0, 1, " PORTFOLIO ",
                 curses.A_REVERSE | curses.A_BOLD)
    _safe_addstr(stdscr, 0, 12, f"realized=", curses.A_REVERSE)
    _safe_addstr(stdscr, 0, 21, f"${realized:+9.2f}",
                 curses.A_REVERSE | _pnl_attr(realized))
    _safe_addstr(stdscr, 0, 32, f"  macro={cap_str:>6}", curses.A_REVERSE)
    _safe_addstr(stdscr, 0, 48, f"  odds={credits_str:>5}", curses.A_REVERSE)
    _safe_addstr(stdscr, 0, 62, f"  {now_s}", curses.A_REVERSE)

    # ── ENGINE TABLE ────────────────────────────────────────────────
    row = 2
    hdr = f" {'ENG':<4} {'mode':<8} {'clean_real':>11}  {'W/L':>7}  {'open':>4}  {'last_fire':<10}  {'gate':<24}"
    _safe_addstr(stdscr, row, 0, hdr, curses.A_BOLD | curses.color_pair(C_CYAN))
    row += 1
    _safe_addstr(stdscr, row, 0, "─" * (w - 1), curses.color_pair(C_DIM))
    row += 1

    for eid in ACTIVE_ENGINE_ORDER:
        e = state["per_engine"][eid]
        wl = f"{e['wins']}/{e['losses']}"
        # Build the row in segments so we can color the P&L cell only.
        _safe_addstr(stdscr, row, 0, f" {eid:<4} {e['mode']:<8} ")
        _safe_addstr(stdscr, row, 15, f"${e['realized']:>+10.2f}",
                     _pnl_attr(e['realized']))
        _safe_addstr(stdscr, row, 27,
                     f"  {wl:>7}  {e['open']:>4}  "
                     f"{e['last_fire']:<10}  {e['gate']:<24}")

        # Highlight gate cell for T6 if early-kill or dead
        if eid == "T6" and ("KILL" in e["gate"] or "DEAD" in e["gate"]):
            _safe_addstr(stdscr, row, 50, f"{e['gate']:<24}",
                         curses.color_pair(C_RED) | curses.A_BOLD)
        elif eid == "T3a" and "PAUSED" in e["gate"]:
            _safe_addstr(stdscr, row, 50, f"{e['gate']:<24}",
                         curses.color_pair(C_YELLOW))
        row += 1

    # ── ALERTS ──────────────────────────────────────────────────────
    row += 1
    _safe_addstr(stdscr, row, 0, "─" * (w - 1), curses.color_pair(C_DIM))
    row += 1
    _safe_addstr(stdscr, row, 0, " ALERTS",
                 curses.A_BOLD | curses.color_pair(C_CYAN))
    row += 1

    alerts = state["alerts"]
    if not alerts:
        _safe_addstr(stdscr, row, 1, "✓ NO ALERTS",
                     curses.color_pair(C_GREEN) | curses.A_BOLD)
        row += 1
    else:
        for sev, msg in alerts:
            if row >= h - 2:
                _safe_addstr(stdscr, row, 1,
                             f"… {len(alerts) - (row - (h-2-len(alerts)))} more (truncated)",
                             curses.color_pair(C_DIM))
                row += 1
                break
            attr = curses.color_pair(C_RED) if sev == "red" else curses.color_pair(C_YELLOW)
            _safe_addstr(stdscr, row, 1, f"⚠ {msg}", attr)
            row += 1

    # ── FOOTER (always last line) ───────────────────────────────────
    foot_row = h - 1
    foot = (f" refreshed {state['now_utc'].strftime('%H:%M:%S')} UTC   "
            f"next in {next_in:>2}s   Ctrl+C to exit")
    _safe_addstr(stdscr, foot_row, 0, " " * (w - 1),
                 curses.A_REVERSE | curses.color_pair(C_DIM))
    _safe_addstr(stdscr, foot_row, 0, foot,
                 curses.A_REVERSE | curses.color_pair(C_DIM))

    stdscr.refresh()


# ─────────────────────────────────────────────────────────────────────
# Plain (no-curses) fallback render — for ssh/CI/log-pipe contexts
# ─────────────────────────────────────────────────────────────────────

def render_plain(state: dict, refresh_sec: int) -> str:
    """ANSI-color plain text render, used when curses is unavailable
    (e.g., output is a pipe). Same layout, but printed each tick instead
    of in-place redraw."""
    ESC = "\x1b["
    GREEN, RED, YELLOW, CYAN, BOLD, DIM, RESET = (
        f"{ESC}32m", f"{ESC}31m", f"{ESC}33m", f"{ESC}36m",
        f"{ESC}1m", f"{ESC}2m", f"{ESC}0m",
    )
    cap = state["macro_cap_pct"]
    credits = state["odds_api_credits"]
    realized = state["portfolio_realized"]
    pnl_col = GREEN if realized > 0 else (RED if realized < 0 else "")
    out = []
    out.append(f"{BOLD}{CYAN}PORTFOLIO{RESET}  "
               f"realized={pnl_col}${realized:+.2f}{RESET}  "
               f"macro={(f'{cap:.1f}%' if cap else '?')}  "
               f"odds={credits if credits is not None else '?'}  "
               f"{state['now_utc'].strftime('%Y-%m-%d %H:%M:%S UTC')}")
    out.append(f"{DIM}{'─'*78}{RESET}")
    out.append(f"{BOLD}{CYAN} ENG  mode      clean_real    W/L      open  last_fire   gate{RESET}")
    for eid in ACTIVE_ENGINE_ORDER:
        e = state["per_engine"][eid]
        pnl_c = GREEN if e["realized"] > 0 else (RED if e["realized"] < 0 else "")
        gate_c = YELLOW if "PAUSED" in e["gate"] else (
            RED if ("KILL" in e["gate"] or "DEAD" in e["gate"]) else "")
        out.append(
            f" {eid:<4} {e['mode']:<8}  {pnl_c}${e['realized']:>+10.2f}{RESET}  "
            f"{e['wins']:>2}/{e['losses']:>2}    {e['open']:>4}  "
            f"{e['last_fire']:<10}  {gate_c}{e['gate']:<24}{RESET}"
        )
    out.append(f"{DIM}{'─'*78}{RESET}")
    out.append(f"{BOLD}{CYAN} ALERTS{RESET}")
    if not state["alerts"]:
        out.append(f"  {GREEN}{BOLD}✓ NO ALERTS{RESET}")
    else:
        for sev, msg in state["alerts"]:
            col = RED if sev == "red" else YELLOW
            out.append(f"  {col}⚠ {msg}{RESET}")
    out.append(f"{DIM} refreshed {state['now_utc'].strftime('%H:%M:%S')} UTC   "
               f"next in {refresh_sec}s   Ctrl+C to exit{RESET}")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────

def _curses_main(stdscr, refresh_sec: int) -> None:
    curses.curs_set(0)
    _init_colors()
    stdscr.nodelay(True)
    stdscr.timeout(1000)  # getch returns -1 after 1s

    state = gather_state()
    tick_end = time.time() + refresh_sec
    while True:
        remaining = max(0, int(tick_end - time.time()))
        render(stdscr, state, refresh_sec, remaining)
        c = stdscr.getch()
        if c == ord('q') or c == 27:  # q or ESC
            break
        if time.time() >= tick_end:
            state = gather_state()
            tick_end = time.time() + refresh_sec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-sec", type=int, default=30)
    ap.add_argument("--once", action="store_true",
                    help="dump state as JSON and exit (smoke-test / headless)")
    ap.add_argument("--plain", action="store_true",
                    help="no-curses ANSI fallback (for ssh / non-tty)")
    args = ap.parse_args()

    if args.once:
        state = gather_state()
        # JSON-serializable copy
        s = dict(state)
        s["now_utc"] = state["now_utc"].isoformat()
        s["alerts"] = [{"severity": sev, "message": msg} for sev, msg in state["alerts"]]
        print(json.dumps(s, indent=2, default=str))
        return 0

    if args.plain or not sys.stdout.isatty():
        try:
            while True:
                state = gather_state()
                sys.stdout.write("\x1b[2J\x1b[H")  # clear+home
                sys.stdout.write(render_plain(state, args.refresh_sec) + "\n")
                sys.stdout.flush()
                time.sleep(args.refresh_sec)
        except KeyboardInterrupt:
            print()
            return 0

    try:
        curses.wrapper(_curses_main, args.refresh_sec)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
