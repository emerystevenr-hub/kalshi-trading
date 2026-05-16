"""Terminal 7 — NBA/NHL Kalshi REST Logger.

REST-poll snapshot logger for KXNBAGAME + KXNHLGAME events. Mirrors
terminal6_mlb_kalshi_logger.py architecture. The WS feed (terminal7_
kalshi_ws.py) is the primary; this REST logger is a fallback that runs
at lower cadence and produces the same JSONL schema.

T7 deltas vs T6:
  - SERIES_TICKERS = ["KXNBAGAME", "KXNHLGAME"] (vs T6 single KXMLBGAME)
  - Captures event['series_ticker'] for trader Filter 15
  - Output filename keyed on event_ticker (works for any sport prefix)

Output:
    ~/Documents/terminal7_data/kalshi_{event_ticker}_{YYYY-MM-DD}.jsonl

Usage:
    python3 ~/Documents/terminal7_kalshi_logger.py
    python3 ~/Documents/terminal7_kalshi_logger.py --once
"""

from __future__ import annotations

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

OUTPUT_DIR = Path.home() / "Documents" / "terminal7_data"
LOG_PATH = OUTPUT_DIR / "kalshi_logger.log"

SERIES_TICKERS = ["KXNBAGAME", "KXNHLGAME"]   # T7 delta vs T6
DEFAULT_INTERVAL_SEC = 300

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
    log("[signal] stop requested; will exit after current snapshot")


def fetch_open_events_for_series(series_ticker: str) -> List[dict]:
    out: List[dict] = []
    cursor = None
    while True:
        params: Dict[str, object] = {
            "series_ticker": series_ticker,
            "status": "open",
            "limit": PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{BASE}/events", params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log(f"  [error] /events fetch failed for {series_ticker}: {e}")
            return out
        if r.status_code != 200:
            log(f"  [error] /events returned {r.status_code} for {series_ticker}")
            return out
        body = r.json()
        out.extend(body.get("events", []) or [])
        cursor = body.get("cursor")
        if not cursor:
            break
    return out


def fetch_open_events() -> List[dict]:
    out: List[dict] = []
    for st in SERIES_TICKERS:
        out.extend(fetch_open_events_for_series(st))
    return out


def fetch_markets(event_ticker: str) -> List[dict]:
    cursor = None
    out: List[dict] = []
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


def parse_event_ticker(et: str) -> dict:
    """Parse KX{NBA|NHL}GAME-{YY}{MMM}{DD}{AWAY}{HOME} format.
    NBA/NHL playoff tickers carry date-only (7-char), no time — verified
    against live Kalshi probe 2026-05-09. Differs from T6 MLB 11-char
    date+time format."""
    out = {"date": None, "start_time_et": None, "sport": None, "teams_raw": None}
    for sp_ticker, sport in (("KXNBAGAME", "nba"), ("KXNHLGAME", "nhl")):
        if et.startswith(sp_ticker + "-"):
            rest = et[len(sp_ticker) + 1:]
            out["sport"] = sport
            break
    else:
        return out
    if len(rest) < 11:        # 7-char date + minimum 4-char team-pair
        return out
    try:
        yy = rest[0:2]; mmm = rest[2:5]; dd = rest[5:7]
        team_part = rest[7:]   # NBA/NHL: no time field; teams immediately follow date
        out["date"] = f"20{yy}-{mmm.title()}-{dd}"
        out["start_time_et"] = None    # Kalshi doesn't encode time in NBA/NHL tickers
        out["teams_raw"] = team_part
    except (ValueError, IndexError):
        pass
    return out


def parse_levels(levels) -> List[tuple]:
    out = []
    for lvl in levels or []:
        try:
            p = float(lvl[0])
            s = float(lvl[1])
            pc = int(round(p * 100)) if p < 2 else int(round(p))
            if 0 < pc < 100:
                out.append((pc, int(s)))
        except (ValueError, TypeError, IndexError):
            continue
    out.sort()
    return out


def project_market(m: dict, ob: Optional[dict], snap_ts: str,
                   event_meta: dict) -> dict:
    yes_levels_raw = (ob or {}).get("yes_dollars") or (ob or {}).get("yes") or []
    no_levels_raw = (ob or {}).get("no_dollars") or (ob or {}).get("no") or []
    yes = parse_levels(yes_levels_raw)
    no = parse_levels(no_levels_raw)
    yes_top = max(yes, key=lambda x: x[0]) if yes else None
    no_top = max(no, key=lambda x: x[0]) if no else None
    return {
        "_schema_version": SCHEMA_VERSION,
        "snap_ts_utc": snap_ts,
        "ticker": m.get("ticker"),
        "event_ticker": m.get("event_ticker"),
        # T7 delta — series_ticker for Filter 15
        "series_ticker": event_meta.get("series_ticker"),
        "title": m.get("title"),
        "subtitle": m.get("subtitle") or m.get("yes_sub_title"),
        "yes_sub_title": m.get("yes_sub_title"),
        "no_sub_title": m.get("no_sub_title"),
        "status": m.get("status"),
        "close_time": m.get("close_time"),
        "expiration_time": m.get("expiration_time"),
        "result": m.get("result"),
        "open_interest": m.get("open_interest"),
        "volume": m.get("volume"),
        "volume_24h": m.get("volume_24h"),
        "liquidity": m.get("liquidity"),
        "yes_top_price_cents": yes_top[0] if yes_top else None,
        "yes_top_size": yes_top[1] if yes_top else None,
        "no_top_price_cents": no_top[0] if no_top else None,
        "no_top_size": no_top[1] if no_top else None,
        "yes_levels": yes,
        "no_levels": no,
        "spread_cents": (
            100 - yes_top[0] - no_top[0] if (yes_top and no_top) else None
        ),
        "implied_mid_cents": (
            (yes_top[0] + (100 - no_top[0])) / 2.0
            if (yes_top and no_top) else None
        ),
        "event_title": event_meta.get("title"),     # Filter 14 input
        **{f"event_{k}": v for k, v in event_meta.items()
           if k not in ("title", "series_ticker")},
    }


def output_path_for_event(event_ticker: str) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return OUTPUT_DIR / f"kalshi_{event_ticker}_{today}.jsonl"


def snapshot_once() -> None:
    snap_ts = datetime.now(timezone.utc).isoformat()
    events = fetch_open_events()
    if not events:
        log(f"snapshot: 0 open NBA+NHL events")
        return
    total_markets = 0
    total_books = 0
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for ev in events:
        et = ev.get("event_ticker")
        if not et:
            continue
        event_meta = parse_event_ticker(et)
        event_meta["title"] = ev.get("title")
        event_meta["series_ticker"] = ev.get("series_ticker")
        markets = fetch_markets(et)
        if not markets:
            continue
        out_path = output_path_for_event(et)
        with open(out_path, "a") as f:
            for m in markets:
                ob = fetch_orderbook(m.get("ticker"))
                row = project_market(m, ob, snap_ts, event_meta)
                f.write(json.dumps(row) + "\n")
                total_markets += 1
                if ob:
                    total_books += 1
    log(f"snapshot: {len(events)} events, {total_markets} markets, "
        f"{total_books} orderbooks")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Run a single snapshot and exit.")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC,
                    help="Snapshot cadence in seconds (default 300).")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"T7 NBA/NHL Kalshi REST logger starting; series={SERIES_TICKERS} "
        f"interval={args.interval_sec}s once={args.once}")

    if args.once:
        snapshot_once()
        return 0

    while not _STOP_REQUESTED:
        try:
            snapshot_once()
        except Exception as e:
            log(f"  [error] snapshot raised: {e}")
        slept = 0
        while slept < args.interval_sec and not _STOP_REQUESTED:
            time.sleep(1)
            slept += 1

    log("T7 NBA/NHL Kalshi REST logger stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
