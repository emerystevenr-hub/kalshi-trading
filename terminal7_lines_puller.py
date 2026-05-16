"""Terminal 7 — NBA/NHL Sharp Vegas Lines Puller.

Pulls current sharp consensus moneylines for NBA and NHL games from The
Odds API and writes them with a 'sport' tag to terminal7_data/vegas_lines_
{date}.jsonl. The signal trader joins these against Kalshi snapshots by
date+teams (sport-tagged) to compute sharp-vs-Kalshi deltas.

Endpoint deltas vs T6 (per spec §4.1):
  - basketball_nba (T6 was baseball_mlb)
  - icehockey_nhl  (added for T7)

Cadence per spec §4.4:
  - Off-hours (00:00-14:00 PT): 90 min between pulls
  - Active hours (14:00-23:00 PT): 15 min between pulls
The daemon ticks every interval-sec and decides per-tick whether enough
time has passed for the time-of-day cadence. Combined burn ~50-90 calls/
day across both sports, comfortably inside Plus-tier 100K/mo.

Auth: same key file as T6 — ~/Documents/.odds_api_key (per spec §9 Q6
decision: "consistency over cleverness").

Usage:
    python3 ~/Documents/terminal7_lines_puller.py
    python3 ~/Documents/terminal7_lines_puller.py --once
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

API_BASE = "https://api.the-odds-api.com/v4"
SPORTS = [
    ("nba", "basketball_nba"),
    ("nhl", "icehockey_nhl"),
]
REGIONS = "us"
MARKETS = "h2h"
ODDS_FORMAT = "decimal"

OUTPUT_DIR = Path.home() / "Documents" / "terminal7_data"
LOG_PATH = OUTPUT_DIR / "lines_puller.log"
KEY_PATH = Path.home() / "Documents" / ".odds_api_key"

# Sharp books — same set as T6. Pinnacle is the gold standard. NHL coverage
# at FanDuel/DraftKings/MGM is sometimes thinner than MLB; the consensus
# requires ≥3 books, which lets thin-coverage games still join.
SHARP_BOOKS = ["pinnacle", "draftkings", "fanduel", "betmgm", "caesars"]
PREFERRED_SHARP = "pinnacle"
MIN_BOOKS_FOR_CONSENSUS = 3   # mirrors T6 — kept gate; relaxes nothing.

# Time-of-day cadence in PT (handoff §11 reminder: NHL puck-drops vary
# from 1pm matinee to 7pm primetime; active window is broad).
# H2 fix: DST-aware via stdlib zoneinfo. T7 runway (May–June 2026) is
# entirely in PDT (UTC-7); previous hardcoded -8 silently shifted active
# cadence an hour late, missing matinee NHL games.
PT_TZ = ZoneInfo("America/Los_Angeles")
ACTIVE_START_HOUR_PT = 14
ACTIVE_END_HOUR_PT = 23
OFF_HOURS_INTERVAL_SEC = 90 * 60   # 90 min
ACTIVE_HOURS_INTERVAL_SEC = 15 * 60   # 15 min
DEFAULT_INTERVAL_SEC = 15 * 60     # daemon tick — 15 min, internal logic
                                   # decides whether to actually pull

_STOP_REQUESTED = False
_LAST_PULL_TS: Dict[str, float] = {}   # per-sport last-pull epoch


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
            f"Sign up at the-odds-api.com (Plus tier $30/mo per spec §9 Q1), "
            f"then: echo YOURKEY > {KEY_PATH}")
        return None
    try:
        return KEY_PATH.read_text().strip()
    except OSError as e:
        log(f"[fatal] could not read API key: {e}")
        return None


def is_active_hours(now_utc: datetime) -> bool:
    """PT-time-of-day check, DST-aware via zoneinfo. Active = 14:00-23:00 PT.
    H2 fix: dynamic DST resolution. Previously hardcoded -8 was wrong for
    T7's runway (entirely in PDT)."""
    pt = now_utc.astimezone(PT_TZ)
    return ACTIVE_START_HOUR_PT <= pt.hour < ACTIVE_END_HOUR_PT


def required_interval_sec(now_utc: datetime) -> int:
    return ACTIVE_HOURS_INTERVAL_SEC if is_active_hours(now_utc) else OFF_HOURS_INTERVAL_SEC


def devig_two_way(decimal_a: float, decimal_b: float) -> Tuple[float, float]:
    if decimal_a <= 1 or decimal_b <= 1:
        return 0.0, 0.0
    pa_raw = 1.0 / decimal_a
    pb_raw = 1.0 / decimal_b
    total = pa_raw + pb_raw
    if total <= 0:
        return 0.0, 0.0
    return pa_raw / total, pb_raw / total


def consensus_implied_p(books: List[dict], home: str, away: str) -> dict:
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
    if len(home_ps) < MIN_BOOKS_FOR_CONSENSUS:
        return {"consensus_home_p": None, "consensus_away_p": None,
                "books_used": len(home_ps), "per_book": per_book}
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


def fetch_lines(api_key: str, sport_endpoint: str) -> List[dict]:
    params = {
        "apiKey": api_key,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
    }
    try:
        r = requests.get(
            f"{API_BASE}/sports/{sport_endpoint}/odds",
            params=params,
            timeout=30,
        )
    except requests.RequestException as e:
        log(f"[error] {sport_endpoint} fetch failed: {e}")
        return []
    if r.status_code == 401:
        log(f"[fatal] 401 unauthorized on {sport_endpoint} — API key bad")
        return []
    if r.status_code == 429:
        log(f"[error] 429 rate-limited on {sport_endpoint}; backing off")
        return []
    if r.status_code != 200:
        log(f"[error] {sport_endpoint} returned {r.status_code}: {r.text[:300]}")
        return []
    used = r.headers.get("x-requests-used", "?")
    remaining = r.headers.get("x-requests-remaining", "?")
    log(f"  odds-api {sport_endpoint}: used={used} remaining={remaining}")
    return r.json() or []


def project_game(game: dict, sport: str) -> dict:
    home = game.get("home_team")
    away = game.get("away_team")
    books = game.get("bookmakers", [])
    consensus = consensus_implied_p(books, home, away)
    return {
        "_schema_version": "v1",
        "snap_ts_utc": datetime.now(timezone.utc).isoformat(),
        "sport": sport,                 # T7 delta — sport tag for trader join
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


def pull_sport(api_key: str, sport: str, sport_endpoint: str) -> int:
    games = fetch_lines(api_key, sport_endpoint)
    if not games:
        log(f"pull[{sport}]: 0 games returned")
        return 0
    rows = [project_game(g, sport) for g in games]
    write_rows(rows)
    n_with_consensus = sum(1 for r in rows if r.get("consensus_home_p") is not None)
    log(f"pull[{sport}]: {len(games)} games, {n_with_consensus} with sharp consensus")
    return len(games)


def pull_once_all_sports(api_key: str, force: bool = False) -> int:
    """Pull each sport if its time-of-day cadence has elapsed (or force=True)."""
    now_utc = datetime.now(timezone.utc)
    interval = required_interval_sec(now_utc)
    pt = now_utc.astimezone(PT_TZ)
    log(f"cadence check: PT={pt.strftime('%H:%M %Z')}, "
        f"window={'active' if is_active_hours(now_utc) else 'off-hours'}, "
        f"required_interval={interval}s")
    total = 0
    for sport, endpoint in SPORTS:
        last = _LAST_PULL_TS.get(sport, 0.0)
        elapsed = time.time() - last
        if not force and elapsed < interval:
            log(f"  [skip] {sport} last pull {elapsed:.0f}s ago < {interval}s")
            continue
        n = pull_sport(api_key, sport, endpoint)
        _LAST_PULL_TS[sport] = time.time()
        total += n
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Single pull (forces both sports), then exit.")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC,
                    help="Daemon tick cadence (default 900 = 15 min). Internal "
                         "logic skips off-hours pulls inside the 90-min window.")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"T7 NBA/NHL Lines Puller starting; tick={args.interval_sec}s "
        f"once={args.once} sports={[s for s, _ in SPORTS]}")

    api_key = load_api_key()
    if not api_key:
        return 1

    if args.once:
        pull_once_all_sports(api_key, force=True)
        return 0

    while not _STOP_REQUESTED:
        try:
            pull_once_all_sports(api_key, force=False)
        except Exception as e:
            log(f"[error] pull raised: {e}")
        slept = 0
        while slept < args.interval_sec and not _STOP_REQUESTED:
            time.sleep(1)
            slept += 1

    log("T7 NBA/NHL Lines Puller stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
