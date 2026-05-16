"""Terminal 3c — Initial Claims Paper Trader.

Generates signals on KXJOBLESSCLAIMS weekly markets using a locally-computed
nowcast: μ = 4-week trailing ICSA mean, σ = 26-week WoW pstdev. Opens
shadow positions via shadow_pnl_core (engine="T3c").

Edge logic (per spec):
    our_p   = P(claims ≥ strike) using Normal(μ, σ)
    side YES if our_p > market_p else NO
    market_p = (yes_bid + yes_ask) / 2   (mid)
    YES entry = yes_ask
    NO  entry = 1 - yes_bid

Filters (per spec):
    min_edge       $0.10
    yes_bid        ∈ [0.10, 0.90]
    our_p          ∈ [0.05, 0.95]
    volume_24h ≥ 50  OR  open_interest ≥ 200
    hours_to_close ∈ [4, 144]
    not already open (ticker)
    cluster cap    5 strikes per event
    YES side adds  edge ≥ $0.15 AND yes_bid ∈ [0.15, 0.85]   (T1 lesson)
    cost basis     ≤ $150 per event

Usage:
    python3 ~/Documents/terminal3c_paper_trader.py --dry-run
    python3 ~/Documents/terminal3c_paper_trader.py
    nohup caffeinate -is python3 ~/Documents/terminal3c_paper_trader.py \\
        --interval-sec 600 > ~/Documents/terminal3c_data/paper_trader.out 2>&1 &
"""

import argparse
import json
import math
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger, _read_ledger  # noqa: E402


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_TICKER = "KXJOBLESSCLAIMS"
ENGINE = "T3c"
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 200

DATA_DIR = Path.home() / "Documents" / "terminal3c_data"
HISTORY_PATH = DATA_DIR / "icsa_history.jsonl"
LOG_PATH = DATA_DIR / "paper_trader.log"

# Strategy parameters (locked in spec 2026-05-02)
MIN_EDGE = 0.10
# 2026-05-03: lowered from $0.15 to $0.10 after switching to real-edge math
# (our_p - yes_ask). The original $0.15 premium absorbed mid-vs-ask spread cost,
# which is now accounted for directly. Double-counting it was over-conservative.
MIN_EDGE_YES = 0.10
YES_BID_RANGE = (0.10, 0.90)
YES_BID_RANGE_YES = (0.15, 0.85)
OUR_P_RANGE = (0.05, 0.95)
MIN_VOL24 = 50
MIN_OI = 200
HOURS_TO_CLOSE_RANGE = (4, 144)
CONTRACTS = 25
COST_CAP_PER_EVENT_USD = 150.0
CLUSTER_CAP = 5

DEFAULT_INTERVAL_SEC = 600

_STOP = False


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


def _handle_sigint(sig, frame):
    global _STOP
    _STOP = True
    log("SIGINT — exiting after current iteration.")


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

def load_icsa_history() -> List[Tuple[str, int]]:
    """Replay history file, dedup on date keeping latest, sort ascending."""
    latest: Dict[str, int] = {}
    if not HISTORY_PATH.exists():
        return []
    with open(HISTORY_PATH, "r") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = rec.get("date")
            v = rec.get("icsa")
            if d and v is not None:
                latest[d] = int(v)
    return sorted(latest.items())


def compute_nowcast(history: List[Tuple[str, int]]) -> Optional[dict]:
    """μ = 4-week trailing mean, σ = 26-week WoW pstdev."""
    if len(history) < 26:
        log(f"[error] insufficient ICSA history ({len(history)} rows; need ≥26)")
        return None
    last4 = [v for _, v in history[-4:]]
    last26 = [v for _, v in history[-26:]]
    mu = sum(last4) / 4
    deltas = [b - a for a, b in zip(last26[:-1], last26[1:])]
    n = len(deltas)
    mean_d = sum(deltas) / n
    var = sum((x - mean_d) ** 2 for x in deltas) / n
    sigma = math.sqrt(var)
    return {
        "mu": mu,
        "sigma": sigma,
        "as_of_date": history[-1][0],
        "last_print_icsa": history[-1][1],
        "trailing_4w": last4,
    }


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_geq_strike(strike: float, mu: float, sigma: float) -> float:
    """P(claims ≥ strike) under Normal(μ, σ)."""
    if sigma <= 0:
        return 1.0 if mu >= strike else 0.0
    z = (strike - mu) / sigma
    return 1.0 - normal_cdf(z)


# ---------------------------------------------------------------------------
# Kalshi fetch
# ---------------------------------------------------------------------------

def fetch_open_events() -> List[dict]:
    out: List[dict] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, object] = {
            "series_ticker": SERIES_TICKER,
            "status": "open",
            "limit": PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(
                f"{KALSHI_BASE}/events", params=params,
                timeout=REQUEST_TIMEOUT,
                proxies={"http": None, "https": None},
            )
        except requests.RequestException as e:
            log(f"[error] /events fetch failed: {e}")
            return out
        if r.status_code != 200:
            log(f"[error] /events returned {r.status_code}")
            return out
        body = r.json()
        out.extend(body.get("events", []) or [])
        cursor = body.get("cursor")
        if not cursor:
            break
    return out


def fetch_markets_for_event(event_ticker: str) -> List[dict]:
    out: List[dict] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, object] = {
            "event_ticker": event_ticker,
            "limit": PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(
                f"{KALSHI_BASE}/markets", params=params,
                timeout=REQUEST_TIMEOUT,
                proxies={"http": None, "https": None},
            )
        except requests.RequestException as e:
            log(f"[error] /markets fetch failed for {event_ticker}: {e}")
            return out
        if r.status_code == 429:
            log(f"[rate-limit] {event_ticker}; retrying after 5s")
            time.sleep(5)
            continue
        if r.status_code != 200:
            log(f"[error] /markets returned {r.status_code} for {event_ticker}")
            return out
        body = r.json()
        out.extend(body.get("markets", []) or [])
        cursor = body.get("cursor")
        if not cursor:
            break
    return out


# ---------------------------------------------------------------------------
# Strike parsing
# ---------------------------------------------------------------------------

def parse_strike(market: dict) -> Optional[float]:
    """Return the claims threshold this market resolves on (in claims units).

    Prefer floor_strike when present (Kalshi populates this for ≥X markets).
    Fall back to parsing the ticker suffix: KXJOBLESSCLAIMS-26MAY07-200000.
    """
    fs = market.get("floor_strike")
    if fs is not None:
        try:
            return float(fs)
        except (TypeError, ValueError):
            pass
    ticker = market.get("ticker") or ""
    m = re.search(r"-(\d{4,7})$", ticker)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Open-position bookkeeping
# ---------------------------------------------------------------------------

def find_open_positions() -> List[dict]:
    """Replay ledger → open T3c position open events (annul-aware)."""
    open_map: Dict[str, dict] = {}
    annulled_close_ts: Dict[str, set] = {}
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        t = r.get("type")
        pid = r.get("position_id")
        if t == "open":
            open_map[pid] = r
        elif t == "close":
            if r.get("ts") not in annulled_close_ts.get(pid, set()):
                open_map.pop(pid, None)
        elif t == "annul_close":
            annulled_close_ts.setdefault(pid, set()).add(r.get("annulled_close_ts"))
    return list(open_map.values())


def event_cost_so_far(open_positions: List[dict], event_ticker: str) -> float:
    return sum(
        p.get("cost_usd", 0)
        for p in open_positions
        if (p.get("signal_metadata") or {}).get("event_ticker") == event_ticker
    )


def event_strike_count(open_positions: List[dict], event_ticker: str) -> int:
    return sum(
        1 for p in open_positions
        if (p.get("signal_metadata") or {}).get("event_ticker") == event_ticker
    )


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def hours_to_close(close_time_iso: Optional[str]) -> Optional[float]:
    if not close_time_iso:
        return None
    try:
        ct = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = ct - datetime.now(timezone.utc)
    return delta.total_seconds() / 3600.0


def evaluate_market(market: dict, event_ticker: str, nowcast: dict) -> Optional[dict]:
    """Return signal dict if market passes filters, else None (with reason)."""
    ticker = market.get("ticker")
    if not ticker:
        return None

    strike = parse_strike(market)
    if strike is None:
        return None

    yes_bid = market.get("yes_bid_dollars")
    yes_ask = market.get("yes_ask_dollars")
    if yes_bid is None or yes_ask is None:
        return None
    yes_bid = float(yes_bid)
    yes_ask = float(yes_ask)

    if not (YES_BID_RANGE[0] <= yes_bid <= YES_BID_RANGE[1]):
        return {"skip": "yes_bid_out_of_range", "yes_bid": yes_bid, "ticker": ticker}

    vol24 = float(market.get("volume_24h_fp") or 0)
    oi = float(market.get("open_interest_fp") or 0)
    if vol24 < MIN_VOL24 and oi < MIN_OI:
        return {"skip": "thin_liquidity", "vol24": vol24, "oi": oi, "ticker": ticker}

    htc = hours_to_close(market.get("close_time"))
    if htc is None:
        return {"skip": "no_close_time", "ticker": ticker}
    if not (HOURS_TO_CLOSE_RANGE[0] <= htc <= HOURS_TO_CLOSE_RANGE[1]):
        return {"skip": "close_window", "hours_to_close": round(htc, 1), "ticker": ticker}

    our_p = p_geq_strike(strike, nowcast["mu"], nowcast["sigma"])
    if not (OUR_P_RANGE[0] <= our_p <= OUR_P_RANGE[1]):
        return {"skip": "our_p_clipped", "our_p": round(our_p, 4), "ticker": ticker}

    market_p = (yes_bid + yes_ask) / 2.0

    # Side selection by mid (which side our nowcast disagrees with the market on),
    # but edge MUST be computed against the price we actually pay — yes_ask for
    # YES, (1 - yes_bid) for NO. Using mid here would over-credit edge by
    # spread/2 and was caught 2026-05-03 (195k borderline candidate).
    if our_p > market_p:
        side = "YES"
        entry = yes_ask
        edge = our_p - yes_ask                # ← real economic edge
        mid_edge = our_p - market_p           # diagnostic only
        bid_lo, bid_hi = YES_BID_RANGE_YES
        if not (bid_lo <= yes_bid <= bid_hi):
            return {"skip": "yes_bid_out_of_range_YES", "yes_bid": yes_bid, "ticker": ticker}
        if edge < MIN_EDGE_YES:
            return {"skip": "edge_below_min_YES", "edge": round(edge, 4),
                    "mid_edge": round(mid_edge, 4), "yes_ask": yes_ask, "ticker": ticker}
    else:
        side = "NO"
        entry = 1.0 - yes_bid
        edge = yes_bid - our_p                # ← real economic edge for NO
        mid_edge = market_p - our_p           # diagnostic only
        if edge < MIN_EDGE:
            return {"skip": "edge_below_min", "edge": round(edge, 4),
                    "mid_edge": round(mid_edge, 4), "yes_bid": yes_bid, "ticker": ticker}

    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "strike": strike,
        "side": side,
        "entry_price": round(entry, 4),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "market_p": round(market_p, 4),
        "spread": round(yes_ask - yes_bid, 4),
        "our_p": round(our_p, 4),
        "edge": round(edge, 4),                # real edge (vs ask for YES, vs bid for NO)
        "mid_edge": round(mid_edge, 4),         # diagnostic
        "vol24": vol24,
        "oi": oi,
        "hours_to_close": round(htc, 1),
        "vol_oi_quality": "OI" if oi >= MIN_OI else "VOL",
    }


# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------

def open_position(sl: ShadowLedger, sig: dict, nowcast: dict, snap_ts: str) -> dict:
    cost = sig["entry_price"] * CONTRACTS
    md = {
        "engine": ENGINE,
        "event_ticker": sig["event_ticker"],
        "strike": sig["strike"],
        "our_p": sig["our_p"],
        "market_p": sig["market_p"],
        "edge": sig["edge"],
        "yes_bid": sig["yes_bid"],
        "yes_ask": sig["yes_ask"],
        "vol24": sig["vol24"],
        "oi": sig["oi"],
        "hours_to_close": sig["hours_to_close"],
        "nowcast_mu": nowcast["mu"],
        "nowcast_sigma": nowcast["sigma"],
        "nowcast_as_of": nowcast["as_of_date"],
        "nowcast_last_print": nowcast["last_print_icsa"],
        "snap_ts": snap_ts,
    }
    pid = sl.open(
        engine=ENGINE,
        venue="kalshi",
        ticker=sig["ticker"],
        side=sig["side"],
        size=CONTRACTS,
        price=sig["entry_price"],
        signal_metadata=md,
        fee_usd=0.0,
    )
    return {"position_id": pid, "cost_usd": cost}


def trade_once(dry_run: bool) -> dict:
    # Freshness gate (added 2026-05-08 — see portfolio_freshness_watchdog.py).
    # If any data path in any engine is stale, watchdog writes the flag and we
    # force dry-run for this cycle. Auto-clears when data recovers.
    _flag = Path.home() / "Documents" / "freshness_alarm.flag"
    if _flag.exists() and not dry_run:
        try:
            log(f"[freshness_alarm] flag present at {_flag} — forcing dry-run for this cycle")
            log(f"[freshness_alarm] {_flag.read_text().strip()}")
        except OSError:
            log(f"[freshness_alarm] flag present at {_flag} — forcing dry-run for this cycle")
        dry_run = True

    history = load_icsa_history()
    nowcast = compute_nowcast(history)
    if nowcast is None:
        return {"opened": 0, "skipped": 0, "errors": 1}
    log(f"nowcast: μ={nowcast['mu']:,.0f}  σ={nowcast['sigma']:,.0f}  "
        f"as_of={nowcast['as_of_date']}  last_print={nowcast['last_print_icsa']:,}")

    events = fetch_open_events()
    if not events:
        log("no open KXJOBLESSCLAIMS events")
        return {"opened": 0, "skipped": 0, "errors": 0}
    log(f"events: {len(events)}")

    open_positions = find_open_positions()
    open_tickers = {p.get("ticker") for p in open_positions}
    sl = ShadowLedger() if not dry_run else None
    snap_ts = datetime.now(timezone.utc).isoformat()

    opened = 0
    skipped = 0
    candidate_count = 0

    for ev in events:
        et = ev.get("event_ticker")
        if not et:
            continue
        markets = fetch_markets_for_event(et)
        log(f"  event {et}: {len(markets)} markets")

        # Sort markets by edge (best first) so cluster cap selects strongest.
        scored: List[Tuple[dict, dict]] = []
        for m in markets:
            res = evaluate_market(m, et, nowcast)
            if res is None:
                continue
            if "skip" in res:
                skipped += 1
                continue
            scored.append((res, m))
            candidate_count += 1
        scored.sort(key=lambda x: x[0]["edge"], reverse=True)

        for sig, _m in scored:
            if sig["ticker"] in open_tickers:
                continue
            if event_strike_count(open_positions, et) >= CLUSTER_CAP:
                log(f"    [skip cluster cap] {et}")
                break
            cost_so_far = event_cost_so_far(open_positions, et)
            this_cost = sig["entry_price"] * CONTRACTS
            if cost_so_far + this_cost > COST_CAP_PER_EVENT_USD:
                log(f"    [skip cost cap] {et}: would be ${cost_so_far + this_cost:.2f}")
                continue

            # Entropy collapse defensive gate (added 2026-05-09).
            try:
                from entropy_alert_helpers import should_block_for_collapse
                blocked, reason = should_block_for_collapse(
                    ticker=sig["ticker"], proposed_side=sig["side"], engine=ENGINE,
                )
                if blocked:
                    log(f"    [entropy-block] {sig['ticker']} {reason}")
                    skipped += 1
                    continue
            except Exception as e:
                log(f"    [warn] entropy gate failed open: {e}")

            log(f"    [OPEN] {sig['ticker']:<40} {sig['side']:<3} @${sig['entry_price']:.4f}  "
                f"edge=${sig['edge']:.3f}  our_p={sig['our_p']:.3f}  mkt_p={sig['market_p']:.3f}  "
                f"strike={int(sig['strike']):,}  {'DRY-RUN' if dry_run else ''}")

            if not dry_run:
                res = open_position(sl, sig, nowcast, snap_ts)
                # Synthesize a fake "open record" to keep bookkeeping in sync within this loop
                open_positions.append({
                    "ticker": sig["ticker"],
                    "cost_usd": this_cost,
                    "signal_metadata": {"event_ticker": et},
                })
                open_tickers.add(sig["ticker"])
            opened += 1

    log(f"trade_once: candidates={candidate_count} opened={opened} skipped={skipped}")
    return {"opened": opened, "skipped": skipped, "candidates": candidate_count, "errors": 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC)
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    log(f"T3c Paper Trader starting. dry_run={args.dry_run} once={args.once} "
        f"interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            r = trade_once(args.dry_run)
            log(f"loop #{loops}: opened={r['opened']} skipped={r['skipped']} "
                f"candidates={r.get('candidates', 0)} errors={r['errors']}")
        except Exception as e:
            log(f"[error] trade_once: {type(e).__name__}: {e}")
        if args.once or _STOP:
            break
        end = time.time() + args.interval_sec
        while time.time() < end and not _STOP:
            time.sleep(min(1.0, end - time.time()))

    log(f"Trader stopped. Loops: {loops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
