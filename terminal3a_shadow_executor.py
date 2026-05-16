"""Terminal 3a — Shadow Executor.

Watches T3a fed_scanner_alerts.jsonl. When a DUTCH_ARB flag fires:
  1. Re-fetch live orderbook for every leg of the flagged event (prices
     move fast — the scanner's snapshot may be stale by seconds).
  2. Compute expected profit AFTER Kalshi fees using the actual per-trade
     fee formula. Reject if net EV <= min_profit_usd threshold.
  3. If profitable, open N-leg shadow positions (one per market) all
     tagged with a shared arb_group_id so we can track the cluster.

Fee model (Kalshi published formula, per trade):
    fee_usd = max(0.01, round(0.07 × contracts × price × (1 - price), 2))

Dedupe: a given event_ticker + flag combo is processed once per life of
the state file. (If a flag fires, then un-fires, then fires again, we
only take the first.) Clear state to re-process by deleting
executor_state.json.

Close/settlement is NOT automated in v1. Positions will show open with
mark-to-market unrealized P&L in the dashboard until a settlement
reconciler runs. Build that next if this v1 actually fires.

Usage:
    # Smoke test — dry run, logs what would happen, opens nothing:
    python3 ~/Documents/terminal3a_shadow_executor.py --dry-run --once

    # Live, daemonized:
    nohup caffeinate -is python3 ~/Documents/terminal3a_shadow_executor.py \\
        > /dev/null 2>&1 &

    # Kill:
    pkill -f 'terminal3a_shadow_executor'

Reads ~/Documents/terminal3a_data/fed_scanner_alerts.jsonl
Reads/writes ~/Documents/terminal3a_data/executor_state.json
Writes to unified shadow ledger via shadow_pnl_core.ShadowLedger.
"""

import argparse
import json
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

# Import the shared shadow ledger
sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger  # noqa: E402


BASE = "https://api.elections.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT = 15

DATA_DIR = Path.home() / "Documents" / "terminal3a_data"
ALERTS_PATH = DATA_DIR / "fed_scanner_alerts.jsonl"
STATE_PATH = DATA_DIR / "executor_state.json"
LOG_PATH = DATA_DIR / "executor.log"

ENGINE = "T3a"

# Only these series have the mutually-exclusive structure that Dutch arb math
# requires (exactly one market settles YES per event). Ordinal series like
# KXFED ("rate ≥ X") break the payout formula — Dutch arb doesn't apply.
# Matches SERIES_EXCLUSIVE in terminal3a_fed_scanner.py.
EXCLUSIVE_SERIES_WHITELIST = {"KXFEDDECISION"}

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
    log("SIGINT — finishing current iteration and exiting.")


# -----------------------------------------------------------------------
# Kalshi live fetchers
# -----------------------------------------------------------------------

def fetch_markets_for_event(event_ticker: str) -> List[dict]:
    try:
        r = requests.get(
            f"{BASE}/markets",
            params={"event_ticker": event_ticker, "limit": 50},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return r.json().get("markets", []) or []


def fetch_orderbook(ticker: str) -> Optional[dict]:
    try:
        r = requests.get(
            f"{BASE}/markets/{ticker}/orderbook",
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    body = r.json()
    return body.get("orderbook_fp") or body.get("orderbook")


def _best_price(levels) -> Optional[tuple]:
    """Return (price_dollars, size_int) of the highest-price level, or None.
    levels format: [[price_str, size_str], ...]. Price may be in dollars
    (e.g. '0.05') or cents — normalize to dollars.
    """
    best = None
    for lvl in levels or []:
        try:
            p_raw = float(lvl[0])
            s_raw = int(float(lvl[1]))
            price = p_raw if p_raw < 2 else p_raw / 100.0
            if 0 < price < 1:
                if best is None or price > best[0]:
                    best = (price, s_raw)
        except (ValueError, TypeError, IndexError):
            continue
    return best


def snapshot_event_legs(event_ticker: str) -> List[dict]:
    """Fetch current live orderbook for every market in the event.
    Returns list of dicts: {ticker, subtitle, yes_bid, yes_bid_size,
    no_bid, no_bid_size, yes_ask, yes_ask_size}.
    """
    markets = fetch_markets_for_event(event_ticker)
    out = []
    for m in markets:
        ob = fetch_orderbook(m["ticker"])
        if ob is None:
            continue
        yes_levels = ob.get("yes_dollars") or ob.get("yes") or []
        no_levels = ob.get("no_dollars") or ob.get("no") or []
        yes_best = _best_price(yes_levels)
        no_best = _best_price(no_levels)
        out.append({
            "ticker": m["ticker"],
            "subtitle": m.get("subtitle") or m.get("yes_sub_title"),
            "yes_bid": yes_best[0] if yes_best else 0.0,
            "yes_bid_size": yes_best[1] if yes_best else 0,
            "no_bid": no_best[0] if no_best else 0.0,
            "no_bid_size": no_best[1] if no_best else 0,
            # Derived ask = 1 - best NO bid, size = NO bid size (what you can
            # take against the NO seller to acquire YES)
            "yes_ask": (1.0 - no_best[0]) if no_best else 0.0,
            "yes_ask_size": no_best[1] if no_best else 0,
        })
    return out


# -----------------------------------------------------------------------
# Kalshi fee model
# -----------------------------------------------------------------------

def kalshi_fee(contracts: int, price: float) -> float:
    """Kalshi published fee formula:
       fee = max(0.01, round(0.07 × contracts × price × (1 - price), 2))
    """
    raw = 0.07 * contracts * price * (1.0 - price)
    rounded = round(raw + 0.005, 2)  # round half up to cents
    return max(0.01, rounded)


# -----------------------------------------------------------------------
# Dutch arb evaluator
# -----------------------------------------------------------------------

def evaluate_arb(flags: List[str], legs: List[dict],
                 contracts_per_leg: int) -> Optional[dict]:
    """Given flags and live leg prices, compute expected profit after fees.

    Returns None if not tradable. Otherwise dict:
        {side, contracts_per_leg, trades, total_cost, total_payout,
         total_fees, net_profit, arb_group_id}

    For DUTCH_ARB_LONG: buy YES on all legs at current yes_ask.
      - cost = Σ(yes_ask × contracts)
      - payout at settle = contracts (one leg resolves YES=$1)
      - profit_pre_fees = contracts - cost
    For DUTCH_ARB_SHORT: buy NO on all legs at current no_ask (= 1 - yes_bid).
      - cost = Σ(no_ask × contracts)
      - payout at settle = (n-1) × contracts (n-1 NOs resolve true)
      - profit_pre_fees = (n-1)×contracts - cost
    """
    n = len(legs)
    if n == 0:
        return None

    if "DUTCH_ARB_LONG" in flags:
        if not all(leg["yes_ask"] > 0 for leg in legs):
            return None
        trades = []
        total_cost = 0.0
        total_fees = 0.0
        for leg in legs:
            price = leg["yes_ask"]
            avail_size = leg["yes_ask_size"]
            if avail_size < contracts_per_leg:
                return None  # can't fill the desired size
            cost = price * contracts_per_leg
            fee = kalshi_fee(contracts_per_leg, price)
            trades.append({
                "ticker": leg["ticker"],
                "subtitle": leg["subtitle"],
                "side": "YES",
                "price": price,
                "size": contracts_per_leg,
                "cost_usd": round(cost, 2),
                "fee_usd": fee,
            })
            total_cost += cost
            total_fees += fee
        payout_at_settle = float(contracts_per_leg)  # exactly one leg pays
        net = payout_at_settle - total_cost - total_fees
        return {
            "side": "long",
            "flag": "DUTCH_ARB_LONG",
            "contracts_per_leg": contracts_per_leg,
            "trades": trades,
            "total_cost": round(total_cost, 2),
            "total_fees": round(total_fees, 2),
            "total_payout_at_settle": round(payout_at_settle, 2),
            "net_profit_expected": round(net, 2),
            "sum_yes_ask_cents": round(sum(leg["yes_ask"] for leg in legs) * 100, 1),
        }

    if "DUTCH_ARB_SHORT" in flags:
        if not all(leg["yes_bid"] > 0 for leg in legs):
            return None
        trades = []
        total_cost = 0.0
        total_fees = 0.0
        for leg in legs:
            no_ask = 1.0 - leg["yes_bid"]
            avail_size = leg["yes_bid_size"]
            if avail_size < contracts_per_leg:
                return None
            cost = no_ask * contracts_per_leg
            fee = kalshi_fee(contracts_per_leg, no_ask)
            trades.append({
                "ticker": leg["ticker"],
                "subtitle": leg["subtitle"],
                "side": "NO",
                "price": no_ask,
                "size": contracts_per_leg,
                "cost_usd": round(cost, 2),
                "fee_usd": fee,
            })
            total_cost += cost
            total_fees += fee
        payout_at_settle = float((n - 1) * contracts_per_leg)
        net = payout_at_settle - total_cost - total_fees
        return {
            "side": "short",
            "flag": "DUTCH_ARB_SHORT",
            "contracts_per_leg": contracts_per_leg,
            "trades": trades,
            "total_cost": round(total_cost, 2),
            "total_fees": round(total_fees, 2),
            "total_payout_at_settle": round(payout_at_settle, 2),
            "net_profit_expected": round(net, 2),
            "sum_yes_bid_cents": round(sum(leg["yes_bid"] for leg in legs) * 100, 1),
        }

    return None


# -----------------------------------------------------------------------
# State management
# -----------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_alert_line": 0, "processed": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def new_alerts_since(last_line: int) -> List[tuple]:
    """Return (line_number, alert_row) tuples for alerts beyond last_line."""
    out = []
    if not ALERTS_PATH.exists():
        return out
    try:
        with open(ALERTS_PATH) as f:
            for i, line in enumerate(f, start=1):
                if i <= last_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append((i, json.loads(line)))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


# -----------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------

def process_once(dry_run: bool, min_profit_usd: float,
                 contracts_per_leg: int, state: dict) -> dict:
    """Process new alerts once. Returns updated state."""
    alerts = new_alerts_since(state["last_alert_line"])
    if not alerts:
        return state

    sl = ShadowLedger() if not dry_run else None

    for line_no, a in alerts:
        state["last_alert_line"] = line_no

        flags = a.get("flags", [])
        # Only care about actual arb flags — skip NEAR_MISS entirely
        arb_flag = None
        for f in flags:
            if f in ("DUTCH_ARB_LONG", "DUTCH_ARB_SHORT"):
                arb_flag = f
                break
        if arb_flag is None:
            continue

        event_ticker = a.get("event_ticker")
        if not event_ticker:
            continue

        # Hard filter: Dutch arb math only valid for exclusive series.
        # Pre-patch alerts may have missing/false is_exclusive — always
        # double-check against the whitelist. Ordinal series like KXFED
        # can produce apparent "arbs" that are mathematically invalid.
        series = a.get("series_ticker", "")
        is_exclusive = a.get("is_exclusive")
        if series not in EXCLUSIVE_SERIES_WHITELIST:
            # Only log once per unrecognized series to avoid spam
            key_skip = f"{event_ticker}|{arb_flag}"
            if key_skip not in state["processed"]:
                log(f"  [skip] {event_ticker}  series={series}  "
                    f"not in exclusive whitelist — ordinal math invalid for Dutch arb")
                state["processed"][key_skip] = {
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    "action": "skip_non_exclusive_series",
                    "series": series,
                }
            continue
        if is_exclusive is False:  # explicit False from post-patch scanner
            continue

        # Dedupe: have we already acted on this (event, flag)?
        key = f"{event_ticker}|{arb_flag}"
        if key in state["processed"]:
            log(f"  [skip] already processed {key}")
            continue

        log(f"  [evaluate] {event_ticker}  flag={arb_flag}  "
            f"(scanner snap: Σask={a.get('sum_yes_ask')}, Σbid={a.get('sum_yes_bid')})")

        # Re-fetch live — prices move
        legs = snapshot_event_legs(event_ticker)
        if not legs:
            log(f"    [skip] no live legs returned for {event_ticker}")
            continue

        result = evaluate_arb([arb_flag], legs, contracts_per_leg)
        if result is None:
            log(f"    [skip] arb evaluation None — price / size gap")
            state["processed"][key] = {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "action": "skip_no_arb",
                "reason": "evaluate_arb returned None",
            }
            continue

        log(f"    [eval] {result['side']}  "
            f"cost=${result['total_cost']}  "
            f"payout=${result['total_payout_at_settle']}  "
            f"fees=${result['total_fees']}  "
            f"net_profit_expected=${result['net_profit_expected']}")

        if result["net_profit_expected"] < min_profit_usd:
            log(f"    [skip] net profit ${result['net_profit_expected']} "
                f"below threshold ${min_profit_usd}")
            state["processed"][key] = {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "action": "skip_below_threshold",
                "net_profit_expected": result["net_profit_expected"],
            }
            continue

        # FIRE — open shadow positions
        arb_group_id = uuid.uuid4().hex[:12]
        log(f"    [FIRE] arb_group_id={arb_group_id}  "
            f"{'DRY-RUN — no positions opened' if dry_run else 'opening shadow positions...'}")

        opened_ids = []
        for trade in result["trades"]:
            log(f"      [leg] {trade['ticker']:<40} {trade['side']} "
                f"{trade['size']}@${trade['price']:.4f}  "
                f"cost=${trade['cost_usd']}  fee=${trade['fee_usd']}")
            if not dry_run:
                pid = sl.open(
                    engine=ENGINE,
                    venue="kalshi",
                    ticker=trade["ticker"],
                    side=trade["side"],
                    price=trade["price"],
                    size=trade["size"],
                    fee_usd=trade["fee_usd"],
                    reason=(
                        f"T3a Dutch arb ({result['flag']}) on {event_ticker}. "
                        f"Net EV expected ${result['net_profit_expected']}."
                    ),
                    signal_metadata={
                        "arb_group_id": arb_group_id,
                        "event_ticker": event_ticker,
                        "flag": result["flag"],
                        "sum_yes_ask_cents": result.get("sum_yes_ask_cents"),
                        "sum_yes_bid_cents": result.get("sum_yes_bid_cents"),
                        "leg_subtitle": trade["subtitle"],
                        "scanner_alert_ts": a.get("snapshot_ts_utc"),
                    },
                )
                opened_ids.append(pid)

        state["processed"][key] = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "action": "opened" if not dry_run else "dry_run_would_open",
            "arb_group_id": arb_group_id,
            "net_profit_expected": result["net_profit_expected"],
            "position_ids": opened_ids,
        }

    return state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Log what would happen, open nothing.")
    ap.add_argument("--once", action="store_true",
                    help="Process available alerts once, then exit.")
    ap.add_argument("--poll-sec", type=int, default=30,
                    help="Seconds between alert checks in daemon mode (default 30).")
    ap.add_argument("--contracts-per-leg", type=int, default=10,
                    help="Contracts to buy per leg of the arb (default 10).")
    ap.add_argument("--min-profit-usd", type=float, default=0.05,
                    help="Only fire if expected net profit >= this USD (default 0.05).")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    log(f"T3a Shadow Executor starting. "
        f"dry_run={args.dry_run}  once={args.once}  "
        f"contracts_per_leg={args.contracts_per_leg}  "
        f"min_profit_usd={args.min_profit_usd}  poll={args.poll_sec}s")

    state = load_state()
    log(f"Loaded state: last_alert_line={state['last_alert_line']}  "
        f"previously_processed={len(state['processed'])}")

    loops = 0
    while True:
        loops += 1
        try:
            state = process_once(
                dry_run=args.dry_run,
                min_profit_usd=args.min_profit_usd,
                contracts_per_leg=args.contracts_per_leg,
                state=state,
            )
            save_state(state)
        except Exception as e:
            log(f"[error] process_once: {type(e).__name__}: {e}")

        if args.once or _STOP:
            break

        end = time.time() + args.poll_sec
        while time.time() < end and not _STOP:
            time.sleep(min(1.0, end - time.time()))

    log(f"Executor stopped. Loops: {loops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
