#!/usr/bin/env python3
"""SGO Pro — degraded-D pull: 30-day MLB open/close + lastUpdatedAt.

Born 2026-05-10. SGO Pro REST tier exposes only opening + closing
aggregate odds at the market level (openBookOdds, closeBookOdds) and
single-point per-book closing odds + a single lastUpdatedAt timestamp
per book. No 5-min cadence time-series. So Steve's D test
("did Pinnacle move farther in same direction than retail?") cannot
be answered exactly per book — only via proxies:

  Signal-1: Pinnacle_close_implied_p - openBookOdds_implied_p, vs
            mean(DK,FD,MGM)_close_implied_p - openBookOdds_implied_p.
            If |Pinnacle_move| consistently > |retail_move| in the
            same sign direction, Pinnacle incorporated more info.

  Signal-2: sign(Pinnacle_move) == sign(retail_move) frequency. If
            they always agree, retail captures the same signal.

  Signal-3: Pinnacle.lastUpdatedAt − retail.lastUpdatedAt. Pinnacle
            consistently updating later than retail = Pinnacle moves
            after retail (sub-spread). Pinnacle consistently updating
            earlier = leadership signal.

Output: jsonl rows, one per game, with all three signal inputs.
       Analysis is in sgo_d_analyze.py.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

DOCS = Path.home() / "Documents"
KEY = (DOCS / ".sgo_api_key").read_text().strip()
OUTPATH = DOCS / "terminal7_data" / "sgo_open_close_30d.jsonl"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
BASE = "https://api.sportsgameodds.com/v2/events"

RETAIL_BOOKS = ["draftkings", "fanduel", "betmgm"]


def implied_p_from_american(odds_str) -> float | None:
    """American odds string ('-120' or '+150') → implied probability."""
    if odds_str is None:
        return None
    try:
        s = str(odds_str).strip()
        n = int(s)
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    if n > 0:
        return 100.0 / (n + 100.0)
    return -n / (-n + 100.0)


def fetch_page(cursor: str, starts_after: str, starts_before: str) -> dict:
    qs = {
        "leagueID": "MLB",
        "finalized": "true",
        "startsAfter": starts_after,
        "startsBefore": starts_before,
        "limit": "50",
    }
    if cursor:
        qs["cursor"] = cursor
    url = BASE + "?" + urllib.parse.urlencode(qs)
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={
                "x-api-key": KEY,
                "User-Agent": UA,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    raise last_err


def extract_row(ev: dict) -> dict | None:
    teams = ev.get("teams", {}) or {}
    home = (teams.get("home") or {}).get("names", {}).get("long")
    away = (teams.get("away") or {}).get("names", {}).get("long")
    starts_at = (ev.get("status") or {}).get("startsAt")
    if not (home and away and starts_at):
        return None
    odds = ev.get("odds", {}) or {}
    home_ml = odds.get("points-home-game-ml-home")
    if not home_ml:
        return None
    by_bk = home_ml.get("byBookmaker", {}) or {}

    def book_entry(name: str):
        v = by_bk.get(name)
        if not v:
            return None
        return {
            "odds": v.get("odds"),
            "implied_p": implied_p_from_american(v.get("odds")),
            "lastUpdatedAt": v.get("lastUpdatedAt"),
            "available": v.get("available"),
        }

    return {
        "eventID": ev.get("eventID"),
        "startsAt": starts_at,
        "home": home,
        "away": away,
        "openBookOdds": home_ml.get("openBookOdds"),
        "openBookOdds_implied_p": implied_p_from_american(home_ml.get("openBookOdds")),
        "closeBookOdds": home_ml.get("closeBookOdds"),
        "closeBookOdds_implied_p": implied_p_from_american(home_ml.get("closeBookOdds")),
        "openFairOdds": home_ml.get("openFairOdds"),
        "closeFairOdds": home_ml.get("closeFairOdds"),
        "pinnacle": book_entry("pinnacle"),
        "draftkings": book_entry("draftkings"),
        "fanduel": book_entry("fanduel"),
        "betmgm": book_entry("betmgm"),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-ago-start", type=int, default=30,
                    help="window starts this many days before today (older edge)")
    ap.add_argument("--days-ago-end", type=int, default=0,
                    help="window ends this many days before today (newer edge)")
    ap.add_argument("--append", action="store_true", help="append to output, don't truncate")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date()
    starts_after = (today - timedelta(days=args.days_ago_start)).isoformat()
    starts_before = (today - timedelta(days=args.days_ago_end)).isoformat()
    print(f"window: [{starts_after}, {starts_before})")
    OUTPATH.parent.mkdir(parents=True, exist_ok=True)
    cursor = ""
    pages = 0
    rows = 0
    skipped = 0
    api_calls = 0
    mode = "a" if args.append else "w"
    with open(OUTPATH, mode) as f:
        while True:
            try:
                page = fetch_page(cursor, starts_after, starts_before)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:400]
                print(f"HTTP {e.code} on page {pages+1}: {body}")
                return 1
            api_calls += 1
            events = page.get("data", []) or []
            for ev in events:
                row = extract_row(ev)
                if row:
                    f.write(json.dumps(row) + "\n")
                    rows += 1
                else:
                    skipped += 1
            pages += 1
            print(f"  page {pages}: {len(events)} events  (rows={rows} skipped={skipped} api_calls={api_calls})")
            cursor = page.get("nextCursor") or ""
            if not cursor:
                break
            # Modest throttle (Cloudflare behind SGO is generous on auth'd traffic)
            time.sleep(0.05)
    print(f"\nDONE. wrote {rows} rows to {OUTPATH}  api_calls={api_calls}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
