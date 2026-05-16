"""Terminal 1 — NWS Actuals Puller.

Pulls historical daily MAX/MIN temperatures from the Iowa State Mesonet
for NYC/ORD/LAX. These are ASOS-station observations — the same sensors
Kalshi's weather markets resolve against, so forecast-vs-truth comparisons
are directly valid.

Data source: https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py
  - Free, no auth required
  - Serves daily aggregates derived from ASOS METAR reports
  - Coverage back to ~1990s for major US airports

Usage:
    # Pull last 90 days for all 3 stations.
    python3 terminal1_nws_actuals.py --backfill-days 90

    # Pull a specific range:
    python3 terminal1_nws_actuals.py --start 2026-01-22 --end 2026-04-22

    # Single station only:
    python3 terminal1_nws_actuals.py --backfill-days 30 --station NYC

Output (canonical per terminal1_data_schema.md §3):
    ~/Documents/terminal1_data/nws_actuals_{station}.jsonl
"""

import argparse
import csv
import io
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests


DATA_DIR = Path.home() / "Documents" / "terminal1_data"
LOG_FILE = Path.home() / "Documents" / "terminal1_actuals.log"
SCHEMA_VERSION = "v1"

MESONET_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py"
REQUEST_TIMEOUT = 60

# ASOS-station mappings. Station codes match Iowa State's mesonet conventions.
STATIONS: Dict[str, Dict[str, str]] = {
    # Tier 1 (original):
    "NYC": {"network": "NY_ASOS", "station": "NYC", "icao": "KNYC"},
    "ORD": {"network": "IL_ASOS", "station": "ORD", "icao": "KORD"},
    "LAX": {"network": "CA_ASOS", "station": "LAX", "icao": "KLAX"},
    # Tier 2 (expansion — added 2026-04-24):
    "DEN": {"network": "CO_ASOS", "station": "DEN", "icao": "KDEN"},
    "ATL": {"network": "GA_ASOS", "station": "ATL", "icao": "KATL"},
    "MIA": {"network": "FL_ASOS", "station": "MIA", "icao": "KMIA"},
    "PHX": {"network": "AZ_ASOS", "station": "PHX", "icao": "KPHX"},
    # DFW removed 2026-04-24 — no Kalshi markets for Dallas.
}


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _to_float(v) -> Optional[float]:
    if v is None or v == "" or v == "None" or v == "M":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_station(
    station_code: str,
    start_date: date,
    end_date: date,
) -> List[dict]:
    """Pull daily MAX/MIN temps for one station from Iowa State Mesonet.
    Returns list of canonical records per schema §3."""
    meta = STATIONS[station_code]
    params = {
        "network": meta["network"],
        "stations": meta["station"],
        "year1": start_date.year,
        "month1": start_date.month,
        "day1": start_date.day,
        "year2": end_date.year,
        "month2": end_date.month,
        "day2": end_date.day,
        "format": "csv",
    }
    try:
        # Mesonet is public; bypass any host HTTP(S)_PROXY env vars that
        # would otherwise tunnel the request and get 403'd. (2026-04-26
        # recovery: a host-side proxy was rejecting CONNECT to Mesonet.)
        r = requests.get(
            MESONET_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log(f"  [error] {station_code} request failed: {e}")
        return []

    text = r.text
    if not text or text.startswith("ERROR"):
        log(f"  [error] {station_code} empty/error response")
        return []

    # Parse CSV. Columns we need: station,day,max_tmpf,min_tmpf
    reader = csv.DictReader(io.StringIO(text))
    records: List[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for row in reader:
        day_str = row.get("day", "").strip()
        if not day_str:
            continue
        try:
            # Iowa State uses YYYY-MM-DD.
            d = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        high_f = _to_float(row.get("max_temp_f"))
        low_f = _to_float(row.get("min_temp_f"))
        if high_f is None and low_f is None:
            continue  # no useful obs

        records.append({
            "_schema_version": SCHEMA_VERSION,
            "station": station_code,
            "station_icao": meta["icao"],
            "date_local": day_str,
            "high_f": high_f,
            "low_f": low_f,
            "source": "IOWA_STATE_ASOS",
            "observed_ts_utc": now_iso,
        })

    return records


def write_records(station_code: str, records: List[dict]) -> int:
    """Merge new records into the per-station JSONL.

    Behavior (2026-04-27 fix): for any record whose date_local already
    exists in the file, OVERWRITE it with the newer values (Mesonet
    returns running daily aggregates — a re-pull during the day produces
    higher highs / lower lows than the morning snapshot). Without this
    overwrite, the partial-day reading sticks forever and the reconciler
    settles against stale data (3 positions had to be manually corrected
    on 2026-04-27 — see terminal1_fix_apr27_partial.py).

    Returns the number of rows added or revised.
    """
    if not records:
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"nws_actuals_{station_code}.jsonl"

    # Read existing rows keyed by date_local
    existing: Dict[str, dict] = {}
    if path.exists():
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    d = r.get("date_local")
                    if d:
                        existing[d] = r
        except OSError:
            pass

    changes = 0
    for new_rec in records:
        d = new_rec["date_local"]
        prev = existing.get(d)
        if prev is None:
            existing[d] = new_rec
            changes += 1
            continue
        # Compare high_f and low_f. If the new value extends max/min
        # (running daily aggregates can only grow), update. If equal,
        # skip (no change).
        prev_h = prev.get("high_f")
        prev_l = prev.get("low_f")
        new_h = new_rec.get("high_f")
        new_l = new_rec.get("low_f")
        merged_h = prev_h
        merged_l = prev_l
        merged = False
        if new_h is not None and (prev_h is None or new_h > prev_h):
            merged_h = new_h
            merged = True
        if new_l is not None and (prev_l is None or new_l < prev_l):
            merged_l = new_l
            merged = True
        if merged:
            existing[d] = {
                **prev,
                "high_f": merged_h,
                "low_f": merged_l,
                "observed_ts_utc": new_rec.get("observed_ts_utc",
                                               prev.get("observed_ts_utc")),
                "_revised_from": {
                    "old_high_f": prev_h, "old_low_f": prev_l,
                    "old_observed_ts_utc": prev.get("observed_ts_utc"),
                },
            }
            changes += 1

    if changes == 0:
        return 0

    # Rewrite the file in date order (atomic via temp + rename)
    try:
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w") as f:
            for d in sorted(existing.keys()):
                f.write(json.dumps(existing[d]) + "\n")
        tmp.replace(path)
    except OSError as e:
        log(f"  [error] write failed {path}: {e}")
        return 0
    return changes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-days", type=int, default=0,
                    help="Pull last N days of history.")
    ap.add_argument("--start", type=str, default=None,
                    help="Start date YYYY-MM-DD.")
    ap.add_argument("--end", type=str, default=None,
                    help="End date YYYY-MM-DD.")
    ap.add_argument("--station", choices=list(STATIONS.keys()), default=None,
                    help="Limit to one station (default: all).")
    args = ap.parse_args()

    if args.backfill_days > 0:
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=args.backfill_days)
    elif args.start and args.end:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    else:
        ap.print_help()
        sys.exit(1)

    stations = [args.station] if args.station else list(STATIONS.keys())

    log("=" * 80)
    log("TERMINAL 1 — NWS ACTUALS PULLER")
    log("=" * 80)
    log(f"Range: {start_date} → {end_date} ({(end_date - start_date).days} days)")
    log(f"Stations: {', '.join(stations)}")
    log("")

    total_new = 0
    for s in stations:
        t0 = time.time()
        recs = fetch_station(s, start_date, end_date)
        n = write_records(s, recs)
        total_new += n
        elapsed = time.time() - t0
        log(f"  {s}: fetched {len(recs)} records, wrote {n} new "
            f"({elapsed:.1f}s)")

    log("")
    log(f"Done. Total new records written: {total_new}")


if __name__ == "__main__":
    main()
