"""Shadow P&L Dashboard — live terminal scoreboard.

Run in a terminal, refreshes every N seconds (default 30). Shows:
  - Portfolio total: starting capital, realized P&L, unrealized, current balance
  - Per-engine breakdown with W/L count and current balance
  - Open positions MARKED-TO-MARKET against live Kalshi orderbooks
  - Active scanner state: T3a scanner alerts, weather logger snaps,
    HRRR backfill progress, thesis factory latest run + candidate count

Usage:
    python3 ~/Documents/shadow_dashboard.py
    python3 ~/Documents/shadow_dashboard.py --refresh-sec 10   # faster tick

Exit: Ctrl+C

Reads (never writes):
  ~/Documents/shadow_pnl/engines.json
  ~/Documents/shadow_pnl/ledger.jsonl
  ~/Documents/terminal3a_data/fed_scanner*
  ~/Documents/terminal1_logger.log
  ~/Documents/terminal1_backfill_{gfs,hrrr,ecmwf_hres,aifs}.log
  ~/Documents/thesis_candidates_latest.json  (if exists)
"""

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import requests


BASE = "https://api.elections.kalshi.com/trade-api/v2"
HOME = Path.home()
SHADOW_DIR = HOME / "Documents" / "shadow_pnl"
LEDGER_PATH = SHADOW_DIR / "ledger.jsonl"
ENGINES_PATH = SHADOW_DIR / "engines.json"

# T6 vegas-match contamination filter (mirror of terminal6_milestone_check.py +
# terminal6_dashboard.py, kept inline to avoid hard imports between dashboards).
# Scoped to engine=T6 only — other engines unaffected. See HANDOFF_2026-05-10_session5.md.
_T6_CONTAMINATION_WINDOW = timedelta(hours=12)
_T6_TICKER_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _t6_parse_ticker_dt_utc(event_ticker: str) -> Optional[datetime]:
    if not event_ticker or not event_ticker.startswith("KXMLBGAME-"):
        return None
    rest = event_ticker[len("KXMLBGAME-"):]
    if len(rest) < 11:
        return None
    try:
        yy = int(rest[0:2]); mmm = rest[2:5].upper()
        dd = int(rest[5:7]); hh = int(rest[7:9]); mm = int(rest[9:11])
    except ValueError:
        return None
    if mmm not in _T6_TICKER_MONTHS:
        return None
    try:
        local = datetime(2000 + yy, _T6_TICKER_MONTHS[mmm], dd, hh, mm)
    except ValueError:
        return None
    return local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def _is_t6_contaminated_open(open_record: dict) -> bool:
    """True if a T6 open was bound to a Vegas line whose commence_time is
    more than 12h from the ticker's encoded date (the vegas-match
    wrong-day binding bug, fixed in trader 2026-05-10)."""
    if open_record.get("engine") != "T6":
        return False
    meta = open_record.get("signal_metadata") or {}
    ticker_dt = _t6_parse_ticker_dt_utc(meta.get("event_ticker") or "")
    commence_str = meta.get("commence_time_utc") or ""
    if not ticker_dt or not commence_str:
        return False
    try:
        commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return abs(commence_dt - ticker_dt) > _T6_CONTAMINATION_WINDOW

T3A_DATA = HOME / "Documents" / "terminal3a_data"
T3A_LOG = T3A_DATA / "fed_scanner.log"
T3A_ALERTS = T3A_DATA / "fed_scanner_alerts.jsonl"

WEATHER_LOG = HOME / "Documents" / "terminal1_logger.log"
HRRR_LOG = HOME / "Documents" / "terminal1_backfill_hrrr.log"
GFS_LOG = HOME / "Documents" / "terminal1_backfill_gfs.log"
AIFS_LOG = HOME / "Documents" / "terminal1_backfill_aifs.log"
ECMWF_LOG = HOME / "Documents" / "terminal1_backfill_ecmwf_hres.log"

THESIS_DIR = HOME / "Documents"

# ANSI escape codes
ESC = "\x1b["
CLEAR = f"{ESC}2J{ESC}H"      # clear screen, home
HOME_CUR = f"{ESC}H"
BOLD = f"{ESC}1m"
DIM = f"{ESC}2m"
RESET = f"{ESC}0m"
GREEN = f"{ESC}32m"
RED = f"{ESC}31m"
YELLOW = f"{ESC}33m"
CYAN = f"{ESC}36m"
BLUE = f"{ESC}34m"

_STOP = False


def _handle_sigint(sig, frame):
    global _STOP
    _STOP = True


def _color_pnl(v: float, width: int = 10) -> str:
    s = f"{v:+.2f}"
    pad = " " * max(0, width - len(s) - 1)
    if v > 0:
        return f"{pad}{GREEN}${s}{RESET}"
    if v < 0:
        return f"{pad}{RED}${s}{RESET}"
    return f"{pad}${s}"


def _read_ledger() -> List[dict]:
    if not LEDGER_PATH.exists():
        return []
    rows = []
    try:
        with open(LEDGER_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return rows


def _read_engines() -> Dict[str, dict]:
    try:
        return json.loads(ENGINES_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_live_price(ticker: str, timeout: float = 3.0) -> Optional[Dict[str, float]]:
    """Return current {yes_bid, yes_ask, mid} for a Kalshi ticker, or None."""
    try:
        r = requests.get(
            f"{BASE}/markets/{ticker}/orderbook", timeout=timeout
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    body = r.json()
    ob = body.get("orderbook_fp") or body.get("orderbook") or {}
    yes_levels = ob.get("yes_dollars") or ob.get("yes") or []
    no_levels = ob.get("no_dollars") or ob.get("no") or []

    def best(levels):
        best_p = 0.0
        for lvl in levels:
            try:
                p = float(lvl[0])
                if p < 2:
                    pc = round(p * 100)
                else:
                    pc = round(p)
                if pc > best_p:
                    best_p = pc
            except (ValueError, IndexError, TypeError):
                continue
        return best_p

    yes_bid_c = best(yes_levels)
    no_bid_c = best(no_levels)
    yes_ask_c = 100 - no_bid_c if no_bid_c else 0
    if yes_bid_c and yes_ask_c:
        mid = (yes_bid_c + yes_ask_c) / 200.0  # dollars
    elif yes_bid_c:
        mid = yes_bid_c / 100.0
    else:
        mid = 0.0
    return {
        "yes_bid": yes_bid_c / 100.0,
        "yes_ask": yes_ask_c / 100.0 if yes_ask_c else 0.0,
        "mid": mid,
    }


def compute_portfolio_state(fetch_live: bool = True) -> dict:
    """Build the full dashboard state."""
    engines = _read_engines()
    ledger = _read_ledger()

    state: Dict[str, dict] = {}
    for eid, meta in engines.items():
        state[eid] = {
            "name": meta["name"][:26],
            "mode": meta.get("mode", "?")[:9],
            "active": meta.get("active", False),
            "bankroll_start": meta["bankroll_usd"],
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "cost_tied_up": 0.0,
            "n_open": 0,
            "n_win": 0,
            "n_loss": 0,
            "excluded_realized_pnl": 0.0,
            "excluded_n": 0,
            "positions_open": [],
        }

    # 2026-05-10: pair each close with its open so we can detect T6 vegas-match
    # contamination (the open record carries the signal_metadata we need).
    # All-closes loop kept structurally similar to the original but now we
    # remember the open BEFORE pop, then pass it to the contamination filter.
    open_map: Dict[str, dict] = {}
    closes: List[dict] = []
    close_to_open: Dict[str, dict] = {}
    for r in ledger:
        if r["type"] == "open":
            open_map[r["position_id"]] = r
        elif r["type"] == "close":
            pid = r["position_id"]
            close_to_open[pid] = open_map.get(pid, {})  # may be {} if never opened in this ledger
            closes.append(r)
            open_map.pop(pid, None)

    for pid, p in open_map.items():
        eid = p["engine"]
        if eid not in state:
            continue
        state[eid]["n_open"] += 1
        state[eid]["cost_tied_up"] += p["cost_usd"] + p.get("fee_usd", 0.0)

        live = None
        mark = p["price"]  # default: at entry price (no unrealized)
        if fetch_live and p.get("venue") == "kalshi":
            live = fetch_live_price(p["ticker"])
            if live:
                mark = live["mid"]
        if p["side"] == "YES":
            cur_value = mark * p["size"]
        else:
            cur_value = (1.0 - mark) * p["size"]
        unrealized = cur_value - p["cost_usd"] - p.get("fee_usd", 0.0)
        state[eid]["unrealized_pnl"] += unrealized
        state[eid]["positions_open"].append({
            **p,
            "mark_price": mark,
            "cur_value": cur_value,
            "unrealized": unrealized,
            "live": live,
        })

    for c in closes:
        eid = c.get("engine")
        if eid not in state:
            continue
        op = close_to_open.get(c["position_id"], {})
        # Contamination filter is T6-specific (see _is_t6_contaminated_open).
        # Closes flagged contaminated go into the "excluded" buckets so the
        # validation gate matches terminal6_milestone_check.py and the
        # in-engine terminal6_dashboard.py.
        if _is_t6_contaminated_open(op):
            state[eid]["excluded_realized_pnl"] += c["realized_pnl_usd"]
            state[eid]["excluded_n"] += 1
            continue
        state[eid]["realized_pnl"] += c["realized_pnl_usd"]
        if c["outcome"] == "win":
            state[eid]["n_win"] += 1
        elif c["outcome"] == "loss":
            state[eid]["n_loss"] += 1

    for eid, s in state.items():
        s["realized_pnl"] = round(s["realized_pnl"], 2)
        s["unrealized_pnl"] = round(s["unrealized_pnl"], 2)
        s["cost_tied_up"] = round(s["cost_tied_up"], 2)
        s["excluded_realized_pnl"] = round(s["excluded_realized_pnl"], 2)
        s["current_balance"] = round(
            s["bankroll_start"] + s["realized_pnl"]
            - s["cost_tied_up"] + s["unrealized_pnl"],
            2,
        )

    return state


# -----------------------------------------------------------------------
# Scanner state lookups (non-fatal on missing files)
# -----------------------------------------------------------------------

def _tail(path: Path, n: int = 1) -> List[str]:
    if not path.exists():
        return []
    try:
        with open(path) as f:
            lines = f.readlines()
        return [ln.rstrip() for ln in lines[-n:]]
    except OSError:
        return []


def scanner_state() -> dict:
    state = {}

    # T3a scanner
    if T3A_LOG.exists():
        tail_log = _tail(T3A_LOG, 30)
        snap_lines = [ln for ln in tail_log if "snap #" in ln]
        last = snap_lines[-1] if snap_lines else ""
        m = re.search(r"snap #(\d+).*alerts=(\d+).*in ([\d.]+)s", last)
        state["t3a"] = {
            "snap": int(m.group(1)) if m else 0,
            "alerts_last": int(m.group(2)) if m else 0,
            "snap_time_s": float(m.group(3)) if m else 0.0,
            "last_line": last[:110],
        }
    else:
        state["t3a"] = None

    # T3a alerts total
    if T3A_ALERTS.exists():
        try:
            with open(T3A_ALERTS) as f:
                state["t3a_total_alerts"] = sum(1 for _ in f)
        except OSError:
            state["t3a_total_alerts"] = 0
    else:
        state["t3a_total_alerts"] = 0

    # Weather logger
    if WEATHER_LOG.exists():
        tail_log = _tail(WEATHER_LOG, 20)
        snap_lines = [ln for ln in tail_log if "snap #" in ln]
        last = snap_lines[-1] if snap_lines else ""
        m = re.search(r"snap #(\d+).*total=(\d+)", last)
        state["weather"] = {
            "snap": int(m.group(1)) if m else 0,
            "total": int(m.group(2)) if m else 0,
            "last_line": last[:110],
        }
    else:
        state["weather"] = None

    # HRRR backfill
    for name, path in [("hrrr", HRRR_LOG), ("gfs", GFS_LOG),
                       ("aifs", AIFS_LOG), ("ecmwf_hres", ECMWF_LOG)]:
        done = False
        last_cum = 0
        if path.exists():
            try:
                with open(path) as f:
                    for line in f:
                        if "BACKFILL COMPLETE" in line:
                            done = True
                        m = re.search(r"cumulative: (\d+)", line)
                        if m:
                            last_cum = int(m.group(1))
            except OSError:
                pass
        state[name] = {"cumulative": last_cum, "complete": done}

    # Thesis factory latest
    latest_json = None
    for path in sorted(THESIS_DIR.glob("thesis_candidates_*.json"), reverse=True):
        if "latest" in path.name:
            continue
        latest_json = path
        break
    state["thesis_latest"] = {"path": None, "count": 0, "mtime": None}
    if latest_json:
        try:
            data = json.loads(latest_json.read_text())
            count = len(data.get("candidates", data)) if isinstance(data, (dict, list)) else 0
            if isinstance(data, list):
                count = len(data)
            elif isinstance(data, dict) and "candidates" in data:
                count = len(data["candidates"])
            state["thesis_latest"] = {
                "path": latest_json.name,
                "count": count,
                "mtime": datetime.fromtimestamp(
                    latest_json.stat().st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M"),
            }
        except (json.JSONDecodeError, OSError, PermissionError):
            state["thesis_latest"] = {"path": latest_json.name, "count": "?", "mtime": None}

    return state


# -----------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------

def render(state_portfolio: dict, state_scanners: dict,
           refresh_sec: int, last_fetch_dt: float) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []
    lines.append(f"{CLEAR}{BOLD}{CYAN}╔════════════════════════════════════════════════════════════════════════════════════════╗{RESET}")
    lines.append(f"{BOLD}{CYAN}║  SHADOW P&L DASHBOARD   {now}    refresh={refresh_sec}s   fetch={last_fetch_dt:.1f}s   ║{RESET}")
    lines.append(f"{BOLD}{CYAN}╠════════════════════════════════════════════════════════════════════════════════════════╣{RESET}")

    # DEPLOYED portfolio = engines with any activity (open or closed positions).
    # Everything else is reserved capital, not in play yet.
    def _has_activity(s):
        return s["n_open"] + s["n_win"] + s["n_loss"] > 0

    deployed = {k: v for k, v in state_portfolio.items() if _has_activity(v)}
    reserved = {k: v for k, v in state_portfolio.items() if not _has_activity(v)}

    d_start = sum(s["bankroll_start"] for s in deployed.values())
    d_real = sum(s["realized_pnl"] for s in deployed.values())
    d_unreal = sum(s["unrealized_pnl"] for s in deployed.values())
    d_tied = sum(s["cost_tied_up"] for s in deployed.values())
    d_current = d_start + d_real - d_tied + d_unreal
    n_open = sum(s["n_open"] for s in state_portfolio.values())
    r_start = sum(s["bankroll_start"] for s in reserved.values())

    n_deployed = len(deployed)
    lines.append(f"{BOLD}  DEPLOYED PORTFOLIO{RESET}  "
                 f"({n_deployed} engine{'s' if n_deployed != 1 else ''} trading)")
    lines.append(f"    Start  ${d_start:>10,.0f}      "
                 f"Realized  {_color_pnl(d_real, 8)}      "
                 f"Unrealized  {_color_pnl(d_unreal, 8)}")
    lines.append(f"    Current{_color_pnl(d_current - d_start, 12)}   "
                 f"→ {BOLD}${d_current:>10,.2f}{RESET}      "
                 f"Tied-up  ${d_tied:>6,.0f}      "
                 f"Open={n_open}")
    if r_start > 0:
        n_reserved = len(reserved)
        lines.append(f"    {DIM}Reserved capital (idle engines): ${r_start:,.0f} "
                     f"across {n_reserved} engine{'s' if n_reserved != 1 else ''}{RESET}")

    lines.append(f"{DIM}  ───────────────────────────────────────────────────────────────────────────────────────{RESET}")
    lines.append(f"{BOLD}  PER-ENGINE{RESET}")
    lines.append(f"    {'':<5} {'name':<26} {'mode':<9} "
                 f"{'start':>7} {'real':>9} {'unreal':>9} {'curr_bal':>10} "
                 f"{'open':>4} {'W':>2} {'L':>2}")
    for eid, s in state_portfolio.items():
        mark = "●" if s["active"] else "○"
        color = GREEN if s["current_balance"] > s["bankroll_start"] else (
            RED if s["current_balance"] < s["bankroll_start"] else ""
        )
        lines.append(
            f"    {mark} {eid:<4}{s['name']:<26} {s['mode']:<9} "
            f"${s['bankroll_start']:>5,.0f}  "
            f"{_color_pnl(s['realized_pnl'], 8)}  "
            f"{_color_pnl(s['unrealized_pnl'], 8)}  "
            f"{color}${s['current_balance']:>8,.2f}{RESET} "
            f"{s['n_open']:>4} {s['n_win']:>2} {s['n_loss']:>2}"
        )
        # Audit secondary line for any engine with contamination-excluded closes.
        # Currently only T6 (vegas-match wrong-day bug, fixed 2026-05-10).
        if s.get("excluded_n", 0) > 0:
            lines.append(
                f"    {DIM}     └─ excluded (contaminated): "
                f"{s['excluded_n']} closes  P&L=${s['excluded_realized_pnl']:+.2f}{RESET}"
            )

    # Open positions
    lines.append(f"{DIM}  ───────────────────────────────────────────────────────────────────────────────────────{RESET}")
    open_positions = []
    for eid, s in state_portfolio.items():
        for p in s["positions_open"]:
            open_positions.append((eid, p))
    lines.append(f"{BOLD}  OPEN POSITIONS ({len(open_positions)}){RESET}")
    if open_positions:
        lines.append(f"    {'eng':<4} {'ticker':<38} {'side':<4} {'size':>6} "
                     f"{'entry':>7} {'mark_side':>9} {'cost':>8} {'unreal':>9}")
        for eid, p in open_positions[:15]:
            # Show the mark on YOUR side: for YES position → YES mid; for NO → NO mid
            your_side_mark = p['mark_price'] if p['side'] == 'YES' else (1.0 - p['mark_price'])
            lines.append(
                f"    {eid:<4} {p['ticker'][:38]:<38} {p['side']:<4} "
                f"{p['size']:>6} ${p['price']:>5.2f}  ${your_side_mark:>5.2f} "
                f"   ${p['cost_usd']:>6.2f} {_color_pnl(p['unrealized'], 8)}"
            )
    else:
        lines.append(f"    {DIM}(none){RESET}")

    # Active scanners
    lines.append(f"{DIM}  ───────────────────────────────────────────────────────────────────────────────────────{RESET}")
    lines.append(f"{BOLD}  ACTIVE SCANNERS{RESET}")

    t3a = state_scanners.get("t3a")
    if t3a and t3a["snap"]:
        lines.append(f"    {BLUE}T3a Fed Scanner{RESET}:   snap #{t3a['snap']}   "
                     f"alerts_last_snap={t3a['alerts_last']}   "
                     f"total_alerts={state_scanners['t3a_total_alerts']}")
    else:
        lines.append(f"    {DIM}T3a Fed Scanner:   not running / no log yet{RESET}")

    w = state_scanners.get("weather")
    if w and w["snap"]:
        lines.append(f"    {BLUE}Weather logger{RESET}:    snap #{w['snap']}   "
                     f"markets_this_snap={w['total']}")
    else:
        lines.append(f"    {DIM}Weather logger:    no log{RESET}")

    lines.append(f"    {BLUE}Backfill{RESET}:  ")
    for m in ("gfs", "hrrr", "ecmwf_hres", "aifs"):
        meta = state_scanners.get(m, {})
        status = (f"{GREEN}DONE{RESET}" if meta.get("complete")
                  else f"{YELLOW}running{RESET}")
        lines.append(f"       {m:<12} cumulative={meta.get('cumulative', 0):>5}  {status}")

    th = state_scanners.get("thesis_latest", {})
    if th.get("path"):
        lines.append(f"    {BLUE}Thesis factory{RESET}:   latest={th['path']}  "
                     f"candidates={th['count']}  at_utc={th.get('mtime')}")
    else:
        lines.append(f"    {DIM}Thesis factory:    no runs yet{RESET}")

    lines.append(f"{BOLD}{CYAN}╚════════════════════════════════════════════════════════════════════════════════════════╝{RESET}")
    lines.append(f"{DIM}Ctrl+C to exit{RESET}")

    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-sec", type=int, default=30,
                    help="refresh interval in seconds (default 30)")
    ap.add_argument("--no-live-prices", action="store_true",
                    help="skip Kalshi orderbook fetches (use entry prices as mark)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    while not _STOP:
        t0 = time.time()
        try:
            portfolio = compute_portfolio_state(fetch_live=not args.no_live_prices)
            scanners = scanner_state()
        except Exception as e:
            portfolio = {}
            scanners = {"error": str(e)}
        dt = time.time() - t0
        sys.stdout.write(render(portfolio, scanners, args.refresh_sec, dt))
        sys.stdout.flush()

        # Sleep in 1s chunks so Ctrl+C is responsive
        end = time.time() + args.refresh_sec
        while time.time() < end and not _STOP:
            time.sleep(min(1.0, end - time.time()))

    print("\nbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
