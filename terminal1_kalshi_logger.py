"""Terminal 1 — Kalshi Weather Market Logger.

Purpose: snapshot KXHIGH / KXLOW orderbooks for target stations every 15 min
during market hours. Builds the historical dataset Track B needs to backtest
against real Kalshi prices (Track A uses climatology as the price proxy since
Kalshi doesn't expose deep history).

Output: JSONL files, one per (station, date):
    ~/Documents/terminal1_data/kalshi_{station}_{YYYY-MM-DD}.jsonl

Each line = one snapshot of one market at one timestamp, with book depth.

Usage:
    python3 ~/Documents/terminal1_kalshi_logger.py

Runs in foreground, logs to stdout. Run under nohup or tmux for persistence:
    nohup python3 ~/Documents/terminal1_kalshi_logger.py > ~/Documents/terminal1_logger.out 2>&1 &

No Kalshi auth required — uses public /markets and /orderbook endpoints.
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests


BASE = "https://api.elections.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 1000
SNAPSHOT_INTERVAL_SEC = 15 * 60  # 15 minutes

# --- STATION MAPPING -------------------------------------------------------
# Kalshi weather ticker grammar observed in live data:
#   KX(HIGH|LOW)(T?)<STATION>-<DATE>-<STRIKE>
# Examples:
#   KXHIGHCHI-26APR22-B73.5     (Chicago, no T)
#   KXLOWTNYC-26APR23-T49       (NYC, with T)
#   KXHIGHTNOLA-26APR23-B82.5   (New Orleans, with T)
#
# We map our canonical station codes (NYC/ORD/LAX) to the Kalshi ticker
# codes that appear in the position immediately after HIGH/LOW/HIGHT/LOWT.
STATION_TICKER_CODES: Dict[str, str] = {
    # Tier 1 (confirmed from live data):
    "NYC": "NYC",
    "ORD": "CHI",   # Chicago uses CHI in Kalshi tickers
    "LAX": "LAX",
    # Tier 2 (added 2026-04-24, verified via logger snap #1):
    "DEN": "DEN",
    "ATL": "ATL",
    "MIA": "MIA",
    "PHX": "PHX",
    # DFW dropped 2026-04-24 — Kalshi doesn't currently offer Dallas weather.
}

# Weather ticker prefixes. Longest match first to avoid greedy stripping
# (KXHIGHT must be tested before KXHIGH because KXHIGH is a prefix of KXHIGHT).
WEATHER_PREFIXES: List[str] = ["KXHIGHT", "KXHIGH", "KXLOWT", "KXLOW"]

DATA_DIR = Path.home() / "Documents" / "terminal1_data"
LOG_FILE = Path.home() / "Documents" / "terminal1_logger.log"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _parse_weather_station(ticker: str) -> Optional[str]:
    """Parse a Kalshi weather ticker and return our canonical station code
    (NYC/ORD/LAX) if it's for one of our target stations, else None.

    Strictly positional — the station code must appear immediately after the
    HIGH/LOW/HIGHT/LOWT prefix and be followed by '-'. This prevents false
    positives like KXLOWTNOLA matching LAX via substring 'LA', or
    KXHIGHINFLATION matching LAX because INFLATION contains 'LA'.
    """
    t = ticker.upper()
    rest: Optional[str] = None
    for prefix in WEATHER_PREFIXES:  # longest-first
        if t.startswith(prefix):
            rest = t[len(prefix):]
            break
    if rest is None:
        return None
    # rest should be <STATION>-<DATE>-<STRIKE>
    station_part = rest.split("-", 1)[0]
    for our_code, kalshi_code in STATION_TICKER_CODES.items():
        if station_part == kalshi_code:
            return our_code
    return None


# Days forward to look for events (today + N days).
LOOKAHEAD_DAYS = 5

# Every (HIGH/LOW) × (T/no-T) variant we try per station per day.
# Kalshi inconsistently uses HIGH/HIGHT and LOW/LOWT across stations, so we
# try all 4 variants; non-existent events return empty cleanly.
TICKER_VARIANTS = ["KXHIGH", "KXHIGHT", "KXLOW", "KXLOWT"]

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _kalshi_date_str(d: datetime) -> str:
    """Format: '26APR23' (YY + 3-letter MONTH + DD)."""
    return f"{d.year % 100:02d}{MONTHS[d.month - 1]}{d.day:02d}"


def fetch_weather_markets() -> List[dict]:
    """Fetch weather markets for our target stations by querying directly
    against known event_tickers. Much faster than paginating the full market
    universe (which is 60k+ records dominated by parlays)."""
    out: List[dict] = []
    now = datetime.now(timezone.utc)
    queried = 0
    hits = 0

    for day_offset in range(LOOKAHEAD_DAYS):
        d = now.replace(hour=0, minute=0, second=0, microsecond=0) + \
            timedelta(days=day_offset)
        date_str = _kalshi_date_str(d)

        for station, kalshi_code in STATION_TICKER_CODES.items():
            for variant in TICKER_VARIANTS:
                event_ticker = f"{variant}{kalshi_code}-{date_str}"
                queried += 1
                try:
                    r = requests.get(
                        f"{BASE}/markets",
                        params={
                            "event_ticker": event_ticker,
                            "status": "open",
                            "limit": 100,
                        },
                        timeout=REQUEST_TIMEOUT,
                    )
                    if r.status_code != 200:
                        continue
                    batch = r.json().get("markets", []) or []
                except requests.RequestException as e:
                    log(f"  [warn] event {event_ticker}: {e}")
                    continue

                for m in batch:
                    # Sanity-check: the ticker we got must parse to our station.
                    parsed = _parse_weather_station(m.get("ticker", ""))
                    if parsed != station:
                        # Defensive: skip any unexpected market that slipped in.
                        continue
                    m["_station"] = station
                    out.append(m)
                    hits += 1

    log(f"  queried {queried} event-tickers across "
        f"{LOOKAHEAD_DAYS}d × {len(STATION_TICKER_CODES)} stations × "
        f"{len(TICKER_VARIANTS)} variants → {hits} markets matched")
    return out


def _to_float(v) -> Optional[float]:
    """Kalshi sends fixed-point numeric fields as strings ('0.0400'). Coerce
    to float so the JSONL is numeric-typed per schema; return None on bad
    input so nulls pass through cleanly."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def snapshot_all() -> Dict[str, int]:
    """Pull all target markets, write canonical snapshot records to JSONL.

    Uses the top-of-book fields embedded in /markets responses — no per-market
    orderbook fetch. Book depth can be added later via a separate enrichment
    job if needed; it's not required for Track B's historical pricing data.
    """
    log("  fetching /markets ...")
    t0 = time.time()
    markets = fetch_weather_markets()
    log(f"  /markets done in {time.time()-t0:.1f}s, "
        f"{len(markets)} target markets")

    counts: Dict[str, int] = defaultdict(int)
    ts_utc = datetime.now(timezone.utc).isoformat()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Group by station for output file routing.
    by_station: Dict[str, List[dict]] = defaultdict(list)

    for m in markets:
        station = m["_station"]
        ticker = m.get("ticker", "")
        snap = {
            "_schema_version": "v1",
            "ts_utc": ts_utc,
            "station": station,
            "ticker": ticker,
            "title": m.get("title", ""),
            "event_ticker": m.get("event_ticker", ""),
            "close_time": m.get("close_time", ""),
            "expiration_time": m.get("expiration_time", ""),
            "yes_bid": _to_float(m.get("yes_bid_dollars")),
            "yes_ask": _to_float(m.get("yes_ask_dollars")),
            "no_bid": _to_float(m.get("no_bid_dollars")),
            "no_ask": _to_float(m.get("no_ask_dollars")),
            "last_price": _to_float(m.get("last_price_dollars")),
            "volume": _to_float(m.get("volume_fp")),
            "volume_24h": _to_float(m.get("volume_24h_fp")),
            "open_interest": _to_float(m.get("open_interest_fp")),
            "book": None,  # Reserved — depth enrichment deferred.
        }
        by_station[station].append(snap)
        counts[station] += 1

    for station, snaps in by_station.items():
        path = DATA_DIR / f"kalshi_{station}_{today}.jsonl"
        try:
            with open(path, "a") as f:
                for s in snaps:
                    f.write(json.dumps(s) + "\n")
        except OSError as e:
            log(f"  [error] write failed for {station}: {e}")

    return dict(counts)


def main() -> None:
    log("=" * 80)
    log("TERMINAL 1 — KALSHI WEATHER MARKET LOGGER")
    log("=" * 80)
    log(f"Snapshot interval: {SNAPSHOT_INTERVAL_SEC // 60} minutes")
    log(f"Stations: {', '.join(STATION_TICKER_CODES.keys())}")
    log(f"Data dir: {DATA_DIR}")
    log("")

    iteration = 0
    while True:
        iteration += 1
        t0 = time.time()
        try:
            counts = snapshot_all()
            total = sum(counts.values())
            parts = " / ".join(f"{s}:{n}" for s, n in sorted(counts.items()))
            dt = time.time() - t0
            log(f"snap #{iteration}: total={total} ({parts}) in {dt:.1f}s")
        except Exception as e:
            log(f"  [error] snapshot failed: {e}")

        # Sleep until next scheduled tick. Align to the interval so snapshots
        # happen on the :00, :15, :30, :45 of the hour (approximately).
        elapsed = time.time() - t0
        sleep_for = max(5.0, SNAPSHOT_INTERVAL_SEC - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("logger stopped by user.")
        sys.exit(0)
