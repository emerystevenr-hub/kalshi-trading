"""Shadow P&L Dashboard — animated full-screen rich UI (session 7 v5).

Live in-place updates via rich.live.Live at 4 Hz. Data refresh every 30s.
Visual motion: pulsing daemon dots, 1 Hz clock ticker, shimmering progress
bar on the T6 validator gate, rotating spinner during data refresh.
No scrolling, ever. Only T6 and T7 render.

Layout (top → bottom):
  HERO      portfolio P&L headline + status dots + capital meta + live clock
  ENGINES   T6 and T7 cards side-by-side with bordered panels
            T6 gate: animated gradient bar (shimmer moves through filled cells)
  ALERTS    real alerts including freshness; never suppressed
  FOOTER    refreshed | next refresh countdown (live) | q to exit

Data sources:
  shadow_pnl/engines.json + ledger.jsonl       — bankroll + raw P&L
  terminal6_milestone_check                    — clean T6 P&L + gate (truth)
  scheduler_status.json + scheduler.pid        — daemon health
  freshness_alarm.flag + macro_cap_alarm.flag  — alert flags
  entropy_alerts.jsonl                         — entropy events
  scheduler_logs/t6_mlb_lines_puller.log       — Odds API credits
  terminal{6,7}_data/ws_logger.log             — daemon-alive heartbeat
  ledger.jsonl                                 — engine "last fire" timestamp

Usage:
  python3 ~/Documents/shadow_dashboard.py
  python3 ~/Documents/shadow_dashboard.py --refresh-sec 10
  python3 ~/Documents/shadow_dashboard.py --once    # JSON dump, no rich

Install: pip install rich --break-system-packages
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
MDD_FLAG = DOCS / "mdd_alarm.flag"
ENTROPY_ALERTS = DOCS / "entropy_alerts.jsonl"
T6_LINES_LOG = DOCS / "scheduler_logs" / "t6_mlb_lines_puller.log"

# Session 7 v6: engine list is DERIVED from engines.json at every gather_state()
# call. No hardcoded list. Set `monitoring.show_in_dashboard: false` on an
# engine (or `active: false`) to drop it from the dashboard. T6 is hardcoded
# only as the engine that uses milestone_check for clean P&L (special path) —
# every other engine reads from the raw ledger.
T6_ENGINE_ID = "T6"   # only ID kept; defines which engine routes through milestone_check

DAEMON_STALE_SEC = 5 * 60
ENTROPY_RECENT_SEC = 60 * 60
ODDS_API_LOW_CREDITS = 50


# ─────────────────────────────────────────────────────────────────────
# Pure helpers
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


def _engine_ledger_stats(ledger: List[dict], engine: str) -> Tuple[float, int, int, int, Optional[float]]:
    """Return (realized, wins, losses, open_count, last_event_ts_epoch_or_None)."""
    opens: Dict[str, dict] = {}
    closed_pids: set = set()
    pnl = 0.0
    wins = losses = 0
    last_event_ts: Optional[float] = None
    for r in ledger:
        if r.get("engine") != engine:
            continue
        t = r.get("type")
        pid = r.get("position_id")
        # Track the most recent open OR close timestamp — that's "last fire"
        # at engine level (when did this engine last touch the market).
        ts_str = r.get("ts")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                if last_event_ts is None or ts > last_event_ts:
                    last_event_ts = ts
            except (TypeError, ValueError):
                pass
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
    return round(pnl, 2), wins, losses, open_count, last_event_ts


def _t6_clean_stats(ledger: List[dict]) -> Tuple[float, int, int, int, dict, Optional[float], dict]:
    """Returns (clean_total, wins, losses, open_count, gate_dict, last_event_ts, full_stats).
    full_stats is the entire compute_stats dict, which now includes profit_factor,
    sharpe_annualized, gross_profit, gross_loss, pf_sharpe_warning."""
    all_closes = load_t6_closed()
    clean = [c for c in all_closes if not _is_contaminated(c)]
    stats = compute_stats(clean)
    gate = evaluate_gate(stats)

    open_pids: Dict[str, bool] = {}
    closed_pids: set = set()
    last_event_ts: Optional[float] = None
    for r in ledger:
        if r.get("engine") != "T6":
            continue
        pid = r.get("position_id")
        if not pid:
            continue
        ts_str = r.get("ts")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                if last_event_ts is None or ts > last_event_ts:
                    last_event_ts = ts
            except (TypeError, ValueError):
                pass
        if r.get("type") == "open":
            open_pids[pid] = True
        elif r.get("type") == "close":
            closed_pids.add(pid)
    open_count = sum(1 for pid in open_pids if pid not in closed_pids)
    return (round(stats["total_pnl"], 2), stats["wins"], stats["losses"],
            open_count, gate, last_event_ts, stats)


def _total_deployed_capital(engines: dict) -> float:
    """Policy denominator — sum of all active engine bankrolls. Used by the
    macro-concentration cap calc. Includes T3a $5K placeholder when T3a is
    active=true (it anchors the macro slot even if the scanner is paused)."""
    return sum(
        float(m.get("bankroll_usd") or 0)
        for m in engines.values()
        if isinstance(m, dict)
        and m.get("active")
        and float(m.get("bankroll_usd") or 0) > 0
    )


def _dashboard_capital(engines: dict, active_order: List[str]) -> float:
    """Sum of bankrolls for engines actually shown in the dashboard.
    Excludes paused-but-active-flagged engines like T3a (placeholder). This
    is the number that drives the hero 'starting' line — it should match
    what the user sees in the engine cards (T6 + T7 only = $20K)."""
    return sum(
        float(engines.get(eid, {}).get("bankroll_usd") or 0)
        for eid in active_order
    )


def _macro_cap_pct(engines: dict) -> Optional[float]:
    macro = 0.0
    total = _total_deployed_capital(engines)
    for meta in engines.values():
        if not isinstance(meta, dict) or not meta.get("active"):
            continue
        bk = float(meta.get("bankroll_usd") or 0)
        if bk <= 0:
            continue
        if meta.get("category") == "macro":
            macro += bk
    return (100.0 * macro / total) if total > 0 else None


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


def _active_engine_order(engines_meta: dict) -> List[str]:
    """Engines visible in the dashboard — active AND monitoring.show_in_dashboard.
    Sorted alphabetically so order is deterministic regardless of dict order."""
    out = []
    for eid, meta in engines_meta.items():
        if not isinstance(meta, dict) or not meta.get("active"):
            continue
        mon = meta.get("monitoring") or {}
        if mon.get("show_in_dashboard", False):
            out.append(eid)
    return sorted(out)


def _engine_log_path(meta: dict) -> Optional[Path]:
    mon = meta.get("monitoring") or {}
    rel = mon.get("ws_logger_path")
    return (DOCS / rel) if rel else None


def _engine_title(eid: str, meta: dict) -> str:
    name = meta.get("name", eid)
    # Strip parenthetical qualifiers and truncate for headline space
    short = name.split("(")[0].strip()
    return f"{eid}  ·  {short[:30]}"


def _scheduler_health(sched: dict) -> dict:
    pid = None
    if SCHED_PID.exists():
        try:
            pid = int(SCHED_PID.read_text().strip())
        except (ValueError, OSError):
            pid = None
    if pid is None:
        return {"alive": False, "pid": None}
    return {"alive": _proc_alive(pid), "pid": pid}


def _alerts(state: dict) -> List[Tuple[str, str]]:
    """List of (severity, message) tuples. 'red' or 'yellow'.
    Freshness alarm is NEVER suppressed — it shows until the flag is cleared."""
    alerts: List[Tuple[str, str]] = []

    if MACRO_FLAG.exists():
        try:
            msg = MACRO_FLAG.read_text().strip().splitlines()[0]
        except OSError:
            msg = "macro cap breached"
        alerts.append(("red", f"MACRO CAP  {msg[:60]}"))

    if MDD_FLAG.exists():
        try:
            lines = MDD_FLAG.read_text().strip().splitlines()
            msg = lines[1] if len(lines) > 1 else lines[0]
        except OSError:
            msg = "max drawdown gate tripped — new opens blocked"
        alerts.append(("red", f"MDD GATE  {msg[:64]}"))

    if FRESHNESS_FLAG.exists():
        try:
            lines = FRESHNESS_FLAG.read_text().strip().splitlines()
            msg = lines[1] if len(lines) > 1 else lines[0]
        except OSError:
            msg = "freshness alarm raised"
        alerts.append(("red", f"FRESHNESS  {msg[:64]}"))

    sh = state["scheduler_health"]
    if sh["pid"] is not None and not sh["alive"]:
        alerts.append(("red", f"SCHEDULER DOWN  pid {sh['pid']} not running"))

    for eng in state["active_engine_order"]:
        meta = state["engines_meta"].get(eng, {})
        log = _engine_log_path(meta)
        if not log:
            continue
        mt = _file_mtime(log)
        if mt is None:
            alerts.append(("yellow", f"{eng} LOGGER  no log file at {log.name}"))
            continue
        age = time.time() - mt
        if age > DAEMON_STALE_SEC:
            alerts.append(("red", f"{eng} DAEMON DOWN  logger silent {_fmt_age(age)}"))

    credits = state.get("odds_api_credits")
    if credits is not None and credits < ODDS_API_LOW_CREDITS:
        alerts.append(("red", f"ODDS API  only {credits} credits remaining"))

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
        if rec.get("engine") not in state["active_engine_order"]:
            continue
        alerts.append(("yellow",
                       f"ENTROPY  {rec.get('engine')} {rec.get('ticker', '?')[:24]} "
                       f"z={rec.get('z_score', 0):.1f} · {_fmt_age(age)}"))
        break

    for j in state.get("scheduler_status", {}).get("jobs", []):
        ec = j.get("last_exit_code")
        if ec is not None and ec != 0:
            alerts.append(("yellow", f"SCHEDULER JOB  {j['name']} exit={ec}"))

    return alerts[:10]


def gather_state() -> dict:
    engines_meta = _read_json(ENGINES_JSON)
    ledger = _read_jsonl(LEDGER)
    sched = _read_json(SCHED_STATUS)
    credits = _odds_api_credits()
    cap_pct = _macro_cap_pct(engines_meta)
    active_order = _active_engine_order(engines_meta)
    total_capital = _dashboard_capital(engines_meta, active_order)
    policy_capital = _total_deployed_capital(engines_meta)

    per_engine: Dict[str, dict] = {}
    portfolio_realized = 0.0
    for eid in active_order:
        meta = engines_meta.get(eid, {})
        extra_stats: dict = {}
        if eid == T6_ENGINE_ID:
            pnl, wins, losses, open_count, gate, last_ts, extra_stats = _t6_clean_stats(ledger)
            gate_label = gate["gate"]
            gate_n, gate_target = wins + losses, VALIDATE_N
            gate_severity = (
                "red"    if gate_label in ("EARLY_KILL", "DEAD") else
                "green"  if gate_label == "VALIDATED" else
                "yellow" if gate_label == "INCONCLUSIVE" else
                "normal"
            )
        else:
            pnl, wins, losses, open_count, last_ts = _engine_ledger_stats(ledger, eid)
            gate_label = "ACTIVE" if meta.get("active") else "IDLE"
            gate_n = gate_target = None
            gate_severity = "normal"

        log = _engine_log_path(meta)
        log_mt = _file_mtime(log) if log else None
        log_age = (time.time() - log_mt) if log_mt is not None else None
        daemon_alive = (log_age is not None and log_age <= DAEMON_STALE_SEC)

        last_fire_age = (time.time() - last_ts) if last_ts else None
        per_engine[eid] = {
            "title":         _engine_title(eid, meta),
            "mode":          meta.get("mode") or ("active" if meta.get("active") else "idle"),
            "bankroll":      float(meta.get("bankroll_usd") or 0),
            "realized":      pnl,
            "wins":          wins,
            "losses":        losses,
            "open":          open_count,
            "last_fire":     _fmt_age(last_fire_age) if last_fire_age is not None else "none",
            "last_fire_ts":  last_ts,
            "daemon_alive":  daemon_alive,
            "gate":          gate_label,
            "gate_n":        gate_n,
            "gate_target":   gate_target,
            "gate_severity": gate_severity,
            # Only populated for T6 today (full milestone stats); other engines
            # show as None. Surfaces in the daily status email.
            "profit_factor":     extra_stats.get("profit_factor"),
            "sharpe_annualized": extra_stats.get("sharpe_annualized"),
            "gross_profit":      extra_stats.get("gross_profit"),
            "gross_loss":        extra_stats.get("gross_loss"),
            "pf_sharpe_warning": extra_stats.get("pf_sharpe_warning"),
        }
        portfolio_realized += pnl

    state = {
        "now_utc":             datetime.now(timezone.utc),
        "portfolio_realized":  round(portfolio_realized, 2),
        "total_capital":       total_capital,
        "policy_capital":      policy_capital,
        "macro_cap_pct":       cap_pct,
        "odds_api_credits":    credits,
        "engines_meta":        engines_meta,
        "active_engine_order": active_order,
        "scheduler_status":    sched,
        "scheduler_health":    _scheduler_health(sched),
        "per_engine":          per_engine,
    }
    state["alerts"] = _alerts(state)
    return state


# ─────────────────────────────────────────────────────────────────────
# Rich + motion
# ─────────────────────────────────────────────────────────────────────

def _import_rich():
    from rich import box
    from rich.align import Align
    from rich.columns import Columns
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.text import Text
    return {
        "box": box, "Align": Align, "Columns": Columns,
        "Console": Console, "Group": Group, "Layout": Layout,
        "Live": Live, "Padding": Padding, "Panel": Panel, "Text": Text,
    }


# Palette
PNL_POS = "bright_green"
PNL_NEG = "bright_red"
ACCENT  = "cyan"
WARN    = "yellow"
DANGER  = "bright_red"
OK      = "bright_green"
LABEL   = "grey50"
HEADING = "bold bright_white"
DIM     = "grey39"

# Spinner glyphs — Braille spinner cycling
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _pnl_color(v: float) -> str:
    return PNL_POS if v > 0 else (PNL_NEG if v < 0 else "white")


def _pnl_arrow(v: float) -> str:
    return "▲" if v > 0 else ("▼" if v < 0 else "·")


def _pulse_dot(alive: bool, warn: bool, phase: int) -> Tuple[str, str]:
    """Status dot that pulses between full/dim every second."""
    if not alive:
        return ("●", f"bold {DANGER}")
    style_color = WARN if warn else OK
    # Even phase = bright, odd phase = dimmer (creates a soft pulse)
    if phase % 2 == 0:
        return ("●", f"bold {style_color}")
    return ("●", style_color)


def _shimmer_bar(n: int, target: int, phase: int, width: int = 28) -> Tuple[str, str, int]:
    """Progress bar with a moving shimmer cell. Returns (chars, base_style, shimmer_pos).
    The shimmer position is also returned so we can render it in a brighter style."""
    if target <= 0:
        return ("", LABEL, -1)
    pct = max(0.0, min(1.0, n / target))
    filled = int(round(width * pct))
    bar = ["█"] * filled + ["░"] * (width - filled)
    # Shimmer travels through filled cells. If nothing filled, no shimmer.
    shimmer_pos = (phase % filled) if filled > 0 else -1
    if pct >= 0.95:
        style = "bold bright_green"
    elif pct >= 0.50:
        style = "bold cyan"
    elif pct >= 0.10:
        style = "cyan"
    else:
        style = "blue"
    return ("".join(bar), style, shimmer_pos)


# ─────────────────── HERO PANEL ───────────────────

def _build_hero(state, phase, R):
    box = R["box"]; Align = R["Align"]; Padding = R["Padding"]
    Panel = R["Panel"]; Text = R["Text"]; Group = R["Group"]; Columns = R["Columns"]

    realized = state["portfolio_realized"]
    total_cap = state["total_capital"]
    pct_of_cap = (100.0 * realized / total_cap) if total_cap else 0.0

    # Live HH:MM:SS clock (uses time.time(), ticks at 1Hz visually)
    live_clock = time.strftime("%H:%M:%S", time.gmtime())
    date_part = state["now_utc"].strftime("%Y-%m-%d")
    spin = SPINNER[phase % len(SPINNER)]

    headline = Text(no_wrap=True, justify="center")
    headline.append(f"  ${realized:+,.2f}  ",
                    style=f"bold {_pnl_color(realized)} on grey11")
    headline.append("   ", style="")
    headline.append(_pnl_arrow(realized), style=_pnl_color(realized))
    headline.append(f" {pct_of_cap:+.2f}%", style=_pnl_color(realized))

    sub = Text(justify="center", style=LABEL)
    sub.append("PORTFOLIO REALIZED  ·  starting ", style=LABEL)
    sub.append(f"${total_cap:,.0f}", style=HEADING)
    sub.append("  →  current ", style=LABEL)
    sub.append(f"${total_cap + realized:,.2f}", style=HEADING)

    clock = Text(justify="center", no_wrap=True)
    clock.append(f"{spin} ", style=f"bold {ACCENT}")
    clock.append(date_part, style=LABEL)
    clock.append("  ", style="")
    clock.append(live_clock, style=f"bold {ACCENT}")
    clock.append(" UTC", style=LABEL)

    left = Group(Align.center(headline), Text(""), Align.center(sub),
                 Text(""), Align.center(clock))

    # Right side: status + capital
    sh = state["scheduler_health"]
    sched_glyph, sched_style = _pulse_dot(sh["alive"], False, phase)
    # Status dots for every active engine — built dynamically.
    engine_dots = []
    for eid in state["active_engine_order"]:
        e = state["per_engine"][eid]
        glyph, glyph_style = _pulse_dot(e["daemon_alive"], False, phase)
        engine_dots.append((eid, glyph, glyph_style, e["daemon_alive"]))

    cap = state["macro_cap_pct"]
    if cap is None:
        cap_style, cap_str = LABEL, "?"
    elif cap >= 50.0:
        cap_style, cap_str = f"bold {DANGER}", f"{cap:.1f}%"
    elif cap >= 45.0:
        cap_style, cap_str = f"bold {WARN}",   f"{cap:.1f}%"
    else:
        cap_style, cap_str = f"bold {OK}",     f"{cap:.1f}%"

    credits = state["odds_api_credits"]
    if credits is None:
        cr_style, cr_str = LABEL, "?"
    elif credits < ODDS_API_LOW_CREDITS:
        cr_style, cr_str = f"bold {DANGER}", f"{credits:,}"
    else:
        cr_style, cr_str = f"bold {OK}",     f"{credits:,}"

    status = Text(no_wrap=True)
    status.append("  STATUS\n", style=LABEL)
    status.append(f"  {sched_glyph} ", style=sched_style)
    status.append("scheduler\n", style="white" if sh["alive"] else DANGER)
    for i, (eid, glyph, glyph_style, alive) in enumerate(engine_dots):
        status.append(f"  {glyph} ", style=glyph_style)
        status.append(f"{eid} ws_logger", style="white" if alive else DANGER)
        if i < len(engine_dots) - 1:
            status.append("\n")

    capital = Text(no_wrap=True)
    capital.append("  CAPITAL\n", style=LABEL)
    capital.append("  macro     ", style=LABEL)
    capital.append(cap_str + "\n", style=cap_style)
    capital.append("  odds api  ", style=LABEL)
    capital.append(cr_str + "\n", style=cr_style)
    capital.append("  deployed  ", style=LABEL)
    capital.append(f"${total_cap:,.0f}", style=HEADING)

    right = Columns([status, capital], padding=(0, 4), expand=False)

    inner = Columns([left, right], padding=(0, 4), expand=True)
    return Panel(
        Padding(inner, (1, 2)),
        title=f"[{ACCENT}]◆ PORTFOLIO ◆[/]",
        title_align="left",
        border_style=ACCENT,
        box=box.ROUNDED,
    )


# ─────────────────── ENGINE CARD ───────────────────

def _build_engine_card(eid: str, state, phase, R):
    """Both cards use a fixed-row Table so T6 and T7 are guaranteed
    identical layout regardless of content. The gate-bar row is the
    differentiator: T6 shows a shimmering progress bar, T7 shows a
    'awaiting first close / N closes settled' subline — same row count."""
    box = R["box"]; Padding = R["Padding"]
    Panel = R["Panel"]; Text = R["Text"]
    from rich.table import Table

    e = state["per_engine"][eid]
    realized = e["realized"]
    pnl_color = _pnl_color(realized)

    t = Table(
        show_header=False, show_edge=False, box=None,
        expand=True, padding=(0, 1), pad_edge=False,
    )
    t.add_column("label", style=LABEL, justify="left", width=10, no_wrap=True)
    t.add_column("value", justify="left", no_wrap=True, overflow="ellipsis")

    # Row 1 — Realized (big)
    realized_cell = Text(no_wrap=True)
    realized_cell.append(f"${realized:+,.2f}", style=f"bold {pnl_color}")
    realized_cell.append(f"  {_pnl_arrow(realized)}", style=pnl_color)
    t.add_row("REALIZED", realized_cell)

    # Row 2 — Wins/Losses
    wl_cell = Text(no_wrap=True)
    wl_cell.append(f"{e['wins']}", style=f"bold {OK}")
    wl_cell.append(" W  ·  ", style=LABEL)
    wl_cell.append(f"{e['losses']}", style=f"bold {DANGER}")
    wl_cell.append(" L", style=LABEL)
    t.add_row("W / L", wl_cell)

    # Row 3 — Mode
    t.add_row("MODE", Text(e["mode"].upper(), style=f"bold {ACCENT}", no_wrap=True))

    # Row 4 — Bankroll
    t.add_row("BANKROLL", Text(f"${e['bankroll']:,.0f}", style=HEADING, no_wrap=True))

    # Row 5 — Open positions
    open_style = f"bold {WARN}" if e["open"] > 0 else "white"
    t.add_row("OPEN", Text(str(e["open"]), style=open_style, no_wrap=True))

    # Row 6 — Last fire (from ledger)
    t.add_row("LAST FIRE", Text(e["last_fire"], style="white", no_wrap=True))

    # Row 7 — Daemon health (with pulsing dot)
    glyph, glyph_style = _pulse_dot(e["daemon_alive"], False, phase)
    daemon_cell = Text(no_wrap=True)
    daemon_cell.append(f"{glyph} ", style=glyph_style)
    daemon_cell.append("alive" if e["daemon_alive"] else "DOWN",
                       style="white" if e["daemon_alive"] else f"bold {DANGER}")
    t.add_row("DAEMON", daemon_cell)

    # Row 8 — Gate label
    sev = e["gate_severity"]
    gate_color = (
        DANGER if sev == "red" else
        OK     if sev == "green" else
        WARN   if sev == "yellow" else
        (OK if e["gate"] == "ACTIVE" else ACCENT)
    )
    t.add_row("GATE", Text(e["gate"], style=f"bold {gate_color}", no_wrap=True))

    # Row 9 — Progress bar (T6) or close summary (T7) — always present.
    if e["gate_n"] is not None and e["gate_target"]:
        bar, base_style, shimmer_pos = _shimmer_bar(
            e["gate_n"], e["gate_target"], phase, width=22
        )
        bar_cell = Text(no_wrap=True)
        for i, ch in enumerate(bar):
            if i == shimmer_pos:
                bar_cell.append(ch, style="bold bright_white")
            else:
                bar_cell.append(ch, style=base_style)
        pct = 100.0 * e["gate_n"] / e["gate_target"]
        bar_cell.append(f"  {e['gate_n']}/{e['gate_target']}", style=HEADING)
        bar_cell.append(f"  ({pct:.1f}%)", style=LABEL)
        t.add_row("", bar_cell)
    else:
        n_closes = e["wins"] + e["losses"]
        if n_closes == 0:
            sub = Text("awaiting first close", style=LABEL, no_wrap=True)
        else:
            sub = Text(f"{n_closes} close{'s' if n_closes != 1 else ''} settled",
                       style="white", no_wrap=True)
        t.add_row("", sub)

    return Panel(
        Padding(t, (1, 1)),
        title=f"[{ACCENT}]{e['title']}[/]",
        title_align="left",
        border_style=ACCENT,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _build_engine_row(state, phase, R):
    Columns = R["Columns"]
    cards = [_build_engine_card(eid, state, phase, R)
             for eid in state["active_engine_order"]]
    return Columns(cards, padding=(0, 1), expand=True, equal=True)


# ─────────────────── ALERTS PANEL ───────────────────

def _build_alerts_panel(state, phase, R):
    box = R["box"]; Panel = R["Panel"]; Text = R["Text"]; Padding = R["Padding"]
    alerts = state["alerts"]
    if not alerts:
        body = Text(no_wrap=True)
        body.append("  ✓ ", style=f"bold {OK}")
        body.append("All systems nominal", style=f"bold {OK}")
        return Panel(
            Padding(body, (0, 1)),
            title=f"[{ACCENT}]Alerts[/]",
            title_align="left",
            border_style=OK,
            box=box.ROUNDED,
        )
    body = Text(no_wrap=False)
    for i, (sev, msg) in enumerate(alerts):
        style = f"bold {DANGER}" if sev == "red" else f"bold {WARN}"
        # Pulse the warning icon every second
        icon = "⚠" if phase % 2 == 0 else " "
        body.append(f"  {icon}  ", style=style)
        body.append(msg, style=style)
        if i < len(alerts) - 1:
            body.append("\n")
    border = DANGER if any(s == "red" for s, _ in alerts) else WARN
    return Panel(
        Padding(body, (0, 1)),
        title=f"[{ACCENT}]Alerts ({len(alerts)})[/]",
        title_align="left",
        border_style=border,
        box=box.ROUNDED,
    )


# ─────────────────── FOOTER ───────────────────

def _build_footer(state, refresh_sec, next_in_sec, phase, R):
    Text = R["Text"]; Align = R["Align"]
    spin = SPINNER[phase % len(SPINNER)]
    now_s = state["now_utc"].strftime("%Y-%m-%d %H:%M:%S UTC")
    body = Text(no_wrap=True, justify="center")
    body.append(f"{spin}  ", style=f"bold {ACCENT}")
    body.append("data refreshed ", style=DIM)
    body.append(now_s, style="bold white")
    body.append("   ·   ", style=DIM)
    body.append("next in ", style=DIM)
    body.append(f"{next_in_sec:>2}s", style=f"bold {ACCENT}")
    body.append("   ·   ", style=DIM)
    body.append("q", style=f"bold {ACCENT}")
    body.append(" to exit", style=DIM)
    return Align.center(body)


# ─────────────────── LAYOUT ───────────────────

def build_layout(state, refresh_sec, next_in_sec, phase):
    R = _import_rich()
    layout = R["Layout"]()
    layout.split_column(
        R["Layout"](_build_hero(state, phase, R),                  name="hero",    size=10),
        R["Layout"](_build_engine_row(state, phase, R),            name="engines", size=15),
        R["Layout"](_build_alerts_panel(state, phase, R),          name="alerts",  size=6),
        R["Layout"](_build_footer(state, refresh_sec, next_in_sec, phase, R),
                    name="footer", size=1),
    )
    return layout


# ─────────────────────────────────────────────────────────────────────
# Main
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
        R = _import_rich()
    except ImportError:
        print("ERROR: rich library not installed.")
        print("Install: pip install rich --break-system-packages")
        return 1

    console = R["Console"]()
    state = gather_state()
    tick_end = time.time() + args.refresh_sec

    # 4 Hz UI repaint, 1 Hz phase advance (so pulses/spinner tick at 1s)
    REPAINT_HZ = 4
    REPAINT_SEC = 1.0 / REPAINT_HZ
    phase = 0
    last_phase_tick = time.time()

    try:
        with R["Live"](
            build_layout(state, args.refresh_sec, args.refresh_sec, phase),
            console=console,
            screen=True,
            refresh_per_second=REPAINT_HZ,
            auto_refresh=False,
        ) as live:
            while True:
                now = time.time()
                next_in = max(0, int(tick_end - now))
                # Advance phase once per second — gives pulses/spinner their cadence
                if now - last_phase_tick >= 1.0:
                    phase += 1
                    last_phase_tick = now
                live.update(
                    build_layout(state, args.refresh_sec, next_in, phase),
                    refresh=True,
                )
                # Heavy data re-gather every refresh_sec
                if now >= tick_end:
                    state = gather_state()
                    tick_end = time.time() + args.refresh_sec
                time.sleep(REPAINT_SEC)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
