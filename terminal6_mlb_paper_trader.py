"""Terminal 6 — MLB Paper Trader.

Joins the latest Kalshi MLB snapshot with the latest sharp Vegas
consensus lines. For each market where |sharp_implied_p − kalshi_implied_p|
≥ DELTA_THRESHOLD, opens a shadow position via ShadowLedger.

DRY-RUN BY DEFAULT. Pass --live to write to ledger.

Edge thesis (per terminal6_mlb_spec.md):
  Sharp books update on lineup/weather news 30-90 min before Kalshi retail
  flow catches up. Trade the gap. Long YES on the team where sharp consensus
  > Kalshi implied; long NO where Kalshi implied > sharp consensus.

Trader gates:
  1. freshness_alarm.flag present → force dry-run for this cycle
  2. Vegas line for the joined game must be ≤ 60 min old (engine-specific
     freshness check — sharp consensus must be live, not stale)
  3. Kalshi spread ≤ MAX_SPREAD_CENTS — no fills inside wide books
  4. Kalshi implied p in [MIN_P, MAX_P] — skip extreme priced markets
  5. Game start ≥ MIN_LEAD_MIN minutes away — Kalshi closes minutes before
     first pitch; we want margin

Usage:
    python3 ~/Documents/terminal6_mlb_paper_trader.py --once
    python3 ~/Documents/terminal6_mlb_paper_trader.py --once --live
    nohup caffeinate -is python3 ~/Documents/terminal6_mlb_paper_trader.py \\
        --interval-sec 1800 \\
        > ~/Documents/terminal6_data/paper_trader.out 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Allow imports from ~/Documents
sys.path.insert(0, str(Path.home() / "Documents"))

from shadow_pnl_core import ShadowLedger, _read_ledger  # noqa: E402

ENGINE = "T6"
DATA_DIR = Path.home() / "Documents" / "terminal6_data"
LOG_PATH = DATA_DIR / "paper_trader.log"

# --- Phase 1 parameters (per spec §13) ---
DELTA_THRESHOLD = 0.03        # 3 percentage points
MIN_CONTRACTS = 5             # floor — never bet less than this if signal triggers
MAX_CONTRACTS = 500           # ceiling — variance cap on a single position
KELLY_FRACTION = 0.5          # half-Kelly (industry standard, lower variance vs full Kelly)
MAX_BET_PCT_BANKROLL = 0.05   # never stake more than 5% of bankroll on a single bet
MAX_TOTAL_EXPOSURE_PCT = 0.50 # never have more than 50% of bankroll tied up across all opens
BANKROLL_USD_INITIAL = 12000  # T6 starting bankroll (engines.json T6.bankroll_usd).
                              # Bankroll history (2026-05-09): $5K → $10K (T2 archive absorb)
                              # → $12K (+$2K from T1 sub-bucket capital recycle). Kelly cap 5%
                              # unchanged. Max single position $600. Total exposure cap $6K (50%).
                              # Live bankroll = INITIAL + realized P&L; recomputed each cycle.
# Kalshi fee model — taker rate × p × (1-p) per contract on entry.
# Settlement is fee-free; we hold positions to settle so we pay one-sided fee only.
# If a position is ever closed pre-settle, an exit fee of equal magnitude applies.
KALSHI_TAKER_FEE_RATE = 0.07
MAX_DAILY_OPENS = 15
MAX_TOTAL_OPENS = 30
MIN_KALSHI_P = 0.20
MAX_KALSHI_P = 0.80
MAX_SPREAD_CENTS = 5
MIN_LEAD_MINUTES = 15
MAX_VEGAS_AGE_MIN = 60
DEFAULT_INTERVAL_SEC = 1800   # 30 min

_STOP_REQUESTED = False


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _handle_sigint(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    log("[signal] stop requested")


# --------------------------------------------------------------------------
# Vegas lines: load most recent line per game
# --------------------------------------------------------------------------

def load_latest_vegas_lines() -> Dict[str, dict]:
    """Return {game_id: most-recent line dict} from today's vegas_lines file.

    File format: ~/Documents/terminal6_data/vegas_lines_{YYYY-MM-DD}.jsonl
    Each row: {snap_ts_utc, game_id, commence_time_utc, home_team, away_team,
               consensus_home_p, consensus_away_p, ...}
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = DATA_DIR / f"vegas_lines_{today}.jsonl"
    latest: Dict[str, dict] = {}
    if not path.exists():
        log(f"  [warn] no vegas lines file at {path}")
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
                gid = r.get("game_id")
                if not gid:
                    continue
                prev = latest.get(gid)
                if prev is None or r.get("snap_ts_utc", "") > prev.get("snap_ts_utc", ""):
                    latest[gid] = r
    except OSError as e:
        log(f"  [error] could not read vegas lines: {e}")
    return latest


# --------------------------------------------------------------------------
# Kalshi snapshots: load most recent row per market
# --------------------------------------------------------------------------

def load_latest_kalshi_markets() -> List[dict]:
    """Read all today's KXMLBGAME snapshot files; return one row per (event,
    ticker) — the most recent."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pattern = f"kalshi_KXMLBGAME-*_{today}.jsonl"
    files = sorted(DATA_DIR.glob(pattern))
    latest: Dict[str, dict] = {}
    for path in files:
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
    return list(latest.values())


# --------------------------------------------------------------------------
# Team-name normalization for join
# --------------------------------------------------------------------------

# Map Kalshi 2-3 letter team codes ↔ Odds API full names. The Odds API uses
# full team names like "New York Yankees", Kalshi uses abbreviations.
KALSHI_TO_NAME = {
    "ATL": "Atlanta Braves", "ARI": "Arizona Diamondbacks", "AZ": "Arizona Diamondbacks",
    "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox", "CHC": "Chicago Cubs",
    "CWS": "Chicago White Sox", "CHW": "Chicago White Sox", "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians", "COL": "Colorado Rockies", "DET": "Detroit Tigers",
    "HOU": "Houston Astros", "KC": "Kansas City Royals", "KCR": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers", "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers", "MIN": "Minnesota Twins", "NYY": "New York Yankees",
    "NYM": "New York Mets", "OAK": "Athletics", "ATH": "Athletics",
    # Note 2026-05-09: Odds API now returns "Athletics" (no city) since the
    # 2024 Oakland departure / Sacramento-Las Vegas relocation. Historical
    # snapshots may still say "Oakland Athletics"; team_name_match() normalizes
    # both forms to "Athletics" before comparison.
    "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates", "SD": "San Diego Padres",
    "SDP": "San Diego Padres", "SF": "San Francisco Giants", "SFG": "San Francisco Giants",
    "SEA": "Seattle Mariners", "STL": "St. Louis Cardinals", "TB": "Tampa Bay Rays",
    "TBR": "Tampa Bay Rays", "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals", "WAS": "Washington Nationals",
}


def parse_kalshi_event_teams(event_ticker: str) -> Optional[Tuple[str, str]]:
    """KXMLBGAME-26MAY101920DETKC → (away_full, home_full) using map.
    Convention: away team listed first.
    """
    if not event_ticker.startswith("KXMLBGAME-"):
        return None
    rest = event_ticker[len("KXMLBGAME-"):]
    # date+time = 11 chars; teams = remainder
    if len(rest) < 12:
        return None
    team_part = rest[11:]
    # Try splits: 2/2, 2/3, 3/2, 3/3
    for split in (2, 3):
        if 2 <= len(team_part) - split <= 3:
            away = team_part[:split]
            home = team_part[split:]
            if away in KALSHI_TO_NAME and home in KALSHI_TO_NAME:
                return KALSHI_TO_NAME[away], KALSHI_TO_NAME[home]
    return None


_TICKER_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_ticker_game_dt_utc(event_ticker: str) -> Optional[datetime]:
    """KXMLBGAME-26MAY091610WSHMIA → datetime(2026,5,9,16,10) ET → UTC.

    Born 2026-05-10 to fix the multi-game series mismatch bug: vegas
    matching by team-pair alone selected the soonest *upcoming* WSH/MIA
    game (May 10) when the Kalshi ticker was for the already-settled
    May-9 game. Result: 9× fires on a settled ticker over ~12 hours,
    fabricating $3,549 of P&L. The ticker carries an unambiguous date
    inside it; constrain candidates to within ±12h of this datetime.

    Kalshi tickers encode local game start in ET. MLB regular season
    runs Apr–Sep, all DST/EDT. Postseason can cross the Nov DST
    boundary; ZoneInfo handles both cleanly.
    """
    if not event_ticker.startswith("KXMLBGAME-"):
        return None
    rest = event_ticker[len("KXMLBGAME-"):]
    if len(rest) < 11:
        return None
    try:
        yy = int(rest[0:2])
        mmm = rest[2:5].upper()
        dd = int(rest[5:7])
        hh = int(rest[7:9])
        mm = int(rest[9:11])
    except ValueError:
        return None
    if mmm not in _TICKER_MONTHS:
        return None
    try:
        local = datetime(2000 + yy, _TICKER_MONTHS[mmm], dd, hh, mm)
    except ValueError:
        return None
    return local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Trade trigger logic
# --------------------------------------------------------------------------

def evaluate_market(market_row: dict, vegas_by_game: Dict[str, dict],
                    now_utc: datetime) -> Optional[dict]:
    """Return a signal dict if the market triggers a trade, else None.

    Signal: side=YES if sharp > kalshi (we think kalshi is underpricing),
    side=NO if kalshi > sharp (overpricing).
    """
    et = market_row.get("event_ticker") or ""
    ticker = market_row.get("ticker") or ""
    teams = parse_kalshi_event_teams(et)
    if not teams:
        return {"reject_reason": f"unparseable event {et}"}
    away, home = teams

    # Determine which team this market is on (subtitle holds team name typically)
    yes_sub = (market_row.get("yes_sub_title") or
               market_row.get("subtitle") or "").strip()
    if not yes_sub:
        return {"reject_reason": "no subtitle"}
    market_team = None
    if yes_sub.lower() in home.lower() or home.lower() in yes_sub.lower():
        market_team = "home"
    elif yes_sub.lower() in away.lower() or away.lower() in yes_sub.lower():
        market_team = "away"
    else:
        # Subtitle didn't match — try Kalshi suffix from ticker (e.g. -DET)
        suffix_match = re.search(r"-([A-Z]{2,3})$", ticker)
        if suffix_match:
            kcode = suffix_match.group(1)
            full = KALSHI_TO_NAME.get(kcode)
            if full == home:
                market_team = "home"
            elif full == away:
                market_team = "away"
    if not market_team:
        return {"reject_reason": f"could not match team for {ticker}"}

    # Find matching Vegas line by team names + commence date proximity.
    # Normalize "Oakland Athletics" / "Athletics" to a single form so the
    # post-2024 rebrand doesn't cause silent no-match.
    #
    # 2026-05-09 BUG FIX: when multiple Vegas games match the same team-pair
    # (MLB series — same teams play consecutive days), the loop used to take
    # the first match, which was usually yesterday's game. Yesterday's
    # commence_time is past, lead_min went hugely negative, and the market
    # got rejected as "game starts in -1100 min". Tonight's upcoming game
    # was silently invisible. Now: collect all matches, prefer the soonest
    # upcoming game; if none are upcoming, fall back to the most recent past
    # within a 36h window.
    def _normalize(name: str) -> str:
        if not name:
            return ""
        return name.replace("Oakland Athletics", "Athletics").strip()
    home_n, away_n = _normalize(home), _normalize(away)

    candidates = [
        g for g in vegas_by_game.values()
        if _normalize(g.get("home_team")) == home_n and _normalize(g.get("away_team")) == away_n
    ]
    if not candidates:
        return {"reject_reason": "no vegas match"}

    def _commence_dt(g):
        c = g.get("commence_time_utc")
        if not c:
            return None
        try:
            return datetime.fromisoformat(c.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            return None

    WINDOW = timedelta(hours=36)
    in_window = [g for g in candidates
                 if _commence_dt(g) and abs(_commence_dt(g) - now_utc) <= WINDOW]
    if not in_window:
        return {"reject_reason": "no vegas match (no game within 36h)"}

    # 2026-05-10 BUG FIX: narrow candidates by the *ticker's* encoded
    # game date BEFORE the future/past selection. Without this, a
    # multi-day series matched team-pair-only and the "soonest upcoming"
    # rule picked tomorrow's game — but the Kalshi ticker we're scoring
    # was yesterday's already-settled game. Result: stale-ticker fires
    # ($3,549 fabricated on WSH/MIA May-9 ticker priced against the
    # May-10 Vegas line). The ticker carries an unambiguous date; use it.
    ticker_dt_utc = parse_ticker_game_dt_utc(et)
    if ticker_dt_utc is not None:
        TICKER_DT_WINDOW = timedelta(hours=12)
        date_matched = [g for g in in_window
                        if _commence_dt(g) is not None
                        and abs(_commence_dt(g) - ticker_dt_utc) <= TICKER_DT_WINDOW]
        if not date_matched:
            return {"reject_reason": "no vegas match (no game within 12h of ticker date)"}
        in_window = date_matched

    future = [g for g in in_window if _commence_dt(g) > now_utc]
    if future:
        # Soonest upcoming game wins.
        vegas = min(future, key=_commence_dt)
    else:
        # All matches are in the past — pick the most recent for diagnostic
        # accuracy (will be rejected at the lead-time gate next).
        vegas = max(in_window, key=_commence_dt)

    # Game start lead time FIRST — deterministic, no external freshness dependency.
    # Reject in-progress / about-to-start games before checking Vegas line age,
    # because the Odds API drops in-progress games from its feed and their
    # "stale Vegas" reading is misleading. The real reason these markets
    # don't trade is the lead-time gate.
    commence = vegas.get("commence_time_utc")
    try:
        commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        lead_min = (commence_dt - now_utc).total_seconds() / 60.0
    except (TypeError, ValueError, AttributeError):
        return {"reject_reason": "commence time unparseable"}
    if lead_min < MIN_LEAD_MINUTES:
        return {"reject_reason": f"game starts in {lead_min:.0f} min"}

    # Vegas freshness check — only meaningful for upcoming games.
    vegas_ts_str = vegas.get("snap_ts_utc")
    try:
        vegas_ts = datetime.fromisoformat(vegas_ts_str.replace("Z", "+00:00"))
        age_min = (now_utc - vegas_ts).total_seconds() / 60.0
    except (TypeError, ValueError, AttributeError):
        return {"reject_reason": "vegas timestamp unparseable"}
    if age_min > MAX_VEGAS_AGE_MIN:
        return {"reject_reason": f"vegas line stale ({age_min:.0f} min)"}

    # Kalshi book sanity
    yes_top = market_row.get("yes_top_price_cents")
    no_top = market_row.get("no_top_price_cents")
    spread = market_row.get("spread_cents")
    if yes_top is None or no_top is None:
        return {"reject_reason": "no two-sided book"}
    if spread is None or spread > MAX_SPREAD_CENTS:
        # Collapse all over-spread rejections into one bucket so the breakdown
        # aggregates instead of producing one category per unique spread value.
        return {"reject_reason": f"spread > {MAX_SPREAD_CENTS}c"}

    # Kalshi implied probability of THIS team (the YES-side team) winning.
    # yes_top is the best bid; the implied ask = 100 - no_top. Use mid.
    yes_ask_cents = 100 - no_top
    kalshi_implied_p = ((yes_top + yes_ask_cents) / 2.0) / 100.0
    if kalshi_implied_p < MIN_KALSHI_P or kalshi_implied_p > MAX_KALSHI_P:
        # Collapse out-of-band rejections so heavy favorites and heavy dogs
        # aggregate into one bucket instead of splintering by exact price.
        return {"reject_reason": f"kalshi p outside [{MIN_KALSHI_P},{MAX_KALSHI_P}]"}

    # Sharp consensus for the SAME team (which side this market is on)
    sharp_p_field = "consensus_home_p" if market_team == "home" else "consensus_away_p"
    sharp_p = vegas.get(sharp_p_field)
    if sharp_p is None:
        return {"reject_reason": "vegas consensus null"}

    delta = sharp_p - kalshi_implied_p

    # KL divergence (parallel calculation per Steve 2026-05-09).
    # D_KL(sharp || kalshi) = sharp*ln(sharp/kalshi) + (1-sharp)*ln((1-sharp)/(1-kalshi))
    # Logged for empirical threshold calibration at n=50; trigger remains
    # linear delta until calibration data available.
    import math
    p_clip = lambda x: max(0.01, min(0.99, x))
    sp = p_clip(sharp_p)
    kp = p_clip(kalshi_implied_p)
    kl_nats = sp * math.log(sp / kp) + (1.0 - sp) * math.log((1.0 - sp) / (1.0 - kp))

    if abs(delta) < DELTA_THRESHOLD:
        # Bucket all sub-threshold deltas under one category so the breakdown
        # aggregates them; embedding the value made each market its own category
        # and the top-10 truncation hid them entirely.
        # KL preserved on rejection (Steve 2026-05-09): near-miss markets are
        # the larger sample for empirical KL distribution fitting. The Monday
        # milestone task reads near_miss_kl.jsonl to determine whether KL
        # would have caught signals the linear delta threshold rejected.
        return {
            "reject_reason": f"|delta| < {DELTA_THRESHOLD} (sub-threshold)",
            "ticker": ticker,
            "event_ticker": et,
            "kalshi_implied_p": kalshi_implied_p,
            "sharp_p": sharp_p,
            "delta": delta,
            "abs_delta": abs(delta),
            "kl_nats": kl_nats,
            "lead_min": lead_min,
            "spread_cents": spread,
            "_near_miss": True,
        }

    side = "YES" if delta > 0 else "NO"
    if side == "YES":
        entry_cents = yes_ask_cents       # buy YES at ask
    else:
        entry_cents = 100 - yes_top       # buy NO; NO ask = 100 - YES bid

    return {
        "ticker": ticker,
        "event_ticker": et,
        "side": side,
        "team_market": market_team,
        "team_name": home if market_team == "home" else away,
        "kalshi_implied_p": kalshi_implied_p,
        "sharp_p": sharp_p,
        "delta": delta,
        "kl_nats": kl_nats,
        "entry_price": entry_cents / 100.0,
        "spread_cents": spread,
        "lead_min": lead_min,
        "vegas_age_min": age_min,
        "yes_top_cents": yes_top,
        "no_top_cents": no_top,
        "books_used": vegas.get("books_used"),
        "commence_time_utc": commence,
    }


def kalshi_taker_fee_per_contract(price: float) -> float:
    """Kalshi taker fee per contract on order entry: rate × p × (1-p)."""
    if price <= 0 or price >= 1:
        return 0.0
    return KALSHI_TAKER_FEE_RATE * price * (1.0 - price)


def kelly_size_contracts(side: str, entry_price: float, our_p: float,
                         bankroll: float, current_exposure_usd: float) -> Tuple[int, dict]:
    """Compute Kelly-sized position in contracts, accounting for Kalshi fees.

    For YES side at price p with our estimated win prob q:
        Per-contract effective stake = p + fee
        Per-contract net payout if win = 1 - p - fee  (settlement at $1, minus what we paid)
        Per-contract loss if lose = p + fee
        b (net odds) = net_payout / effective_stake
        f* = (b*q - (1-q)) / b
    For NO side: q = (1 - our_p_yes), p = NO entry price.

    Returns (contracts, sizing_metadata). contracts=0 means skip.
    """
    if side == "YES":
        q = our_p
        p = entry_price
    else:
        q = 1.0 - our_p
        p = entry_price
    if p <= 0 or p >= 1:
        return 0, {"reason": "invalid price", "q": q, "p": p}

    fee_per_contract = kalshi_taker_fee_per_contract(p)
    effective_stake = p + fee_per_contract     # what we lose if the bet loses
    net_payout = 1.0 - p - fee_per_contract    # what we gain if the bet wins (settlement at $1)
    if net_payout <= 0 or effective_stake <= 0:
        return 0, {"reason": "fee swallows payout", "fee": fee_per_contract,
                   "effective_stake": effective_stake, "net_payout": net_payout}

    b = net_payout / effective_stake           # net odds per dollar risked
    f_full = (b * q - (1.0 - q)) / b
    if f_full <= 0:
        return 0, {
            "reason": "edge dissolved by fees",
            "f_full": f_full, "b": b, "q": q, "p": p,
            "fee_per_contract": fee_per_contract,
        }

    f_used = f_full * KELLY_FRACTION
    bet_usd = bankroll * f_used

    # Per-bet cap (5% of live bankroll)
    max_bet_usd = bankroll * MAX_BET_PCT_BANKROLL
    bet_usd = min(bet_usd, max_bet_usd)

    # Total exposure cap (50% of live bankroll across all open positions)
    headroom_usd = max(0.0, bankroll * MAX_TOTAL_EXPOSURE_PCT - current_exposure_usd)
    bet_usd = min(bet_usd, headroom_usd)

    # Stake in dollars → contracts. Use effective_stake (includes fee) so the
    # cap works in true-cost terms, not nominal entry-price terms.
    contracts = int(bet_usd / effective_stake)
    if contracts < MIN_CONTRACTS:
        if bet_usd < MIN_CONTRACTS * effective_stake:
            return 0, {
                "reason": "below MIN_CONTRACTS floor",
                "f_full": f_full, "f_used": f_used,
                "bet_usd": bet_usd, "fee_per_contract": fee_per_contract,
                "contracts_implied": contracts,
            }
        contracts = MIN_CONTRACTS
    contracts = min(contracts, MAX_CONTRACTS)

    return contracts, {
        "f_full": f_full,                   # Kelly-with-fees
        "f_used": f_used,                   # × KELLY_FRACTION
        "b": b,                             # net odds after fees
        "q": q, "p": p,
        "fee_per_contract": fee_per_contract,
        "effective_stake_per_contract": effective_stake,
        "net_payout_per_contract": net_payout,
        "bet_usd": contracts * effective_stake,
        "max_bet_cap_usd": max_bet_usd,
        "exposure_headroom_usd": headroom_usd,
        "bankroll_usd": bankroll,
    }


def already_open_on_event(event_ticker: str) -> bool:
    """Cluster cap: 1 position per game. Replays the ledger for open T6 positions."""
    opens_by_pid = {}
    closed_pids = set()
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        if r.get("type") == "open":
            opens_by_pid[r["position_id"]] = r
        elif r.get("type") == "close":
            closed_pids.add(r["position_id"])
    open_events = set()
    for pid, o in opens_by_pid.items():
        if pid in closed_pids:
            continue
        md = o.get("signal_metadata") or {}
        if md.get("event_ticker") == event_ticker:
            open_events.add(event_ticker)
    return event_ticker in open_events


def count_open_t6_positions() -> int:
    open_pids = set()
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        if r.get("type") == "open":
            open_pids.add(r["position_id"])
        elif r.get("type") == "close":
            open_pids.discard(r["position_id"])
    return len(open_pids)


def t6_current_exposure_usd() -> float:
    """Sum of cost_usd across all currently open T6 positions."""
    opens: Dict[str, dict] = {}
    closed = set()
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        if r.get("type") == "open":
            opens[r["position_id"]] = r
        elif r.get("type") == "close":
            closed.add(r["position_id"])
    return sum(
        float(o.get("cost_usd") or 0)
        for pid, o in opens.items() if pid not in closed
    )


def t6_live_bankroll() -> float:
    """Current T6 bankroll = INITIAL + sum(CLEAN realized P&L from closed T6 positions).

    This is what Kelly should size on — bankroll grows or shrinks with realized
    outcomes. Open positions do NOT count toward bankroll (their P&L is unrealized).
    Open exposure is tracked separately and capped by MAX_TOTAL_EXPOSURE_PCT.

    2026-05-10: filters out vegas-match-contaminated closes (the wrong-day
    binding bug fixed earlier this session). Without this filter, Kelly
    sizing would inflate by the ~$3,696 of fabricated P&L from the bug
    period — sizing cap would shift from $600 (5% of $12K true) to $785
    (5% of $15.7K inflated). Matches the same filter applied in
    terminal6_milestone_check.py, terminal6_dashboard.py, and
    shadow_dashboard.py.
    """
    CONTAMINATION_WINDOW = timedelta(hours=12)
    opens: Dict[str, dict] = {}
    realized = 0.0
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        t = r.get("type")
        pid = r.get("position_id")
        if t == "open" and pid:
            opens[pid] = r
        elif t == "close" and pid and pid in opens:
            op = opens[pid]
            meta = op.get("signal_metadata") or {}
            ticker_dt = parse_ticker_game_dt_utc(meta.get("event_ticker") or "")
            commence_str = meta.get("commence_time_utc") or ""
            contaminated = False
            if ticker_dt and commence_str:
                try:
                    commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                    contaminated = abs(commence_dt - ticker_dt) > CONTAMINATION_WINDOW
                except (TypeError, ValueError):
                    pass
            if contaminated:
                continue  # exclude from Kelly bankroll
            try:
                realized += float(r.get("realized_pnl_usd") or 0)
            except (TypeError, ValueError):
                continue
    return BANKROLL_USD_INITIAL + realized


def count_today_t6_opens() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    for r in _read_ledger():
        if r.get("engine") != ENGINE or r.get("type") != "open":
            continue
        ts = r.get("ts", "")
        if ts.startswith(today):
            n += 1
    return n


def trade_once(dry_run: bool) -> dict:
    # Freshness gate
    flag = Path.home() / "Documents" / "freshness_alarm.flag"
    if flag.exists() and not dry_run:
        try:
            log(f"[freshness_alarm] flag present — forcing dry-run")
            log(f"[freshness_alarm] {flag.read_text().strip()}")
        except OSError:
            log(f"[freshness_alarm] flag present — forcing dry-run")
        dry_run = True

    now = datetime.now(timezone.utc)
    vegas_lines = load_latest_vegas_lines()
    log(f"loaded vegas: {len(vegas_lines)} games with consensus")

    kalshi_markets = load_latest_kalshi_markets()
    log(f"loaded kalshi: {len(kalshi_markets)} latest market rows")

    if not vegas_lines or not kalshi_markets:
        log("insufficient data — skipping cycle")
        return {"opened": 0, "rejected": 0}

    # Caps + live bankroll
    n_open = count_open_t6_positions()
    n_today = count_today_t6_opens()
    exposure_usd = t6_current_exposure_usd()
    bankroll = t6_live_bankroll()
    log(f"current T6 state: {n_open} open positions ({n_today} today), "
        f"bankroll=${bankroll:.2f} (Δ${bankroll - BANKROLL_USD_INITIAL:+.2f}), "
        f"exposure=${exposure_usd:.2f}/${bankroll * MAX_TOTAL_EXPOSURE_PCT:.0f}")
    if n_open >= MAX_TOTAL_OPENS:
        log(f"  [cap] total opens {n_open} ≥ {MAX_TOTAL_OPENS}, skipping")
        return {"opened": 0, "rejected": 0}
    if n_today >= MAX_DAILY_OPENS:
        log(f"  [cap] daily opens {n_today} ≥ {MAX_DAILY_OPENS}, skipping")
        return {"opened": 0, "rejected": 0}

    sl = ShadowLedger() if not dry_run else None
    snap_ts = now.isoformat()

    accepted = []
    rejected_counts: Dict[str, int] = defaultdict(int)
    near_misses: List[dict] = []  # sub-threshold delta rejections with full signal data
    for m in kalshi_markets:
        sig = evaluate_market(m, vegas_lines, now)
        if sig is None:
            rejected_counts["null"] += 1
            continue
        if "reject_reason" in sig:
            rejected_counts[sig["reject_reason"][:30]] += 1
            if sig.get("_near_miss"):
                near_misses.append(sig)
            continue
        accepted.append(sig)

    # Near-miss KL telemetry (Steve 2026-05-09).
    # Near-miss markets are sub-threshold-delta rejections — they carry full
    # signal metadata (delta, KL, prices) that's the larger sample for
    # empirical KL distribution fitting vs waiting for n=50 closes.
    # Persist to terminal6_data/near_miss_kl.jsonl for milestone task to read.
    if near_misses:
        import statistics
        kls = [m["kl_nats"] for m in near_misses]
        deltas = [m["abs_delta"] for m in near_misses]
        log(f"near-miss telemetry: n={len(near_misses)} "
            f"|delta| median={statistics.median(deltas):.4f} max={max(deltas):.4f} | "
            f"KL median={statistics.median(kls):.5f} max={max(kls):.5f} | "
            f"would-trigger-at-KL≥0.001: {sum(1 for k in kls if k >= 0.001)} "
            f"(KL≥0.005: {sum(1 for k in kls if k >= 0.005)})")
        try:
            nm_path = Path.home() / "Documents" / "terminal6_data" / "near_miss_kl.jsonl"
            nm_path.parent.mkdir(parents=True, exist_ok=True)
            with open(nm_path, "a") as f:
                for m in near_misses:
                    rec = {"cycle_ts": snap_ts, **{k: v for k, v in m.items()
                                                    if k not in ("_near_miss",)}}
                    f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log(f"  [warn] near_miss_kl persistence failed: {e}")

    if rejected_counts:
        log("rejection breakdown:")
        # Show ALL categories — no top-N truncation. Hidden categories were
        # masking real failure modes (the 20-market silent rejection that
        # took half a session to find on 2026-05-09).
        for reason, n in sorted(rejected_counts.items(), key=lambda x: -x[1]):
            log(f"    {reason:<35} {n}")

    log(f"accepted signals: {len(accepted)}")

    # Sort by |delta| descending — fire highest-edge first
    accepted.sort(key=lambda s: -abs(s["delta"]))

    opened = 0
    for sig in accepted:
        if n_open + opened >= MAX_TOTAL_OPENS:
            log(f"  [cap-mid] total opens reached, stopping")
            break
        if n_today + opened >= MAX_DAILY_OPENS:
            log(f"  [cap-mid] daily opens reached, stopping")
            break
        if already_open_on_event(sig["event_ticker"]):
            log(f"  [skip] already open on {sig['event_ticker']}")
            continue

        # Entropy collapse defensive gate (added 2026-05-09).
        try:
            from entropy_alert_helpers import should_block_for_collapse
            blocked, br = should_block_for_collapse(
                ticker=sig["ticker"], proposed_side=sig["side"], engine=ENGINE,
            )
            if blocked:
                log(f"  [entropy-block] {sig['ticker']} {br}")
                continue
        except Exception as e:
            log(f"  [warn] entropy gate failed open: {e}")

        # Kelly sizing — uses LIVE bankroll (refreshed at top of cycle) and
        # running exposure (incremented as we open within this cycle so each
        # new bet respects the residual headroom against the cap).
        contracts, sizing_md = kelly_size_contracts(
            side=sig["side"],
            entry_price=sig["entry_price"],
            our_p=sig["sharp_p"],
            bankroll=bankroll,
            current_exposure_usd=exposure_usd,
        )
        if contracts == 0:
            log(f"  [skip-size] {sig['ticker']} {sizing_md.get('reason', '?')}")
            continue
        # Cost reported to ledger uses entry_price (Kalshi convention); fee is
        # accounted for separately. effective_stake (price + fee) drives sizing.
        cost_usd = contracts * sig["entry_price"]
        fee_usd = contracts * sizing_md.get("fee_per_contract", 0)

        log(f"  [{'FIRE' if not dry_run else 'DRY'}] {sig['ticker']:<40} "
            f"{sig['side']} sz={contracts:>3} entry=${sig['entry_price']:.3f} "
            f"cost=${cost_usd:.2f} fee=${fee_usd:.2f} "
            f"kalshi={sig['kalshi_implied_p']:.3f} "
            f"sharp={sig['sharp_p']:.3f} "
            f"delta={sig['delta']:+.3f} "
            f"kl={sig.get('kl_nats', 0):.4f} "
            f"f_full={sizing_md.get('f_full', 0):.3f}")

        if not dry_run and sl is not None:
            md = {k: v for k, v in sig.items()}
            md["snap_ts"] = snap_ts
            md["sizing"] = sizing_md
            sl.open(
                engine=ENGINE,
                venue="kalshi",
                ticker=sig["ticker"],
                side=sig["side"],
                size=contracts,
                price=sig["entry_price"],
                signal_metadata=md,
                fee_usd=fee_usd,
            )
            # Track exposure incrementally so the next signal in this cycle
            # respects the running headroom (use effective stake including fee)
            exposure_usd += contracts * sizing_md.get("effective_stake_per_contract", sig["entry_price"])
        opened += 1

    log(f"cycle done: opened={opened} accepted={len(accepted)} "
        f"rejected={sum(rejected_counts.values())}")
    return {"opened": opened, "accepted": len(accepted),
            "rejected": sum(rejected_counts.values())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Single cycle, then exit.")
    ap.add_argument("--live", action="store_true",
                    help="Write to ledger. Default is DRY-RUN.")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC,
                    help="Cycle cadence in seconds (default 1800 = 30 min).")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dry_run = not args.live
    log(f"T6 MLB Paper Trader starting; live={args.live} once={args.once} "
        f"interval={args.interval_sec}s")
    log(f"  config: delta≥{DELTA_THRESHOLD} kelly={KELLY_FRACTION:.1f} "
        f"per-bet-cap={MAX_BET_PCT_BANKROLL:.0%} "
        f"total-exposure-cap={MAX_TOTAL_EXPOSURE_PCT:.0%} "
        f"bankroll_initial=${BANKROLL_USD_INITIAL} (live = initial + realized P&L) "
        f"taker_fee_rate={KALSHI_TAKER_FEE_RATE} "
        f"max_daily={MAX_DAILY_OPENS} max_total={MAX_TOTAL_OPENS}")

    if args.once:
        trade_once(dry_run)
        return 0

    while not _STOP_REQUESTED:
        try:
            trade_once(dry_run)
        except Exception as e:
            log(f"[error] cycle raised: {e}")
        slept = 0
        while slept < args.interval_sec and not _STOP_REQUESTED:
            time.sleep(1)
            slept += 1

    log("T6 MLB Paper Trader stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
