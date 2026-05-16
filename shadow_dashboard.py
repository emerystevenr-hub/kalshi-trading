"""Shadow P&L Dashboard — full-screen rich terminal UI (session 7 v3).

In-place refresh every 30s via rich.live.Live. No scrolling, ever.
Only T6 and T7 render. T3a/T3b/T3c/T1/T2/T4/T5 never appear.

Layout (top → bottom):
  HEADER    portfolio realized | macro cap % | Odds API credits | UTC timestamp
  ENGINES   T6 + T7 — Engine | Mode | Clean Realized | W/L | Open | Last Fire | Gate
            T6 Gate is a progress bar: n/300 visual + numeric
  ALERTS    only shown if alerts exist; otherwise single green "✓ All systems nominal"
  FOOTER    refreshed HH:MM:SS UTC | next in Xs

Data sources:
  shadow_pnl/engines.json + ledger.jsonl       — bankroll + raw P&L
  terminal6_milestone_check                    — clean T6 P&L + gate verdict (source of truth)
  scheduler_status.json + scheduler.pid        — daemon health
  freshness_alarm.flag + macro_cap_alarm.flag  — alert flags
  entropy_alerts.jsonl                         — entropy events
  scheduler_logs/t6_mlb_lines_puller.log       — Odds API credits remaining
  terminal{6,7}_data/ws_logger.log             — engine "last fire" mtime

Usage:
  python3 ~/Documents/shadow_dashboard.py
  python3 ~/Documents/shadow_dashboard.py --refresh-sec 10
  python3 ~/Documents/shadow_dashboard.py --once    # JSON dump for smoke test

Install once: pip install rich --break-system-packages
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from terminal6_milestone_check import (
    load_t6_closed,
    _is_contaminated,
    compute_stats,
    evaluate_gate,
    VALIDATE_N,
)

DOCS = Path.home() / "Documents"
SHADOW_DIR = DOCS / "shadow_pnl"
LEDGER = SHADOW_DIR / "ledger.jsonl"
ENGINES_JSON = SHADOW_DIR / "engines.json"
SCHED_STATUS = DOCS / "scheduler_status.json"
SCHED_PID = DOCS / "scheduler.pid"
FRESHNESS_FLAG = DOCS / "freshness_alarm.flag"
MACRO_FLAG = DOCS / "macro_cap_alarm.flag"
ENTROPY_ALERTS = DOCS / "entropy_alerts.jsonl"
T6_LINES_LOG = DOCS / "scheduler_logs" / "t6_mlb_lines_puller.log"

# Engines that render — and ONLY these. T3a/T3b/T3c/T1/T2/T4/T5 never appear.
ACTIVE_ENGINE_ORDER = ["T6", "T7"]

# Per-engine logger paths verified 2026-05-16 session 7 v3 against ls output:
#   T6 writes ws_logger.log every 5s (WS feed) → 5-min staleness = daemon down
#   T7 writes ws_logger.log too                → same threshold
ENGINE_LOGS = {
    "T6": DOCS / "terminal6_data" / "ws_logger.log",
    "T7": DOCS / "terminal7_data" / "ws_logger.log",
}

DAEMON_STALE_SEC = 5 * 60    # WS logger silent > 5 min = daemon down
ENTROPY_RECENT_SEC = 60 * 60
ODDS_API_LOW_CREDITS = 50


# ─────────────────────────────────────────────────────────────────────
# Pure helpers (no I/O side effects, no rich dependencies)
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
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _engine_ledger_stats(ledger: List[dict], engine: str) -> Tuple[float, int, int, int]:
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
    """Clean T6 numbers + gate — sourced from terminal6_milestone_check.
    Returns (clean_total_pnl, wins, losses, open_count, gate_dict)."""
    all_closes = load_t6_closed()
    clean = [c for c in all_closes if not _is_contaminated(c)]
    stats = compute_stats(clean)
    gate = evaluate_gate(stats)
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


def _macro_cap_pct(engines: dict) -> Optional[float]:
    macro = 0.0
    total = 0.0
    for meta in engines.values():
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


_REMAIN_RE = re.compile(r"remaining=(\d+)")


def _odds_api_credits() -> Optional[int]:
    if not T6_LINES_LOG.exists():
        return None
    try:
        with open(T6_LINES_LOG) as f:
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


def _scheduler_last_run(sched: dict, job: str) -> Optional[float]:
    for j in sched.get("jobs", []):
        if j.get("name") == job:
            return j.get("last_run_ts")
    return None


def _last_fire_age(engine: str, sched: dict) -> Optional[float]:
    log = ENGINE_LOGS.get(engine)
    if log:
        mt = _file_mtime(log)
        if mt is not None:
            return time.time() - mt
    if engine == "T6":
        ts = _scheduler_last_run(sched, "t6_mlb_lines_puller")
        if ts:
            return time.time() - ts
    return None


def _build_alerts(state: dict) -> List[Tuple[str, str]]:
    """Return [(severity, message), ...]. severity in {'red','yellow'}."""
    alerts: List[Tuple[str, str]] = []

    if MACRO_FLAG.exists():
        try:
            msg = MACRO_FLAG.read_text().strip().splitlines()[0]
        except OSError:
            msg = "macro cap breached"
        alerts.append(("red", f"MACRO CAP: {msg[:64]}"))

    if FRESHNESS_FLAG.exists():
        try:
            msg = FRESHNESS_FLAG.read_text().strip().splitlines()[0]
        except OSError:
            msg = "freshness alarm raised"
        alerts.append(("red", f"FRESHNESS: {msg[:64]}"))

    if SCHED_PID.exists():
        try:
            pid = int(SCHED_PID.read_text().strip())
            if not _proc_alive(pid):
                alerts.append(("red", f"SCHEDULER DOWN: pid {pid} not running"))
        except (ValueError, OSError):
            pass

    for eng in ACTIVE_ENGINE_ORDER:
        log = ENGINE_LOGS.get(eng)
        if not log:
            continue
        mt = _file_mtime(log)
        if mt is None:
            if state["engines_meta"].get(eng, {}).get("active"):
                alerts.append(("yellow", f"{eng} LOGGER: no log file at {log.name}"))
            continue
        age = time.time() - mt
        if age > DAEMON_STALE_SEC:
            alerts.append(("red", f"{eng} DAEMON DOWN: logger silent {_fmt_age(age)}"))

    credits = state.get("odds_api_credits")
    if credits is not None and credits < ODDS_API_LOW_CREDITS:
        alerts.append(("red", f"ODDS API: only {credits} credits remaining"))

    for rec in reversed(_read_jsonl(ENTROPY_ALERTS)[-200:]):
        if rec.get("alert_level") in (None, "noise"):
            continue
        try:
            ts = datetime.fromisoformat((rec.get("ts") or "").replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except (TypeError, ValueError):
            continue
        if age > ENTROPY_RECENT_SEC:
            break
        if rec.get("engine") not in ACTIVE_ENGINE_ORDER:
            continue   # entropy on an archived engine is noise — suppress
        alerts.append(("yellow",
                       f"ENTROPY: {rec.get('engine')} {rec.get('ticker', '?')[:30]} "
                       f"z={rec.get('z_score', 0):.1f} {_fmt_age(age)}"))
        break

    for j in state.get("scheduler_status", {}).get("jobs", []):
        ec = j.get("last_exit_code")
        if ec is not None and ec != 0:
            alerts.append(("yellow", f"SCHEDULER JOB: {j['name']} last exit={ec}"))

    return alerts[:10]


def gather_state() -> dict:
    engines_meta = _read_json(ENGINES_JSON)
    ledger = _read_jsonl(LEDGER)
    sched = _read_json(SCHED_STATUS)
    credits = _odds_api_credits()
    cap_pct = _macro_cap_pct(engines_meta)

    per_engine: Dict[str, dict] = {}
    portfolio_realized = 0.0
    for eid in ACTIVE_ENGINE_ORDER:
        meta = engines_meta.get(eid, {})
        if eid == "T6":
            pnl, wins, losses, open_count, gate = _t6_clean_stats()
            n = wins + losses
            gate_str = f"{gate['gate']} n={n}/{VALIDATE_N}"
            gate_n, gate_target = n, VALIDATE_N
            gate_severity = (
                "red" if gate["gate"] in ("EARLY_KILL", "DEAD") else
                "green" if gate["gate"] == "VALIDATED" else
                "yellow" if gate["gate"] == "INCONCLUSIVE" else
                "normal"
            )
        else:
            pnl, wins, losses, open_count = _engine_ledger_stats(ledger, eid)
            gate_str = "active" if meta.get("active") else "idle"
            gate_n = gate_target = None
            gate_severity = "normal"

        age_sec = _last_fire_age(eid, sched)
        per_engine[eid] = {
            "mode": meta.get("mode") or ("active" if meta.get("active") else "idle"),
            "realized": pnl,
            "wins": wins,
            "losses": losses,
            "open": open_count,
            "last_fire": _fmt_age(age_sec) if age_sec is not None else "—",
            "gate": gate_str,
            "gate_n": gate_n,
            "gate_target": gate_target,
            "gate_severity": gate_severity,
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
# Rich renderer
# ─────────────────────────────────────────────────────────────────────

def _import_rich():
    """Lazy import so --once smoke test runs without rich installed."""
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.align import Align
    return Console, Layout, Live, Panel, Table, Text, Align


def _pnl_text(value: float, width: int = 11):
    from rich.text import Text
    color = "bright_green" if value > 0 else ("bright_red" if value < 0 else "white")
    return Text(f"${value:>+{width-1}.2f}", style=f"bold {color}")


def _macro_text(pct: Optional[float]):
    from rich.text import Text
    if pct is None:
        return Text("?", style="dim")
    if pct >= 50.0:
        return Text(f"{pct:.1f}%", style="bold bright_red")
    if pct >= 45.0:
        return Text(f"{pct:.1f}%", style="bold yellow")
    return Text(f"{pct:.1f}%", style="bold bright_green")


def _odds_text(credits: Optional[int]):
    from rich.text import Text
    if credits is None:
        return Text("?", style="dim")
    if credits < ODDS_API_LOW_CREDITS:
        return Text(f"{credits:,}", style="bold bright_red")
    return Text(f"{credits:,}", style="bright_green")


def _gate_progress_bar(n: int, target: int, width: int = 20) -> str:
    """ASCII progress bar for T6 validator gate."""
    if target <= 0:
        return ""
    filled = max(0, min(width, int(round(width * n / target))))
    return "▰" * filled + "▱" * (width - filled)


def _build_header_panel(state, Panel, Text, Align):
    now_s = state["now_utc"].strftime("%Y-%m-%d %H:%M:%S UTC")
    realized = state["portfolio_realized"]
    body = Text(no_wrap=True, overflow="ellipsis")
    body.append(" PORTFOLIO  ", style="bold cyan")
    body.append("realized=", style="dim")
    body.append(_pnl_text(realized, 10))
    body.append("   macro=", style="dim")
    body.append(_macro_text(state["macro_cap_pct"]))
    body.append("   odds=", style="dim")
    body.append(_odds_text(state["odds_api_credits"]))
    body.append(f"   {now_s}", style="dim")
    return Panel(Align.left(body), border_style="cyan", padding=(0, 1))


def _build_engine_table(state, Panel, Table, Text):
    table = Table(
        show_header=True,
        header_style="bold cyan",
        expand=True,
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column("Engine", style="bold", no_wrap=True, width=8)
    table.add_column("Mode", no_wrap=True, width=9)
    table.add_column("Clean Realized", justify="right", no_wrap=True, width=15)
    table.add_column("W/L", justify="right", no_wrap=True, width=8)
    table.add_column("Open", justify="right", no_wrap=True, width=5)
    table.add_column("Last Fire", no_wrap=True, width=11)
    table.add_column("Gate Status", no_wrap=False)

    for eid in ACTIVE_ENGINE_ORDER:
        e = state["per_engine"][eid]
        # Gate cell — progress bar for T6, plain text for others.
        if e["gate_n"] is not None and e["gate_target"]:
            bar = _gate_progress_bar(e["gate_n"], e["gate_target"])
            gate_style = {
                "red": "bold bright_red",
                "green": "bold bright_green",
                "yellow": "yellow",
                "normal": "white",
            }[e["gate_severity"]]
            gate_cell = Text()
            gate_cell.append(f"{e['gate'].split(' n=')[0]} ", style=gate_style)
            gate_cell.append(f"{bar} ", style="cyan")
            gate_cell.append(f"{e['gate_n']}/{e['gate_target']}", style="bold")
        else:
            gate_cell = Text(e["gate"],
                             style=("green" if e["mode"] == "shadow" else "dim"))

        wl_text = Text()
        wl_text.append(f"{e['wins']}", style="bright_green")
        wl_text.append("/", style="dim")
        wl_text.append(f"{e['losses']}", style="bright_red")

        table.add_row(
            Text(eid, style="bold bright_white"),
            Text(e["mode"]),
            _pnl_text(e["realized"], 13),
            wl_text,
            Text(str(e["open"]), style="bold yellow" if e["open"] > 0 else "dim"),
            Text(e["last_fire"], style="dim"),
            gate_cell,
        )
    return Panel(table, title="[bold cyan]Engines[/]", border_style="cyan", padding=(0, 1))


def _build_alerts_panel(state, Panel, Text):
    alerts = state["alerts"]
    if not alerts:
        body = Text("✓ All systems nominal", style="bold bright_green")
        return Panel(body, title="[bold cyan]Alerts[/]", border_style="bright_green",
                     padding=(0, 1))
    body = Text()
    for i, (sev, msg) in enumerate(alerts):
        style = "bold bright_red" if sev == "red" else "bold yellow"
        body.append("⚠ ", style=style)
        body.append(msg, style=style)
        if i < len(alerts) - 1:
            body.append("\n")
    border = "bright_red" if any(s == "red" for s, _ in alerts) else "yellow"
    return Panel(body, title=f"[bold cyan]Alerts ({len(alerts)})[/]",
                 border_style=border, padding=(0, 1))


def _build_footer(state, refresh_sec, next_in_sec, Panel, Text):
    now_s = state["now_utc"].strftime("%H:%M:%S UTC")
    body = Text(no_wrap=True)
    body.append(" refreshed ", style="dim")
    body.append(now_s, style="bold")
    body.append("   next refresh in ", style="dim")
    body.append(f"{next_in_sec:>2}s", style="bold cyan")
    body.append("   ", style="dim")
    body.append("q or Ctrl+C to exit", style="dim italic")
    return Panel(body, border_style="dim", padding=(0, 1))


def build_layout(state, refresh_sec, next_in_sec):
    Console, Layout, Live, Panel, Table, Text, Align = _import_rich()
    layout = Layout()
    layout.split_column(
        Layout(_build_header_panel(state, Panel, Text, Align), name="header", size=3),
        Layout(_build_engine_table(state, Panel, Table, Text), name="engines", size=7),
        Layout(_build_alerts_panel(state, Panel, Text), name="alerts"),
        Layout(_build_footer(state, refresh_sec, next_in_sec, Panel, Text),
               name="footer", size=3),
    )
    return layout


# ─────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-sec", type=int, default=30)
    ap.add_argument("--once", action="store_true",
                    help="dump state as JSON and exit (smoke test, no rich)")
    args = ap.parse_args()

    if args.once:
        state = gather_state()
        s = dict(state)
        s["now_utc"] = state["now_utc"].isoformat()
        s["alerts"] = [{"severity": sev, "message": msg} for sev, msg in state["alerts"]]
        print(json.dumps(s, indent=2, default=str))
        return 0

    try:
        Console, Layout, Live, Panel, Table, Text, Align = _import_rich()
    except ImportError:
        print("ERROR: rich library not installed.")
        print("Install: pip install rich --break-system-packages")
        return 1

    console = Console()
    state = gather_state()
    tick_end = time.time() + args.refresh_sec

    try:
        with Live(build_layout(state, args.refresh_sec, args.refresh_sec),
                  console=console, screen=True, refresh_per_second=2,
                  auto_refresh=False) as live:
            while True:
                next_in = max(0, int(tick_end - time.time()))
                live.update(build_layout(state, args.refresh_sec, next_in),
                            refresh=True)
                time.sleep(0.5)
                if time.time() >= tick_end:
                    state = gather_state()
                    tick_end = time.time() + args.refresh_sec
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
