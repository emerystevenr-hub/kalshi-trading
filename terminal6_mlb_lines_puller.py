"""Terminal 6 — MLB Sharp Vegas Lines Puller.

Pulls current sharp consensus moneylines for MLB games from The Odds API
and writes them per-game to terminal6_data/vegas_lines_{date}.jsonl.

The signal trader joins these against Kalshi snapshots by date+teams to
compute (sharp_implied_p − kalshi_implied_p) deltas.

Auth:
    The Odds API requires a free API key. Sign up at the-odds-api.com,
    then write the key to ~/Documents/.odds_api_key (single line, no quotes).
    Free tier: 500 requests/month. Each call = 1 request, ~30 markets/call.

Usage:
    python3 ~/Documents/terminal6_mlb_lines_puller.py
    python3 ~/Documents/terminal6_mlb_lines_puller.py --once

Output rows have fields: game_id, commence_time_utc, home_team, away_team,
home_implied_p_devig, away_implied_p_devig, books_used, raw_books.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

API_BASE = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"
REGIONS = "us"
MARKETS = "h2h"
ODDS_FORMAT = "decimal"

OUTPUT_DIR = Path.home() / "Documents" / "terminal6_data"
LOG_PATH = OUTPUT_DIR / "lines_puller.log"
KEY_PATH = Path.home() / "Documents" / ".odds_api_key"

# Sharp books we trust most. The Odds API returns many books; we filter to
# these and average their de-vigged implied probabilities. Pinnacle is the
# gold standard for sharp consensus on MLB. DraftKings + FanDuel included
# as floor (more retail-aimed, useful for diff comparisons).
SHARP_BOOKS = ["pinnacle", "draftkings", "fanduel", "betmgm", "caesars"]
PREFERRED_SHARP = "pinnacle"

DEFAULT_INTERVAL_SEC = 1800  # 30 min on game days

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
    log("[signal] stop requested; will exit after current pull")


def load_api_key() -> Optional[str]:
    if not KEY_PATH.exists():
        log(f"[fatal] API key file not found at {KEY_PATH}. "
            f"Sign up at the-odds-api.com, then: "
            f"echo YOURKEY > {KEY_PATH}")
        return None
    try:
        return KEY_PATH.read_text().strip()
    except OSError as e:
        log(f"[fatal] could not read API key: {e}")
        return None


def devig_two_way(decimal_a: float, decimal_b: float) -> Tuple[float, float]:
    """Convert two decimal odds into de-vigged fair-value probabilities."""
    if decimal_a <= 1 or decimal_b <= 1:
        return 0.0, 0.0
    pa_raw = 1.0 / decimal_a
    pb_raw = 1.0 / decimal_b
    total = pa_raw + pb_raw
    if total <= 0:
        return 0.0, 0.0
    return pa_raw / total, pb_raw / total


def consensus_implied_p(books: List[dict], home: str, away: str) -> dict:
    """Average de-vigged implied probabilities across sharp books.

    books: list of {key, markets:[{key:'h2h', outcomes:[{name, price}]}]}
    Returns dict with consensus + per-book breakdown.
    """
    per_book = []
    home_ps = []
    away_ps = []
    for b in books:
        if b.get("key") not in SHARP_BOOKS:
            continue
        markets = b.get("markets", [])
        h2h = next((m for m in markets if m.get("key") == "h2h"), None)
        if not h2h:
            continue
        outcomes = h2h.get("outcomes", [])
        h_outcome = next((o for o in outcomes if o.get("name") == home), None)
        a_outcome = next((o for o in outcomes if o.get("name") == away), None)
        if not (h_outcome and a_outcome):
            continue
        try:
            hp, ap = devig_two_way(
                float(h_outcome.get("price")),
                float(a_outcome.get("price")),
            )
        except (TypeError, ValueError):
            continue
        per_book.append({
            "book": b.get("key"),
            "home_p": hp, "away_p": ap,
            "home_decimal": h_outcome.get("price"),
            "away_decimal": a_outcome.get("price"),
        })
        home_ps.append(hp)
        away_ps.append(ap)
    if not home_ps:
        return {"consensus_home_p": None, "consensus_away_p": None,
                "books_used": 0, "per_book": []}
    return {
        "consensus_home_p": sum(home_ps) / len(home_ps),
        "consensus_away_p": sum(away_ps) / len(away_ps),
        "pinnacle_home_p": next(
            (b["home_p"] for b in per_book if b["book"] == PREFERRED_SHARP),
            None),
        "pinnacle_away_p": next(
            (b["away_p"] for b in per_book if b["book"] == PREFERRED_SHARP),
            None),
        "books_used": len(home_ps),
        "per_book": per_book,
    }


def fetch_mlb_lines(api_key: str) -> List[dict]:
    params = {
        "apiKey": api_key,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
    }
    try:
        r = requests.get(
            f"{API_BASE}/sports/{SPORT}/odds",
            params=params,
            timeout=30,
        )
    except requests.RequestException as e:
        log(f"[error] Odds API fetch failed: {e}")
        return []
    if r.status_code == 401:
        log("[fatal] 401 unauthorized — API key bad")
        return []
    if r.status_code == 429:
        log("[error] 429 rate-limited by Odds API; backing off")
        return []
    if r.status_code != 200:
        log(f"[error] Odds API returned {r.status_code}: {r.text[:300]}")
        return []
    # Capture remaining-requests header for visibility
    used = r.headers.get("x-requests-used", "?")
    remaining = r.headers.get("x-requests-remaining", "?")
    log(f"  odds-api: used={used} remaining={remaining}")
    return r.json() or []


def project_game(game: dict) -> dict:
    home = game.get("home_team")
    away = game.get("away_team")
    books = game.get("bookmakers", [])
    consensus = consensus_implied_p(books, home, away)
    return {
        "_schema_version": "v1",
        "snap_ts_utc": datetime.now(timezone.utc).isoformat(),
        "game_id": game.get("id"),
        "commence_time_utc": game.get("commence_time"),
        "home_team": home,
        "away_team": away,
        "books_total": len(books),
        **consensus,
    }


def write_rows(rows: List[dict]) -> None:
    if not rows:
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = OUTPUT_DIR / f"vegas_lines_{today}.jsonl"
    with open(out_path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def pull_once() -> int:
    api_key = load_api_key()
    if not api_key:
        return 1
    games = fetch_mlb_lines(api_key)
    if not games:
        log("pull: 0 games returned")
        return 0
    rows = [project_game(g) for g in games]
    write_rows(rows)
    n_with_consensus = sum(1 for r in rows if r.get("consensus_home_p") is not None)
    log(f"pull: {len(games)} games, {n_with_consensus} with sharp consensus")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Single pull, then exit.")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC,
                    help="Pull cadence in seconds (default 1800 = 30 min).")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"T6 MLB Lines Puller starting; interval={args.interval_sec}s once={args.once}")

    if args.once:
        return pull_once()

    while not _STOP_REQUESTED:
        try:
            pull_once()
        except Exception as e:
            log(f"[error] pull raised: {e}")
        slept = 0
        while slept < args.interval_sec and not _STOP_REQUESTED:
            time.sleep(1)
            slept += 1

    log("T6 MLB Lines Puller stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
