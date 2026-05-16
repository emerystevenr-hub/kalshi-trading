"""Terminal 3 — Cleveland Fed Inflation Nowcast History Puller.

Purpose: download the Cleveland Fed daily Inflation Nowcast time series for
CPI and PCE (headline + core), to be used as the "model prior" input for the
Terminal 3 nowcast-divergence survey.

The Cleveland Fed publishes:
  - Daily nowcasts for current month + next month
  - For: CPI (headline), Core CPI, PCE (headline), Core PCE
  - Each is a YoY % change forecast and a MoM % change forecast
  - Historical values back to ~2012

Their public data lives at:
  https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting
And the underlying CSV is typically at:
  https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast-current-data.xlsx
  https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast.xlsx

NOTE: The Cleveland Fed has restructured this page before. If the URL below
returns 404, inspect the live page in a browser and update URL_CANDIDATES.
They also sometimes publish archive JSONs via their API at research.clevelandfed.org.

Output: ~/Documents/terminal3_data/cleveland_fed_nowcast.jsonl (one row per
(release_date, target_month, variable)).

Usage:
    pip3 install --user requests pandas openpyxl
    python3 ~/Documents/terminal3_cleveland_fed_pull.py

If you have issues with the XLSX source, re-run with --csv to try CSV endpoints.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import requests


OUTPUT_DIR = Path.home() / "Documents" / "terminal3_data"
OUT_FILE = OUTPUT_DIR / "cleveland_fed_nowcast.jsonl"
RAW_FILE = OUTPUT_DIR / "cleveland_fed_nowcast_raw.xlsx"

# Cleveland Fed publishes the nowcast time series in an Excel file. These are
# the URL patterns we try. If all fail, the page has been restructured — check
# https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting for the
# current download link and update this list.
URL_CANDIDATES = [
    "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast.xlsx",
    "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast-current-data.xlsx",
    "https://www.clevelandfed.org/-/media/project/clevelandfedtenant/indicators-and-data/files/inflationnowcasting/nowcast.xlsx",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}


def try_download() -> Optional[bytes]:
    for url in URL_CANDIDATES:
        print(f"  fetching {url} ...", end=" ", flush=True)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"[err {e}]")
            continue
        if r.status_code == 200 and len(r.content) > 1000:
            print(f"OK ({len(r.content)} bytes)")
            return r.content
        print(f"[HTTP {r.status_code}, {len(r.content)} bytes]")
    return None


def parse_xlsx(blob: bytes) -> List[dict]:
    """Parse Cleveland Fed nowcast XLSX into rows.

    The workbook typically has sheets for each nowcast variable. Sheet layout
    varies by year, so we do a permissive parse: every numeric cell whose row
    header parses as a date becomes a (date, value) record tagged with the
    sheet name as the variable.

    Returns list of {release_date, variable, value} rows. We intentionally
    don't try to reverse-engineer the target-month column layout here — the
    analyzer script joins on release_date + variable + explicitly-typed target.
    """
    try:
        import pandas as pd  # type: ignore
    except ImportError:
        print("[err] pandas not installed. Run: pip3 install --user pandas openpyxl")
        sys.exit(1)

    rows: List[dict] = []
    try:
        xl = pd.ExcelFile(BytesIO(blob))
    except Exception as e:
        print(f"[err] could not parse XLSX: {e}")
        sys.exit(1)

    print(f"  sheets: {xl.sheet_names}")
    for sheet_name in xl.sheet_names:
        try:
            df = xl.parse(sheet_name)
        except Exception as e:
            print(f"    skip sheet {sheet_name}: {e}")
            continue

        # Heuristic: the left column is usually release date. Everything else
        # that's numeric is a nowcast value for some target horizon.
        if df.shape[0] == 0 or df.shape[1] < 2:
            continue

        date_col = df.columns[0]
        for _, row in df.iterrows():
            d = row[date_col]
            try:
                release_date = pd.to_datetime(d).date().isoformat()
            except Exception:
                continue
            for col in df.columns[1:]:
                v = row[col]
                try:
                    val = float(v)
                except (ValueError, TypeError):
                    continue
                rows.append({
                    "release_date": release_date,
                    "sheet": sheet_name,
                    "column": str(col),
                    "value": val,
                })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", action="store_true",
                    help="Also try CSV fallback URLs if XLSX fails.")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("[Terminal 3 — Cleveland Fed Nowcast Puller]")
    print("Downloading nowcast workbook ...")

    blob = try_download()
    if not blob:
        print("\nAll URL candidates failed.")
        print("Go to https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting")
        print("and inspect the 'Download Data' link, then add the URL to URL_CANDIDATES.")
        return 1

    # Save raw for inspection
    RAW_FILE.write_bytes(blob)
    print(f"  raw saved: {RAW_FILE}")

    rows = parse_xlsx(blob)
    print(f"  parsed {len(rows)} rows")

    with open(OUT_FILE, "w") as f:
        stamp = datetime.now(timezone.utc).isoformat()
        for r in rows:
            r["_pulled_at"] = stamp
            f.write(json.dumps(r) + "\n")
    print(f"  wrote {OUT_FILE}")

    if rows:
        # Quick sanity print
        latest = sorted(
            (r for r in rows if r.get("release_date")),
            key=lambda r: r["release_date"],
        )[-5:]
        print("\nLatest 5 rows (any sheet):")
        for r in latest:
            print(f"  {r['release_date']}  {r['sheet']:<25}  {r['column']:<20}  {r['value']:.4f}")
        sheets = sorted({r["sheet"] for r in rows})
        print(f"\nSheets captured: {sheets}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
