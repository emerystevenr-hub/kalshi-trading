"""Terminal 3a — Kalshi Fed Scanner.

DATA CAPTURE ONLY. No execution, no CME integration, no executor reuse.

Purpose: snapshot Kalshi KXFEDDECISION and KXFED markets at a fixed interval.
Compute per-event sum-of-YES-bids and sum-of-YES-asks, flag Dutch-arb conditions
and near-misses. All outputs append-only JSONL.

Output files (per-run-day rotation):
    ~/Documents/terminal3a_data/fed_scanner_markets_{YYYY-MM-DD}.jsonl
        one row per (snapshot, market) — full orderbook summary
    ~/Documents/terminal3a_data/fed_scanner_events_{YYYY-MM-DD}.jsonl
        one row per (snapshot, event) — sum metrics + arb flags
    ~/Documents/terminal3a_data/fed_scanner_alerts.jsonl
        append-only, cross-day — only events flagged DUTCH_ARB_LONG, DUTCH_ARB_SHORT,
        or NEAR_MISS (within --near-miss-threshold cents)

Usage:
    # Default — snapshot every 60 sec, run until SIGINT:
    python3 terminal3a_fed_scanner.py

    # Snap once and exit (for cron/smoke-test):
    python3 terminal3a_fed_scanner.py --once

    # Slower cadence (if you're only watching far-dated events):
    python3 terminal3a_fed_scanner.py --interval-sec 300 --near-miss-threshold 3

Run under caffeinate + nohup for persistence:
    nohup caffeinate -is python3 ~/Documents/terminal3a_fed_scanner.py \\
        > ~/Documents/terminal3a_scanner.log 2>&1 &

No auth required — uses the same public /events /markets /orderbook endpoints
as the weather logger and macro depth snapshot.
"""

import argparse
import json
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests


BASE = "https://api.elections.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 200
SCHEMA_VERSION = "v1"

OUTPUT_DIR = Path.home() / "Documents" / "terminal3a_data"
ALERTS_PATH = OUTPUT_DIR / "fed_scanner_alerts.jsonl"
LOG_PATH = OUTPUT_DIR / "fed_scanner.log"

SERIES_TO_SCAN = ["KXFEDDECISION", "KXFED"]

# Dutch arb math only applies to EXCLUSIVE series (exactly one market settles
# to YES — e.g. KXFEDDECISION's H0/H25/H26/C25/C26). For ORDINAL series like
# KXFED ("rate will be at least X"), summing YES probabilities across markets
# is meaningless because the markets aren't mutually exclusive.
#
# We still log aggregates for ordinal series (for later analysis of
# monotonicity arbs, adjacency arbs), but DO NOT fire DUTCH_ARB flags on them.
SERIES_EXCLUSIVE = {
    "KXFEDDECISION": True,
    "KXFED": False,   # ordinal, "rate ≥ X" structure
}

# Dutch arb fires when sum-of-YES-asks < 100 (long arb) or sum-of-YES-bids > 100
# (short arb). Fees not deducted in these raw flags — the alert consumer does
# the fee/threshold decision.
ARB_LONG_THRESHOLD = 100    # ΣYES_ask < this → flag
ARB_SHORT_THRESHOLD = 100   # ΣYES_bid > this → flag

_STOP_REQUESTED = False


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _handle_sigint(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    log("SIGINT received — finishing current snapshot and exiting.")


# --------------------------------------------------------------------------
# Kalshi API helpers — public endpoints, no auth
# --------------------------------------------------------------------------

def fetch_events(series_ticker: str, status: str) -> List[dict]:
    """Paginate all events in a series with the given status."""
    out: List[dict] = []
    cursor = None
    while True:
        params: Dict[str, object] = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{BASE}/events", params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            return out
        if r.status_code != 200:
            return out
        body = r.json()
        out.extend(body.get("events", []) or [])
        cursor = body.get("cursor")
        if not cursor:
            break
    return out


def fetch_markets_for_event(event_ticker: str) -> List[dict]:
    try:
        r = requests.get(
            f"{BASE}/markets",
            params={"event_ticker": event_ticker, "limit": PAGE_LIMIT},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return r.json().get("markets", []) or []


def fetch_orderbook(ticker: str) -> Optional[dict]:
    """Return dict with keys yes_dollars, no_dollars or None on failure.
    Real Kalshi schema: {"orderbook_fp": {"yes_dollars": [[price_str, size_str], ...], "no_dollars": [...]}}.
    """
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


# --------------------------------------------------------------------------
# Orderbook parsing
# --------------------------------------------------------------------------

def _levels_to_cents(levels):
    """Parse [[price_str, size_str], ...] → sorted list[(price_cents_int, size_int)]."""
    out = []
    for lvl in levels or []:
        try:
            p_raw, s_raw = lvl[0], lvl[1]
            p = float(p_raw)
            s = float(s_raw)
            price_cents = int(round(p * 100)) if p < 2 else int(round(p))
            size = int(round(s))
            if 0 < price_cents < 100:
                out.append((price_cents, size))
        except (ValueError, TypeError, IndexError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def summarize_orderbook(ob: Optional[dict]) -> dict:
    """Compute per-market orderbook metrics. All prices in integer cents,
    all sizes in contract counts.

    We intentionally do NOT filter out the 1–5¢ tail here — for the arb
    aggregation, tail prices ARE legitimate best quotes on extreme outcomes
    (e.g. "Hike >25bp" on a near-term meeting). Filtering happens in the
    consumer if needed.
    """
    out = {
        "yes_bid": None,      "yes_bid_size": 0,
        "no_bid": None,       "no_bid_size": 0,
        "yes_ask": None,      "yes_ask_size": 0,   # derived = 100 - no_bid
        "yes_depth_5c": 0,    "no_depth_5c": 0,
        "yes_total": 0,       "no_total": 0,
        "yes_levels": 0,      "no_levels": 0,
        "implied_mid": None,
        "depth_source": "none",
    }

    if not ob:
        return out

    yes = _levels_to_cents(ob.get("yes_dollars") or ob.get("yes") or [])
    no = _levels_to_cents(ob.get("no_dollars") or ob.get("no") or [])

    if yes or no:
        out["depth_source"] = "orderbook"

    if yes:
        best = max(yes, key=lambda x: x[0])
        out["yes_bid"], out["yes_bid_size"] = best
        out["yes_depth_5c"] = sum(s for (p, s) in yes if abs(p - best[0]) <= 5)
        out["yes_total"] = sum(s for _, s in yes)
        out["yes_levels"] = len(yes)
    if no:
        best = max(no, key=lambda x: x[0])
        out["no_bid"], out["no_bid_size"] = best
        out["no_depth_5c"] = sum(s for (p, s) in no if abs(p - best[0]) <= 5)
        out["no_total"] = sum(s for _, s in no)
        out["no_levels"] = len(no)
        # YES ask = implied from best NO bid: yes_ask_price = 100 - no_bid_price.
        # Size you can BUY YES at that ask = size of the NO bid (those sellers
        # of NO are effectively offering YES to buy).
        out["yes_ask"] = 100 - out["no_bid"]
        out["yes_ask_size"] = out["no_bid_size"]

    if out["yes_bid"] is not None and out["yes_ask"] is not None:
        out["implied_mid"] = (out["yes_bid"] + out["yes_ask"]) / 2.0

    return out


# --------------------------------------------------------------------------
# Event aggregation + arb detection
# --------------------------------------------------------------------------

def aggregate_event(event_row: dict, markets_with_depth: List[dict],
                    near_miss_threshold: int) -> dict:
    """Given all markets in an event with their orderbook summaries,
    compute ΣYES_bid, ΣYES_ask, and arb flags."""
    yes_bids, yes_asks, yes_ask_sizes, no_bid_sizes = [], [], [], []
    incomplete = 0

    for m in markets_with_depth:
        yb = m.get("yes_bid")
        ya = m.get("yes_ask")
        ys = m.get("yes_ask_size") or 0
        if yb is None and ya is None:
            incomplete += 1
            continue
        # If YES bid is missing → treat as 0 (can't sell YES, but for sum-of-bids
        # we still count 0). If YES ask missing → treat as 100 (can't buy YES).
        yes_bids.append(yb if yb is not None else 0)
        yes_asks.append(ya if ya is not None else 100)
        yes_ask_sizes.append(ys)

    n_markets = len(markets_with_depth)
    complete = len(yes_bids)

    sum_bid = sum(yes_bids)
    sum_ask = sum(yes_asks)

    # Arb capacity = smallest YES-ask size across markets (you can only buy as
    # much as the smallest leg allows; everything else is excess).
    min_capacity = min(yes_ask_sizes) if yes_ask_sizes and complete == n_markets else 0

    flags = []
    series_ticker = event_row.get("series_ticker")
    is_exclusive = SERIES_EXCLUSIVE.get(series_ticker, False)

    # Σ=0 noise filter (added 2026-04-25): if EVERY leg in the event has no
    # ask or no bid, the sum is 0 — looks like a Dutch arb mathematically
    # (0 < 100) but is just bad data state. Don't flag.
    # NB: yes_ask/yes_bid keys exist but VALUE may be None — coerce with `or 0`.
    sum_ask_is_real = sum_ask > 0 and any(
        (m.get("yes_ask") or 0) > 0 for m in markets_with_depth
    )
    sum_bid_is_real = sum_bid > 0 and any(
        (m.get("yes_bid") or 0) > 0 for m in markets_with_depth
    )

    # Dutch arb flags only meaningful for exclusive series. Ordinal series
    # get aggregate metrics logged but no flags (we'll add monotonicity-arb
    # detection for ordinal series later as a separate flag class).
    if is_exclusive and complete == n_markets:
        if sum_ask < ARB_LONG_THRESHOLD and sum_ask_is_real:
            flags.append("DUTCH_ARB_LONG")
        if sum_bid > ARB_SHORT_THRESHOLD and sum_bid_is_real:
            flags.append("DUTCH_ARB_SHORT")
        if not flags:
            # Near-miss: how many cents away from an arb threshold
            if sum_ask_is_real and \
                    (ARB_LONG_THRESHOLD - sum_ask) >= -near_miss_threshold and \
                    (ARB_LONG_THRESHOLD - sum_ask) <= near_miss_threshold:
                flags.append("NEAR_MISS_LONG")
            if sum_bid_is_real and \
                    (sum_bid - ARB_SHORT_THRESHOLD) >= -near_miss_threshold and \
                    (sum_bid - ARB_SHORT_THRESHOLD) <= near_miss_threshold:
                flags.append("NEAR_MISS_SHORT")

    return {
        "event_ticker": event_row.get("event_ticker"),
        "event_title": event_row.get("title"),
        "series_ticker": series_ticker,
        "is_exclusive": is_exclusive,
        "n_markets": n_markets,
        "n_complete_quotes": complete,
        "sum_yes_bid": sum_bid,
        "sum_yes_ask": sum_ask,
        "long_arb_edge_cents": ARB_LONG_THRESHOLD - sum_ask,
        "short_arb_edge_cents": sum_bid - ARB_SHORT_THRESHOLD,
        "min_long_arb_capacity_contracts": min_capacity,
        "flags": flags,
    }


# --------------------------------------------------------------------------
# Main snapshot loop
# --------------------------------------------------------------------------

def snapshot(series_list: List[str], near_miss_threshold: int) -> dict:
    """Run one full snapshot. Returns summary stats."""
    ts_utc = datetime.now(timezone.utc)
    snap_id = ts_utc.strftime("%Y-%m-%dT%H-%M-%SZ")
    date_str = ts_utc.strftime("%Y-%m-%d")
    markets_path = OUTPUT_DIR / f"fed_scanner_markets_{date_str}.jsonl"
    events_path = OUTPUT_DIR / f"fed_scanner_events_{date_str}.jsonl"

    n_events = 0
    n_markets = 0
    n_alerts = 0
    alerts: List[dict] = []

    # Context managers prevent partial-line corruption if an exception fires
    # mid-write. Both files are append-only — concurrent snapshots from a
    # restart would still interleave cleanly at line boundaries.
    with open(markets_path, "a") as mkt_f, open(events_path, "a") as ev_f:
        for series in series_list:
            events = fetch_events(series, "open") + fetch_events(series, "initialized")
            for ev in events:
                ev_ticker = ev.get("event_ticker")
                ev_title = ev.get("title", "")
                markets = fetch_markets_for_event(ev_ticker)
                per_market_summaries = []
                for m in markets:
                    m_ticker = m.get("ticker")
                    ob = fetch_orderbook(m_ticker)
                    depth = summarize_orderbook(ob)
                    per_market_summaries.append(depth)

                    row = {
                        "_schema_version": SCHEMA_VERSION,
                        "row_type": "market",
                        "snapshot_ts_utc": ts_utc.isoformat(),
                        "snapshot_id": snap_id,
                        "series_ticker": series,
                        "event_ticker": ev_ticker,
                        "event_title": ev_title,
                        "market_ticker": m_ticker,
                        "market_subtitle": m.get("subtitle") or m.get("yes_sub_title"),
                        "close_time": m.get("close_time"),
                        "expiration_time": m.get("expiration_time"),
                        "volume_total": m.get("volume", 0),
                        "volume_24h": m.get("volume_24h", 0),
                        "open_interest": m.get("open_interest", 0),
                        **depth,
                    }
                    mkt_f.write(json.dumps(row) + "\n")
                    n_markets += 1

                ev_agg = aggregate_event(
                    {"event_ticker": ev_ticker, "title": ev_title, "series_ticker": series},
                    per_market_summaries,
                    near_miss_threshold,
                )
                event_row = {
                    "_schema_version": SCHEMA_VERSION,
                    "row_type": "event_aggregate",
                    "snapshot_ts_utc": ts_utc.isoformat(),
                    "snapshot_id": snap_id,
                    **ev_agg,
                }
                ev_f.write(json.dumps(event_row) + "\n")
                n_events += 1

                if ev_agg["flags"]:
                    alerts.append(event_row)

            mkt_f.flush()
            ev_f.flush()

    # Write alerts (append-only, cross-day file)
    if alerts:
        with open(ALERTS_PATH, "a") as f:
            for a in alerts:
                f.write(json.dumps(a) + "\n")
        n_alerts = len(alerts)

    return {
        "snap_id": snap_id,
        "n_events": n_events,
        "n_markets": n_markets,
        "n_alerts": n_alerts,
        "alerts": alerts,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-sec", type=int, default=60,
                    help="seconds between snapshots (default 60). If a snapshot "
                         "takes longer than this, the next runs back-to-back — "
                         "fine for arb hunting.")
    ap.add_argument("--once", action="store_true",
                    help="snap once and exit")
    ap.add_argument("--series", default=",".join(SERIES_TO_SCAN),
                    help="comma-separated series tickers to scan")
    ap.add_argument("--near-miss-threshold", type=int, default=5,
                    help="flag events within N cents of arb threshold (default 5)")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    series_list = [s.strip() for s in args.series.split(",") if s.strip()]
    log(f"Terminal 3a Fed Scanner starting. series={series_list} "
        f"interval_sec={args.interval_sec} near_miss={args.near_miss_threshold}¢")

    snap_count = 0
    while True:
        if _STOP_REQUESTED:
            break
        t0 = time.time()
        try:
            summary = snapshot(series_list, args.near_miss_threshold)
        except Exception as e:
            log(f"[error] snapshot failed: {type(e).__name__}: {e}")
            summary = None

        dt = time.time() - t0
        snap_count += 1
        if summary:
            alert_flags = ",".join(
                sorted({f for a in summary["alerts"] for f in a["flags"]})
            ) or "none"
            log(f"snap #{snap_count} [{summary['snap_id']}] "
                f"events={summary['n_events']} markets={summary['n_markets']} "
                f"alerts={summary['n_alerts']} ({alert_flags}) in {dt:.1f}s")
            for a in summary["alerts"][:5]:  # first 5 alerts inline to the log
                log(f"  ALERT {a['event_ticker']:<25} flags={a['flags']} "
                    f"Σask={a['sum_yes_ask']}¢  Σbid={a['sum_yes_bid']}¢  "
                    f"min_capacity={a['min_long_arb_capacity_contracts']}")

        if args.once or _STOP_REQUESTED:
            break

        # Sleep until next interval (sleep in 1s chunks so SIGINT wakes us)
        elapsed = time.time() - t0
        to_sleep = max(0.0, args.interval_sec - elapsed)
        end = time.time() + to_sleep
        while time.time() < end and not _STOP_REQUESTED:
            time.sleep(min(1.0, end - time.time()))

    log(f"Scanner stopped. Total snapshots: {snap_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
