"""Terminal 3b — Kalshi CPI Logger.

Snapshots all open KXCPIYOY (Y/Y CPI inflation) events at fixed cadence.
One JSONL row per (snapshot, market). Append-only, daily-rotated files.

Phase 1 scope: KXCPIYOY only (April 2026 print first; the engine
auto-discovers all open events in the series).

Output (per spec §8):
    ~/Documents/terminal3b_data/kalshi_{event_ticker}_{YYYY-MM-DD}.jsonl
    ~/Documents/terminal3b_data/kalshi_logger.log

Usage:
    # Default — snap every 5 min, run until SIGINT:
    python3 ~/Documents/terminal3b_kalshi_logger.py

    # One-shot (for cron or smoke test):
    python3 ~/Documents/terminal3b_kalshi_logger.py --once

    # Daemonize:
    nohup caffeinate -is python3 ~/Documents/terminal3b_kalshi_logger.py \\
        > /dev/null 2>&1 &

No auth required (public Kalshi endpoints).
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests


BASE = "https://api.elections.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 200
SCHEMA_VERSION = "v1"

OUTPUT_DIR = Path.home() / "Documents" / "terminal3b_data"
LOG_PATH = OUTPUT_DIR / "kalshi_logger.log"

SERIES_TICKER = "KXCPIYOY"
DEFAULT_INTERVAL_SEC = 300  # 5 min — matches §6.5 arb scanner cadence

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
    log("SIGINT — finishing current snapshot and exiting.")


# --------------------------------------------------------------------------
# Kalshi API — public, no auth
# --------------------------------------------------------------------------

def fetch_open_events() -> List[dict]:
    """Return all open events in the KXCPIYOY series."""
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
            r = requests.get(f"{BASE}/events", params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log(f"  [error] /events fetch failed: {e}")
            return out
        if r.status_code != 200:
            log(f"  [error] /events returned {r.status_code}")
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
            r = requests.get(f"{BASE}/markets", params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log(f"  [error] /markets fetch failed for {event_ticker}: {e}")
            return out
        if r.status_code != 200:
            log(f"  [error] /markets returned {r.status_code} for {event_ticker}")
            return out
        body = r.json()
        out.extend(body.get("markets", []) or [])
        cursor = body.get("cursor")
        if not cursor:
            break
    return out


# --------------------------------------------------------------------------
# Per-market record schema (canonical, frozen for Phase 1)
# --------------------------------------------------------------------------

def market_record(snap_ts: str, event_ticker: str, market: dict) -> dict:
    """Project the Kalshi /markets row into a stable T3b schema. Only fields
    we actually use downstream are first-class; everything else falls into
    `_raw` so we don't lose data when Kalshi adds new fields.
    """
    return {
        "_schema_version": SCHEMA_VERSION,
        "ts_utc": snap_ts,
        "event_ticker": event_ticker,
        "ticker": market.get("ticker"),
        "title": market.get("title"),
        "subtitle": market.get("subtitle"),
        "yes_sub_title": market.get("yes_sub_title"),
        "no_sub_title": market.get("no_sub_title"),
        "strike_type": market.get("strike_type"),       # "greater" / "less" / "between"
        "floor_strike": market.get("floor_strike"),
        # prices in $ (decimal) — direct from Kalshi
        "yes_bid": _f(market.get("yes_bid_dollars")),
        "yes_ask": _f(market.get("yes_ask_dollars")),
        "no_bid": _f(market.get("no_bid_dollars")),
        "no_ask": _f(market.get("no_ask_dollars")),
        "last_price": _f(market.get("last_price_dollars")),
        # depth/flow
        "yes_bid_size": _f(market.get("yes_bid_size_fp")),
        "yes_ask_size": _f(market.get("yes_ask_size_fp")),
        "no_bid_size": _f(market.get("no_bid_size_fp")),
        "no_ask_size": _f(market.get("no_ask_size_fp")),
        "volume_24h": _f(market.get("volume_24h_fp")),
        "volume_total": _f(market.get("volume_fp")),
        "open_interest": _f(market.get("open_interest_fp")),
        # lifecycle
        "status": market.get("status"),
        "open_time": market.get("open_time"),
        "close_time": market.get("close_time"),
        "expected_expiration_time": market.get("expected_expiration_time"),
        "latest_expiration_time": market.get("latest_expiration_time"),
        "expiration_value": market.get("expiration_value"),
        "result": market.get("result"),
    }


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# I/O — write snapshot rows to per-event-per-day file
# --------------------------------------------------------------------------

def output_path(event_ticker: str, snap_ts: str) -> Path:
    """terminal3b_data/kalshi_{event_ticker}_{YYYY-MM-DD}.jsonl, dated by
    snapshot UTC date (file rotates daily at 00:00 UTC)."""
    date_local = snap_ts[:10]  # YYYY-MM-DD
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"kalshi_{event_ticker}_{date_local}.jsonl"


def write_records(records_by_event: Dict[str, List[dict]]) -> int:
    """Write all records grouped by event. Returns total rows written."""
    total = 0
    for event_ticker, recs in records_by_event.items():
        if not recs:
            continue
        path = output_path(event_ticker, recs[0]["ts_utc"])
        try:
            with open(path, "a") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
            total += len(recs)
        except OSError as e:
            log(f"  [error] write {path}: {e}")
    return total


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def snap_once() -> dict:
    """One snapshot pass: fetch all open KXCPIYOY events + their markets.
    Returns small summary dict for logging."""
    snap_ts = datetime.now(timezone.utc).isoformat()
    events = fetch_open_events()
    if not events:
        log("  no open KXCPIYOY events returned (transient or no markets up)")
        return {"events": 0, "markets": 0}

    by_event: Dict[str, List[dict]] = {}
    market_count = 0
    for ev in events:
        et = ev.get("event_ticker")
        if not et:
            continue
        markets = fetch_markets_for_event(et)
        recs = [market_record(snap_ts, et, m) for m in markets if m.get("ticker")]
        if recs:
            by_event[et] = recs
            market_count += len(recs)

    written = write_records(by_event)
    log(f"snap: events={len(events)} markets={market_count} rows_written={written}")
    return {"events": len(events), "markets": market_count, "rows": written}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="snap once and exit")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC,
                    help="poll interval (default 300s = 5 min)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    log(f"T3b Kalshi Logger starting. series={SERIES_TICKER} "
        f"once={args.once} interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            snap_once()
        except Exception as e:
            log(f"[error] snap_once: {type(e).__name__}: {e}")
        if args.once or _STOP_REQUESTED:
            break
        end = time.time() + args.interval_sec
        while time.time() < end and not _STOP_REQUESTED:
            time.sleep(min(1.0, end - time.time()))

    log(f"Logger stopped. Loops: {loops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
