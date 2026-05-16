"""Terminal 6 — Kalshi MLB Game Logger.

Snapshots all open KXMLBGAME events at fixed cadence. Each event has 2
markets (one per team). One JSONL row per (snapshot, market). Append-only,
daily-rotated files keyed by event_ticker.

Mirrors terminal3c_kalshi_logger.py architecture.

Output:
    ~/Documents/terminal6_data/kalshi_{event_ticker}_{YYYY-MM-DD}.jsonl

Usage:
    python3 ~/Documents/terminal6_mlb_kalshi_logger.py
    python3 ~/Documents/terminal6_mlb_kalshi_logger.py --once
    nohup caffeinate -is python3 ~/Documents/terminal6_mlb_kalshi_logger.py \\
        > ~/Documents/terminal6_data/kalshi_logger.out 2>&1 &

No auth required (public Kalshi endpoints).
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

OUTPUT_DIR = Path.home() / "Documents" / "terminal6_data"
LOG_PATH = OUTPUT_DIR / "kalshi_logger.log"

SERIES_TICKER = "KXMLBGAME"
DEFAULT_INTERVAL_SEC = 300  # 5-min cadence — same as T3c

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


def fetch_open_events() -> List[dict]:
    out: List[dict] = []
    cursor = None
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
    """KXMLBGAME-26MAY101920DETKC → date=2026-05-10, time=19:20, away=DET, home=KC.

    Format: KXMLBGAME-{YY}{MMM}{DD}{HHMM}{AWAY}{HOME}
    Team abbreviations are 2-3 letters; total team-pair string varies 4-6 chars.
    """
    out = {"date": None, "start_time_et": None, "away": None, "home": None}
    if not et.startswith(f"{SERIES_TICKER}-"):
        return out
    rest = et[len(SERIES_TICKER) + 1:]
    if len(rest) < 11:
        return out
    try:
        yy = rest[0:2]
        mmm = rest[2:5]
        dd = rest[5:7]
        hh = rest[7:9]
        mm = rest[9:11]
        team_part = rest[11:]
        # Team part is AWAY+HOME; both 2-3 chars. Common pairs: DETKC, NYMAZ,
        # ATLLAD, STLSD, PITSF. We attempt 2/2, 2/3, 3/2, 3/3 splits and pick
        # the most likely. Without a roster lookup we don't perfectly know.
        # Convention: 3+3 is rare (e.g., LAA + LAD); store the raw string and
        # let downstream join keys handle ambiguity.
        out["date"] = f"20{yy}-{mmm.title()}-{dd}"
        out["start_time_et"] = f"{hh}:{mm}"
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
        **{f"event_{k}": v for k, v in event_meta.items()},
    }


def output_path_for_event(event_ticker: str) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return OUTPUT_DIR / f"kalshi_{event_ticker}_{today}.jsonl"


def snapshot_once() -> None:
    snap_ts = datetime.now(timezone.utc).isoformat()
    events = fetch_open_events()
    if not events:
        log(f"snapshot: 0 open {SERIES_TICKER} events")
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
    log(f"T6 MLB Kalshi logger starting; series={SERIES_TICKER} "
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

    log("T6 MLB Kalshi logger stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
