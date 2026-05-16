"""Terminal 6 — Live MLB Signal Dashboard.

Real-time view of every open MLB game: Kalshi implied probabilities vs sharp
Vegas consensus, current delta, signal status, open positions. Auto-refreshes
every N seconds.

Usage:
    python3 ~/Documents/terminal6_dashboard.py
    python3 ~/Documents/terminal6_dashboard.py --refresh-sec 5

Exit: Ctrl+C

Reads (never writes):
  ~/Documents/terminal6_data/kalshi_KXMLBGAME-*.jsonl   (latest snapshots per market)
  ~/Documents/terminal6_data/vegas_lines_*.jsonl        (latest sharp lines per game)
  ~/Documents/shadow_pnl/ledger.jsonl                   (open T6 positions)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

DATA_DIR = Path.home() / "Documents" / "terminal6_data"
LEDGER = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"
FLAG = Path.home() / "Documents" / "freshness_alarm.flag"

# Mirror of contamination filter from terminal6_milestone_check.py.
# Kept inline rather than imported so dashboard has zero hard deps on the
# milestone check module — a future rename of either file won't silently
# break the dashboard. See HANDOFF_2026-05-10_session5.md §lessons 49-50.
_CONTAMINATION_WINDOW = timedelta(hours=12)
_TICKER_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_ticker_dt_utc(event_ticker: str) -> Optional[datetime]:
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
    if mmm not in _TICKER_MONTHS:
        return None
    try:
        local = datetime(2000 + yy, _TICKER_MONTHS[mmm], dd, hh, mm)
    except ValueError:
        return None
    return local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def _is_contaminated_open(open_record: dict) -> bool:
    """True if this open's matched commence_time is more than 12h from
    the ticker's encoded date — vegas-match wrong-day binding (session 5)."""
    meta = open_record.get("signal_metadata") or {}
    ticker_dt = _parse_ticker_dt_utc(meta.get("event_ticker") or "")
    commence_str = meta.get("commence_time_utc") or ""
    if not ticker_dt or not commence_str:
        return False  # missing metadata — fail-open
    try:
        commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return abs(commence_dt - ticker_dt) > _CONTAMINATION_WINDOW

DELTA_THRESHOLD = 0.03

# ANSI
ESC = "\x1b["
CLEAR = f"{ESC}2J{ESC}H"
HOME_CUR = f"{ESC}H"
BOLD = f"{ESC}1m"
DIM = f"{ESC}2m"
RESET = f"{ESC}0m"
GREEN = f"{ESC}32m"
RED = f"{ESC}31m"
YELLOW = f"{ESC}33m"
CYAN = f"{ESC}36m"

_STOP = False


def _handle_sigint(sig, frame):
    global _STOP
    _STOP = True


KALSHI_TO_NAME = {
    "ATL": "Atlanta Braves", "ARI": "Arizona Diamondbacks", "AZ": "Arizona Diamondbacks",
    "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox", "CHC": "Chicago Cubs",
    "CWS": "Chicago White Sox", "CHW": "Chicago White Sox", "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians", "COL": "Colorado Rockies", "DET": "Detroit Tigers",
    "HOU": "Houston Astros", "KC": "Kansas City Royals", "KCR": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers", "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers", "MIN": "Minnesota Twins", "NYY": "New York Yankees",
    # OAK and ATH both map to bare "Athletics" — post-2024 Oakland departure
    # the Odds API emits "Athletics" with no city. Mirrors the trader's
    # _normalize() in terminal6_mlb_paper_trader.py. Without this, dashboard
    # Vegas joins fail on every Athletics game (audit H-T6-1, fixed 2026-05-09).
    "NYM": "New York Mets", "OAK": "Athletics", "ATH": "Athletics",
    "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates", "SD": "San Diego Padres",
    "SDP": "San Diego Padres", "SF": "San Francisco Giants", "SFG": "San Francisco Giants",
    "SEA": "Seattle Mariners", "STL": "St. Louis Cardinals", "TB": "Tampa Bay Rays",
    "TBR": "Tampa Bay Rays", "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals", "WAS": "Washington Nationals",
}
NAME_TO_ABBR = {v: k for k, v in KALSHI_TO_NAME.items() if len(k) <= 3}


def parse_event_teams(et: str) -> Optional[Tuple[str, str]]:
    if not et.startswith("KXMLBGAME-"):
        return None
    rest = et[len("KXMLBGAME-"):]
    if len(rest) < 12:
        return None
    team_part = rest[11:]
    for split in (2, 3):
        if 2 <= len(team_part) - split <= 3:
            away = team_part[:split]
            home = team_part[split:]
            if away in KALSHI_TO_NAME and home in KALSHI_TO_NAME:
                return KALSHI_TO_NAME[away], KALSHI_TO_NAME[home]
    return None


def load_latest_kalshi() -> Dict[str, dict]:
    """Return {ticker: most-recent-snapshot}."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pattern = f"kalshi_KXMLBGAME-*_{today}.jsonl"
    latest: Dict[str, dict] = {}
    for path in DATA_DIR.glob(pattern):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = r.get("ticker")
                    if not key:
                        continue
                    prev = latest.get(key)
                    if prev is None or r.get("snap_ts_utc", "") > prev.get("snap_ts_utc", ""):
                        latest[key] = r
        except OSError:
            continue
    return latest


def load_latest_vegas() -> Dict[Tuple[str, str], dict]:
    """Return {(home_team, away_team): most-recent-vegas-row}."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = DATA_DIR / f"vegas_lines_{today}.jsonl"
    latest: Dict[Tuple[str, str], dict] = {}
    if not path.exists():
        return latest
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (r.get("home_team"), r.get("away_team"))
                prev = latest.get(key)
                if prev is None or r.get("snap_ts_utc", "") > prev.get("snap_ts_utc", ""):
                    latest[key] = r
    except OSError:
        pass
    return latest


def load_open_t6() -> List[dict]:
    if not LEDGER.exists():
        return []
    opens: Dict[str, dict] = {}
    closed = set()
    try:
        with open(LEDGER) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("engine") != "T6":
                    continue
                if r.get("type") == "open":
                    opens[r["position_id"]] = r
                elif r.get("type") == "close":
                    closed.add(r["position_id"])
    except OSError:
        return []
    return [o for pid, o in opens.items() if pid not in closed]


def sum_t6_realized() -> Tuple[float, int, int, float, int]:
    """Return (clean_total, clean_wins, clean_losses, excluded_total, excluded_n).

    2026-05-10: dashboard now mirrors terminal6_milestone_check.py — the
    primary realized P&L excludes vegas-match contaminated closes (where
    the matched commence_time is >12h from the ticker's encoded date).
    Contaminated total is preserved as the secondary "audit" line so the
    dashboard never silently drops the historical record.
    """
    if not LEDGER.exists():
        return 0.0, 0, 0, 0.0, 0
    opens: Dict[str, dict] = {}
    clean_total = 0.0
    clean_wins = clean_losses = 0
    excluded_total = 0.0
    excluded_n = 0
    try:
        with open(LEDGER) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("engine") != "T6":
                    continue
                t = r.get("type"); pid = r.get("position_id")
                if t == "open" and pid:
                    opens[pid] = r
                elif t == "close" and pid and pid in opens:
                    op = opens[pid]
                    pnl = float(r.get("realized_pnl_usd") or 0)
                    if _is_contaminated_open(op):
                        excluded_total += pnl
                        excluded_n += 1
                    else:
                        clean_total += pnl
                        if pnl > 0:
                            clean_wins += 1
                        elif pnl < 0:
                            clean_losses += 1
    except OSError:
        pass
    return clean_total, clean_wins, clean_losses, excluded_total, excluded_n


def color_pnl(v: float) -> str:
    if v > 0:
        return f"{GREEN}{v:+.2f}{RESET}"
    if v < 0:
        return f"{RED}{v:+.2f}{RESET}"
    return f"{v:+.2f}"


def color_delta(v: float) -> str:
    s = f"{v:+.3f}"
    if abs(v) >= DELTA_THRESHOLD:
        return f"{GREEN}{BOLD}{s}{RESET}"
    if abs(v) >= DELTA_THRESHOLD / 2:
        return f"{YELLOW}{s}{RESET}"
    return f"{DIM}{s}{RESET}"


def fmt_age_min(snap_ts_str: str) -> str:
    try:
        ts = datetime.fromisoformat(snap_ts_str.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    except (TypeError, ValueError, AttributeError):
        return "?"
    if age < 1:
        return f"{int(age*60)}s"
    if age < 60:
        return f"{int(age)}m"
    return f"{age/60:.1f}h"


def render() -> None:
    now = datetime.now(timezone.utc)
    kalshi = load_latest_kalshi()
    vegas = load_latest_vegas()
    open_positions = load_open_t6()
    realized, wins, losses, excluded_pnl, excluded_n = sum_t6_realized()
    n_closed = wins + losses

    # Group Kalshi markets by event
    by_event: Dict[str, List[dict]] = {}
    for m in kalshi.values():
        et = m.get("event_ticker") or ""
        by_event.setdefault(et, []).append(m)

    # Open positions index by event
    open_by_event: Dict[str, List[dict]] = {}
    for p in open_positions:
        md = p.get("signal_metadata") or {}
        et = md.get("event_ticker") or ""
        open_by_event.setdefault(et, []).append(p)

    out = []
    out.append(CLEAR)
    out.append(f"{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════════════════════════════════╗{RESET}")
    out.append(f"{BOLD}{CYAN}║  T6 MLB SIGNAL DASHBOARD     {now.strftime('%Y-%m-%d %H:%M:%S UTC')}     "
               f"games={len(by_event):2d}   vegas={len(vegas):2d}   {'flag=' + RED + 'STALE' + RESET + CYAN if FLAG.exists() else 'flag=ok':12s}  ║{RESET}")
    out.append(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════════════════════════════════════════╝{RESET}")

    # Summary row — clean view (matches terminal6_milestone_check.py default).
    # Vegas-match contaminated closes are excluded from realized; surfaced on
    # the secondary line so the dashboard never silently drops the audit trail.
    out.append(f"{BOLD}T6 P&L:{RESET}  realized={color_pnl(realized)}  closed={n_closed} ({wins}W/{losses}L)  "
               f"open={len(open_positions)}  threshold=±{DELTA_THRESHOLD:.2f}pp  "
               f"{DIM}(dry-run until clean n≥300 with edge-confirmed){RESET}")
    if excluded_n > 0:
        out.append(f"{DIM}        excluded (vegas-match contaminated, fixed 2026-05-10): "
                   f"{excluded_n} closes  P&L=${excluded_pnl:+.2f}{RESET}")
    out.append("")

    # Per-game table
    out.append(f"{BOLD}{'matchup':<32} {'kalshi p (away/home)':<22} {'vegas p (away/home)':<22} "
               f"{'Δ_away':<10} {'Δ_home':<10} {'spr':<5} {'k_age':<6} {'v_age':<6} status{RESET}")
    out.append("─" * 130)

    rows = []
    for et, markets in by_event.items():
        teams = parse_event_teams(et)
        if not teams:
            continue
        away_full, home_full = teams
        veg = vegas.get((home_full, away_full))

        # Find away market and home market in Kalshi snapshots
        away_mkt = home_mkt = None
        for m in markets:
            sub = (m.get("subtitle") or m.get("yes_sub_title") or "").lower()
            ticker = m.get("ticker") or ""
            suffix_match = re.search(r"-([A-Z]{2,3})$", ticker)
            kcode = suffix_match.group(1) if suffix_match else ""
            kname = KALSHI_TO_NAME.get(kcode, "")
            if kname == away_full or away_full.lower() in sub:
                away_mkt = m
            elif kname == home_full or home_full.lower() in sub:
                home_mkt = m

        def kalshi_p(mkt):
            if not mkt: return None
            yt = mkt.get("yes_top_price_cents")
            nt = mkt.get("no_top_price_cents")
            if yt is None or nt is None: return None
            yes_ask = 100 - nt
            return ((yt + yes_ask) / 2.0) / 100.0

        def kalshi_spread(mkt):
            if not mkt: return None
            return mkt.get("spread_cents")

        ap = kalshi_p(away_mkt)
        hp = kalshi_p(home_mkt)
        av = veg.get("consensus_away_p") if veg else None
        hv = veg.get("consensus_home_p") if veg else None
        spread = kalshi_spread(away_mkt) or kalshi_spread(home_mkt)

        delta_away = (av - ap) if (av is not None and ap is not None) else None
        delta_home = (hv - hp) if (hv is not None and hp is not None) else None

        k_age = fmt_age_min(away_mkt.get("snap_ts_utc") if away_mkt else "")
        v_age = fmt_age_min(veg.get("snap_ts_utc") if veg else "")

        # Match info
        away_abbr = NAME_TO_ABBR.get(away_full, away_full[:3].upper())
        home_abbr = NAME_TO_ABBR.get(home_full, home_full[:3].upper())
        matchup = f"{away_abbr} @ {home_abbr}"

        # Build status
        status_parts = []
        if et in open_by_event:
            for p in open_by_event[et]:
                md = p.get("signal_metadata") or {}
                status_parts.append(
                    f"{GREEN}OPEN {p.get('side')} ${p.get('cost_usd', 0):.2f}{RESET}"
                )
        elif (delta_away is not None and abs(delta_away) >= DELTA_THRESHOLD) or \
             (delta_home is not None and abs(delta_home) >= DELTA_THRESHOLD):
            status_parts.append(f"{YELLOW}TRIGGER-CANDIDATE{RESET}")
        elif av is None or ap is None:
            status_parts.append(f"{DIM}no vegas/kalshi join{RESET}")
        else:
            status_parts.append(f"{DIM}below threshold{RESET}")

        # Format prices
        def fmt_p(v):
            if v is None: return "  -  "
            return f"{v:.3f}"

        ap_s = fmt_p(ap); hp_s = fmt_p(hp)
        av_s = fmt_p(av); hv_s = fmt_p(hv)
        kalshi_str = f"{ap_s}/{hp_s}"
        vegas_str = f"{av_s}/{hv_s}"
        da_s = color_delta(delta_away) if delta_away is not None else f"{DIM}   -   {RESET}"
        dh_s = color_delta(delta_home) if delta_home is not None else f"{DIM}   -   {RESET}"
        spr_s = f"{spread}c" if spread is not None else "-"
        status = " ".join(status_parts)

        # Sort by largest |delta|
        max_delta = max(abs(delta_away or 0), abs(delta_home or 0))
        rows.append((max_delta, matchup, kalshi_str, vegas_str, da_s, dh_s, spr_s, k_age, v_age, status))

    # Sort: trigger candidates first, then by |delta|
    rows.sort(key=lambda r: -r[0])
    for r in rows:
        out.append(f"{r[1]:<32} {r[2]:<22} {r[3]:<22} {r[4]:<19} {r[5]:<19} {r[6]:<5} {r[7]:<6} {r[8]:<6} {r[9]}")

    out.append("")
    out.append(f"{DIM}Refresh in N seconds. Ctrl+C to exit. "
               f"Kalshi snapshot age = time since last logger tick. "
               f"Vegas age = time since last Odds API pull.{RESET}")

    sys.stdout.write("\n".join(out))
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-sec", type=int, default=15,
                    help="Refresh cadence (default 15s).")
    args = ap.parse_args()
    signal.signal(signal.SIGINT, _handle_sigint)
    while not _STOP:
        try:
            render()
        except Exception as e:
            sys.stdout.write(f"\n[dashboard error] {e}\n")
            sys.stdout.flush()
        slept = 0
        while slept < args.refresh_sec and not _STOP:
            time.sleep(1)
            slept += 1
    sys.stdout.write(RESET + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
