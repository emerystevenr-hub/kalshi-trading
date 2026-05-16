"""Terminal 3c — FRED ICSA Puller.

Pulls weekly Initial Claims (ICSA) history from FRED's public CSV endpoint
and maintains an idempotent JSONL archive. No API key needed for the CSV
endpoint.

ICSA = Initial Claims, Seasonally Adjusted, weekly. Released Thursdays at
8:30 AM ET (12:30 UTC) by DOL; FRED mirrors within ~1 hour.

Output:
    ~/Documents/terminal3c_data/icsa_history.jsonl
        {date: "YYYY-MM-DD" (week-ending Saturday), icsa: int, ts_utc: ...}

Usage:
    python3 ~/Documents/terminal3c_claims_data.py
    python3 ~/Documents/terminal3c_claims_data.py --start 2020-01-01
    python3 ~/Documents/terminal3c_claims_data.py --print-stats

Re-runs are idempotent — existing rows are merged on `date`, only newer
or revised values are written. (FRED occasionally revises prior weeks.)
"""

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests


FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SERIES_ID = "ICSA"
DEFAULT_START = "2020-01-01"
REQUEST_TIMEOUT = 30
INCREMENTAL_LOOKBACK_DAYS = 35  # on incremental pulls, look back this far to catch FRED revisions

DATA_DIR = Path.home() / "Documents" / "terminal3c_data"
HISTORY_PATH = DATA_DIR / "icsa_history.jsonl"
LOG_PATH = DATA_DIR / "claims_data.log"


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


def fetch_fred_csv(start: str) -> Optional[str]:
    """Fetch FRED CSV for ICSA from `start` to present."""
    params = {"id": SERIES_ID, "cosd": start}
    try:
        # NOTE: do NOT set a custom User-Agent here. FRED's WAF slow-lanes
        # browser-style UAs ("Mozilla/...") hitting the CSV endpoint — they
        # expect humans on the chart page and programmatic clients on /csv.
        # The default requests UA ("python-requests/X.Y") is fast-lane.
        r = requests.get(
            FRED_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        log(f"[error] FRED fetch failed: {e}")
        return None
    if r.status_code != 200:
        log(f"[error] FRED returned {r.status_code}")
        return None
    return r.text


def parse_csv(text: str) -> List[Tuple[str, int]]:
    """Parse FRED CSV into [(date_str, claims_int), ...].

    FRED CSV columns: observation_date, ICSA  (or similar — header varies).
    Missing values are '.' which we skip.
    """
    rows: List[Tuple[str, int]] = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 2:
        log(f"[error] unexpected CSV header: {header!r}")
        return rows
    for row in reader:
        if len(row) < 2:
            continue
        date_str, val = row[0].strip(), row[1].strip()
        if not date_str or val in ("", "."):
            continue
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        try:
            claims = int(float(val))
        except ValueError:
            continue
        rows.append((date_str, claims))
    return rows


def load_existing() -> Dict[str, dict]:
    """Replay history file → {date: latest_record}."""
    out: Dict[str, dict] = {}
    if not HISTORY_PATH.exists():
        return out
    with open(HISTORY_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = rec.get("date")
            if d:
                out[d] = rec
    return out


def merge_and_write(rows: List[Tuple[str, int]]) -> dict:
    """Idempotent merge: only append rows that are new or revised."""
    existing = load_existing()
    snap_ts = datetime.now(timezone.utc).isoformat()
    appended = 0
    revised = 0
    unchanged = 0
    new_rows: List[dict] = []
    for date_str, claims in rows:
        prev = existing.get(date_str)
        if prev is None:
            new_rows.append({
                "date": date_str,
                "icsa": claims,
                "ts_utc": snap_ts,
                "_event": "new",
            })
            appended += 1
        elif prev.get("icsa") != claims:
            new_rows.append({
                "date": date_str,
                "icsa": claims,
                "ts_utc": snap_ts,
                "_event": "revision",
                "_prev_icsa": prev.get("icsa"),
            })
            revised += 1
        else:
            unchanged += 1
    if new_rows:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "a") as f:
            for r in new_rows:
                f.write(json.dumps(r) + "\n")
    return {
        "appended": appended,
        "revised": revised,
        "unchanged": unchanged,
        "fetched": len(rows),
    }


def latest_state() -> Dict[str, dict]:
    """Return {date: latest_record} after merge — for stats / consumers."""
    return load_existing()


def print_stats() -> None:
    state = latest_state()
    if not state:
        log("[stats] history empty — run a fetch first")
        return
    dates = sorted(state.keys())
    last10 = dates[-10:]
    log(f"[stats] {len(dates)} weekly observations from {dates[0]} → {dates[-1]}")
    log("[stats] last 10 prints:")
    for d in last10:
        log(f"        {d}  ICSA={state[d]['icsa']:>7,}")
    # 4-week mean and 26-week WoW σ as a sanity check on the model inputs
    if len(dates) >= 26:
        last26_vals = [state[d]["icsa"] for d in dates[-26:]]
        wow_deltas = [b - a for a, b in zip(last26_vals[:-1], last26_vals[1:])]
        n = len(wow_deltas)
        mean_wow = sum(wow_deltas) / n
        var = sum((x - mean_wow) ** 2 for x in wow_deltas) / n
        sigma = var ** 0.5
        last4 = [state[d]["icsa"] for d in dates[-4:]]
        mu = sum(last4) / 4
        log(f"[stats] μ (4-week trailing mean):   {mu:>10,.0f}")
        log(f"[stats] σ (26-week WoW pstdev):     {sigma:>10,.0f}")


def resolve_start(arg_start: Optional[str]) -> str:
    """Pick the smallest start date that is safe to pull.

    If --start was passed explicitly, honor it.
    Else: if history exists, start `INCREMENTAL_LOOKBACK_DAYS` before the
    latest local date (catches FRED revisions). If empty, full backfill.
    """
    if arg_start and arg_start != DEFAULT_START:
        return arg_start
    state = load_existing()
    if not state:
        return DEFAULT_START
    latest = max(state.keys())
    try:
        d = datetime.strptime(latest, "%Y-%m-%d")
    except ValueError:
        return DEFAULT_START
    from datetime import timedelta
    return (d - timedelta(days=INCREMENTAL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START,
                    help=f"earliest date to pull (default: incremental from local history, falls back to {DEFAULT_START})")
    ap.add_argument("--full", action="store_true",
                    help=f"force full backfill from {DEFAULT_START}")
    ap.add_argument("--print-stats", action="store_true",
                    help="after merge, print summary stats")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip the network call; just print stats from local history")
    args = ap.parse_args()

    effective_start = DEFAULT_START if args.full else resolve_start(args.start)
    log(f"T3c Claims Data starting. start={effective_start} no_fetch={args.no_fetch}")
    args.start = effective_start

    if not args.no_fetch:
        text = fetch_fred_csv(args.start)
        if text is None:
            log("[error] no CSV body — aborting")
            return 1
        rows = parse_csv(text)
        if not rows:
            log("[error] CSV parsed to zero rows — aborting")
            return 1
        result = merge_and_write(rows)
        log(f"merge: fetched={result['fetched']} appended={result['appended']} "
            f"revised={result['revised']} unchanged={result['unchanged']}")

    if args.print_stats or args.no_fetch:
        print_stats()

    return 0


if __name__ == "__main__":
    sys.exit(main())
