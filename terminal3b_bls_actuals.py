"""Terminal 3b — BLS CPI Actuals Puller.

Pulls CPI-U All Items NSA from BLS, computes Y/Y inflation per month
(matching Kalshi's settlement convention: 1-decimal-place rounding), and
writes a canonical JSONL.

Series: CUUR0000SA0 — CPI for All Urban Consumers, U.S. City Average,
All Items, Not Seasonally Adjusted. Same series Kalshi resolves CPI YoY
markets against.

YoY computation:
    yoy = (index_M / index_M_minus_12 - 1) × 100
    yoy_rounded = round(yoy, 1)        # to one decimal — Kalshi convention

Output:
    ~/Documents/terminal3b_data/bls_cpi_yoy.jsonl
    Each row: { _schema_version, year, period, period_name, date_local,
                index_value, yoy_pct, yoy_rounded, source, observed_ts_utc }

Usage:
    # Pull last 24 months (default, sufficient for YoY computation):
    python3 ~/Documents/terminal3b_bls_actuals.py

    # Pull more history (e.g. for σ backfit calibration analysis):
    python3 ~/Documents/terminal3b_bls_actuals.py --years-back 5

Public API allows ≤25 queries/day without a key. For higher limits, register
at https://data.bls.gov/registrationEngine/ and set BLS_API_KEY env var.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests


BLS_BASE = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
SERIES_ID = "CUUR0000SA0"  # CPI-U All Items, NSA, U.S. City Average

DATA_DIR = Path.home() / "Documents" / "terminal3b_data"
LOG_PATH = DATA_DIR / "bls_actuals.log"
OUT_PATH = DATA_DIR / "bls_cpi_yoy.jsonl"

SCHEMA_VERSION = "v1"
REQUEST_TIMEOUT = 30


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


def _period_to_month(period: str) -> Optional[int]:
    """BLS period codes: 'M01'..'M12' → 1..12. 'M13' is annual avg, ignored."""
    if not period or not period.startswith("M"):
        return None
    try:
        m = int(period[1:])
        return m if 1 <= m <= 12 else None
    except ValueError:
        return None


def fetch_cpi(start_year: int, end_year: int) -> List[dict]:
    """Hit the BLS public API for CUUR0000SA0 between start_year and end_year
    inclusive. Returns the raw monthly index list.
    """
    payload = {
        "seriesid": [SERIES_ID],
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    api_key = os.environ.get("BLS_API_KEY")
    if api_key:
        payload["registrationkey"] = api_key

    try:
        # Bypass any host HTTP(S)_PROXY env vars that might be 403'ing
        # (mirrors the lesson from T1's NWS puller).
        r = requests.post(
            BLS_BASE + SERIES_ID,
            json=payload,
            timeout=REQUEST_TIMEOUT,
            proxies={"http": None, "https": None},
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log(f"  [error] BLS POST failed: {e}")
        # Fall back to GET (works for non-keyed requests):
        try:
            r = requests.get(
                BLS_BASE + SERIES_ID,
                params={"startyear": str(start_year), "endyear": str(end_year)},
                timeout=REQUEST_TIMEOUT,
                proxies={"http": None, "https": None},
            )
            r.raise_for_status()
        except requests.RequestException as e2:
            log(f"  [error] BLS GET fallback failed: {e2}")
            return []

    body = r.json()
    if body.get("status") != "REQUEST_SUCCEEDED":
        log(f"  [error] BLS status={body.get('status')} msg={body.get('message')}")
        return []

    series = (body.get("Results", {}) or {}).get("series", []) or []
    if not series:
        log("  [error] BLS returned empty series")
        return []
    return series[0].get("data", []) or []


def compute_yoy_records(raw_data: List[dict]) -> List[dict]:
    """Take raw BLS monthly index rows (newest first per the API), compute
    YoY for each month that has a 12-months-ago counterpart, and return
    canonical schema records.
    """
    # Build (year, month) → index for fast lookup
    index_map: Dict[tuple, dict] = {}
    for r in raw_data:
        try:
            y = int(r["year"])
        except (KeyError, ValueError):
            continue
        m = _period_to_month(r.get("period", ""))
        if m is None:
            continue
        try:
            v = float(r["value"])
        except (KeyError, ValueError, TypeError):
            continue
        index_map[(y, m)] = {
            "index_value": v,
            "period": r.get("period"),
            "period_name": r.get("periodName"),
            "latest": r.get("latest") == "true",
        }

    out: List[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for (y, m), info in sorted(index_map.items()):
        prev = index_map.get((y - 1, m))
        if prev is None:
            continue  # no YoY base
        prev_v = prev["index_value"]
        if prev_v <= 0:
            continue
        yoy = (info["index_value"] / prev_v - 1.0) * 100.0
        out.append({
            "_schema_version": SCHEMA_VERSION,
            "year": y,
            "period": info["period"],
            "period_name": info["period_name"],
            "date_local": f"{y:04d}-{m:02d}-01",  # first of month for sortability
            "index_value": info["index_value"],
            "index_value_yoy_base": prev_v,
            "yoy_pct": round(yoy, 4),                   # high-precision for analysis
            "yoy_rounded": round(yoy, 1),               # Kalshi settlement convention
            "is_latest": info.get("latest", False),
            "source": "BLS_CUUR0000SA0",
            "observed_ts_utc": now_iso,
        })
    return out


def write_records(recs: List[dict]) -> int:
    """Append-only with dedup on (year, period). Re-writes the file in place
    if any existing rows need to be replaced (newer observations of same
    period — BLS occasionally revises)."""
    if not recs:
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing: Dict[tuple, dict] = {}
    if OUT_PATH.exists():
        try:
            with open(OUT_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = (r.get("year"), r.get("period"))
                    existing[key] = r
        except OSError:
            pass

    new_or_revised = 0
    for r in recs:
        key = (r["year"], r["period"])
        prev = existing.get(key)
        if prev is None:
            new_or_revised += 1
            existing[key] = r
        elif prev.get("index_value") != r["index_value"] or \
             prev.get("yoy_rounded") != r["yoy_rounded"]:
            new_or_revised += 1
            existing[key] = r

    # Rewrite sorted
    try:
        with open(OUT_PATH, "w") as f:
            for key in sorted(existing.keys()):
                f.write(json.dumps(existing[key]) + "\n")
    except OSError as e:
        log(f"  [error] write {OUT_PATH}: {e}")
        return 0
    return new_or_revised


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years-back", type=int, default=2,
                    help="how many years of history to pull (default 2 — covers YoY "
                         "computation. Use 5+ for σ backfit context.)")
    args = ap.parse_args()

    end_year = datetime.now(timezone.utc).year
    start_year = end_year - args.years_back

    log("=" * 70)
    log(f"T3b BLS CPI Puller — {SERIES_ID}")
    log(f"Range: {start_year} → {end_year}  ({args.years_back + 1} yrs)")
    log("=" * 70)

    raw = fetch_cpi(start_year, end_year)
    if not raw:
        log("No data returned from BLS. Aborting.")
        return 1
    log(f"Fetched {len(raw)} raw monthly index rows from BLS.")

    recs = compute_yoy_records(raw)
    log(f"Computed YoY for {len(recs)} months (need 12-month-prior base).")

    n = write_records(recs)
    log(f"Wrote/revised {n} rows in {OUT_PATH.name}.")

    if recs:
        latest = max(recs, key=lambda r: (r["year"], r["period"]))
        log(f"Latest published month: {latest['period_name']} {latest['year']}  "
            f"index={latest['index_value']}  "
            f"YoY={latest['yoy_pct']:.4f}%  rounded={latest['yoy_rounded']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
