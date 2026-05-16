"""Terminal 1 — METAR + TAF Puller (Phase 3 data layer).

Pulls real-time airport observations (METAR) and Terminal Aerodrome Forecasts
(TAF) for the stations we trade. Appends to JSONL for historical analysis +
Phase 3 late-execution signal fusion.

Data source: aviationweather.gov — NWS official, free, no auth, JSON API.

Endpoints:
  METAR: https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json&hours=6
  TAF:   https://aviationweather.gov/api/data/taf?ids={ICAO}&format=json

Outputs:
  ~/Documents/terminal1_data/metar_{station}.jsonl    — append-only obs log
  ~/Documents/terminal1_data/taf_{station}.jsonl      — append-only forecast log
  ~/Documents/terminal1_data/metar_taf_puller.log     — puller activity log

Usage:
  # One shot for testing:
  python3 ~/Documents/terminal1_metar_taf_puller.py --once

  # Daemonize (default 15 min interval):
  nohup caffeinate -is python3 ~/Documents/terminal1_metar_taf_puller.py \\
      > /dev/null 2>&1 &

  # Tighter cadence / more stations:
  python3 ~/Documents/terminal1_metar_taf_puller.py \\
      --interval-sec 600 --stations NYC,ORD,LAX,DEN,ATL
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests


BASE_METAR = "https://aviationweather.gov/api/data/metar"
BASE_TAF = "https://aviationweather.gov/api/data/taf"
REQUEST_TIMEOUT = 30

DATA_DIR = Path.home() / "Documents" / "terminal1_data"
LOG_PATH = DATA_DIR / "metar_taf_puller.log"

# Kalshi station code → ICAO airport identifier.
STATION_TO_ICAO = {
    "NYC": "KNYC",   # Central Park
    "ORD": "KORD",   # Chicago O'Hare
    "LAX": "KLAX",   # Los Angeles
    # Tier 2 stations (for future expansion):
    "DEN": "KDEN",
    "ATL": "KATL",
    "MIA": "KMIA",
    "PHX": "KPHX",
    "DFW": "KDFW",
}

DEFAULT_STATIONS = ["NYC", "ORD", "LAX"]
DEFAULT_INTERVAL_SEC = 900   # 15 min

_STOP = False


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


def _handle_sigint(sig, frame):
    global _STOP
    _STOP = True
    log("SIGINT — finishing current cycle.")


# --------------------------------------------------------------------------
# METAR fetch + parse
# --------------------------------------------------------------------------

def fetch_metar(icao: str, hours: int = 6) -> List[dict]:
    """Return list of METAR observation dicts from last `hours` hours.

    Response schema (aviationweather.gov /data/metar JSON):
    [
      {
        "metar_id": int, "icaoId": "KNYC", "receiptTime": "...",
        "obsTime": 1714000000 (unix), "reportTime": "...",
        "temp": 12.3 (°C), "dewp": 5.1 (°C),
        "wdir": 270, "wspd": 12 (kt), "wgst": null (gust kt),
        "visib": "10+", "altim": 1015.2, "slp": null,
        "wxString": null, "presTend": null,
        "rawOb": "KNYC 241551Z 27012KT 10SM FEW050 12/05 A2999",
        ...
      },
      ...
    ]
    """
    try:
        r = requests.get(
            BASE_METAR,
            params={"ids": icao, "format": "json", "hours": hours},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        log(f"  [err] METAR {icao}: {e}")
        return []
    if r.status_code != 200:
        log(f"  [http{r.status_code}] METAR {icao}")
        return []
    try:
        data = r.json()
    except json.JSONDecodeError:
        log(f"  [parse-err] METAR {icao}")
        return []
    return data if isinstance(data, list) else []


def fetch_taf(icao: str) -> List[dict]:
    """Return list of TAF forecast dicts (typically 1 current TAF per station).

    Response schema:
    [
      {
        "tafId": int, "icaoId": "KNYC",
        "issueTime": "2026-04-24T12:00:00Z", "bulletinTime": "...",
        "validTimeFrom": unix, "validTimeTo": unix,
        "rawTAF": "TAF KNYC 241120Z 2412/2512 ...",
        "fcsts": [
          {
            "timeFrom": unix, "timeTo": unix,
            "fcstChange": "FM", "wdir": 270, "wspd": 10,
            "temp": [{ "validTime": unix, "sfcTemp": 18 (°C), "maxMinTemp": null }, ...],
            ...
          }, ...
        ]
      }
    ]
    """
    try:
        r = requests.get(
            BASE_TAF,
            params={"ids": icao, "format": "json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        log(f"  [err] TAF {icao}: {e}")
        return []
    if r.status_code != 200:
        log(f"  [http{r.status_code}] TAF {icao}")
        return []
    try:
        data = r.json()
    except json.JSONDecodeError:
        log(f"  [parse-err] TAF {icao}")
        return []
    return data if isinstance(data, list) else []


def _c_to_f(c) -> Optional[float]:
    """Celsius → Fahrenheit, safe for None / non-numeric."""
    if c is None:
        return None
    try:
        return round(float(c) * 9.0 / 5.0 + 32.0, 2)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------

def write_metar_records(station: str, icao: str, obs: List[dict]) -> int:
    """Append METAR obs to metar_{station}.jsonl. Dedupe by obsTime.

    Dedup strategy: read the last 2000 lines of the file (enough for any
    practical pull window — METAR runs ~2 per hour, so 2000 lines = ~6 weeks
    of history, which far exceeds any "hours=6" pull + realistic downtime).
    This is robust against: multi-hour process downtime, restart storms, and
    non-sequential obsTime arrivals."""
    path = DATA_DIR / f"metar_{station}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    seen_times = set()
    if path.exists():
        try:
            with open(path) as f:
                lines = f.readlines()
            # Enlarged window from 200 → 2000: covers any realistic downtime.
            for line in lines[-2000:]:
                try:
                    r = json.loads(line)
                    ot = r.get("obs_time_unix")
                    if ot is not None:
                        seen_times.add(int(ot))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    pulled_ts = datetime.now(timezone.utc).isoformat()
    written = 0
    with open(path, "a") as f:
        for o in obs:
            ot = o.get("obsTime")
            if ot is None or int(ot) in seen_times:
                continue
            row = {
                "_schema_version": "v1",
                "pulled_ts_utc": pulled_ts,
                "station": station,
                "icao": icao,
                "obs_time_unix": int(ot),
                "obs_time_utc": datetime.fromtimestamp(int(ot), tz=timezone.utc).isoformat(),
                "temp_c": o.get("temp"),
                "temp_f": _c_to_f(o.get("temp")),
                "dewp_c": o.get("dewp"),
                "dewp_f": _c_to_f(o.get("dewp")),
                "wind_dir_deg": o.get("wdir"),
                "wind_speed_kt": o.get("wspd"),
                "wind_gust_kt": o.get("wgst"),
                "visib": o.get("visib"),
                "altim_hpa": o.get("altim"),
                "slp_hpa": o.get("slp"),
                "wx_string": o.get("wxString"),
                "raw_metar": o.get("rawOb") or o.get("rawMetar"),
            }
            f.write(json.dumps(row) + "\n")
            written += 1
            seen_times.add(int(ot))
    return written


def write_taf_records(station: str, icao: str, tafs: List[dict]) -> int:
    """Append current TAF to taf_{station}.jsonl. Dedupe by issueTime."""
    path = DATA_DIR / f"taf_{station}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    # TAFs issue 4x/day, amendments occasional. Even 500 lines = ~3 months.
    seen_issues = set()
    if path.exists():
        try:
            with open(path) as f:
                lines = f.readlines()
            for line in lines[-500:]:
                try:
                    r = json.loads(line)
                    it = r.get("issue_time_utc")
                    if it:
                        seen_issues.add(it)
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    pulled_ts = datetime.now(timezone.utc).isoformat()
    written = 0
    with open(path, "a") as f:
        for t in tafs:
            issue = t.get("issueTime")
            if issue is None or issue in seen_issues:
                continue
            row = {
                "_schema_version": "v1",
                "pulled_ts_utc": pulled_ts,
                "station": station,
                "icao": icao,
                "issue_time_utc": issue,
                "bulletin_time_utc": t.get("bulletinTime"),
                "valid_from_unix": t.get("validTimeFrom"),
                "valid_to_unix": t.get("validTimeTo"),
                "fcsts": t.get("fcsts"),
                "raw_taf": t.get("rawTAF"),
            }
            f.write(json.dumps(row) + "\n")
            written += 1
            seen_issues.add(issue)
    return written


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def run_once(stations: List[str]) -> dict:
    pulled = {"metar_total": 0, "taf_total": 0, "per_station": {}}
    for st in stations:
        icao = STATION_TO_ICAO.get(st)
        if not icao:
            log(f"  [skip] no ICAO mapping for station {st}")
            continue

        obs = fetch_metar(icao)
        n_met = write_metar_records(st, icao, obs) if obs else 0

        tafs = fetch_taf(icao)
        n_taf = write_taf_records(st, icao, tafs) if tafs else 0

        log(f"  [{st:<4} {icao}]  metar_new={n_met:>2}  (fetched {len(obs)})  "
            f"taf_new={n_taf}  (fetched {len(tafs)})")
        pulled["metar_total"] += n_met
        pulled["taf_total"] += n_taf
        pulled["per_station"][st] = {"metar_new": n_met, "taf_new": n_taf}

    return pulled


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC,
                    help=f"seconds between cycles (default {DEFAULT_INTERVAL_SEC})")
    ap.add_argument("--once", action="store_true", help="one cycle then exit")
    ap.add_argument("--stations", default=",".join(DEFAULT_STATIONS),
                    help=f"comma-separated station codes (default {','.join(DEFAULT_STATIONS)})")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()]
    log(f"METAR/TAF Puller starting. stations={stations}  interval={args.interval_sec}s")

    cycles = 0
    while True:
        cycles += 1
        t0 = time.time()
        try:
            summary = run_once(stations)
            log(f"cycle #{cycles}: metar_new={summary['metar_total']}  "
                f"taf_new={summary['taf_total']}  "
                f"in {time.time()-t0:.1f}s")
        except Exception as e:
            log(f"[error] run_once: {type(e).__name__}: {e}")

        if args.once or _STOP:
            break
        end = time.time() + args.interval_sec
        while time.time() < end and not _STOP:
            time.sleep(min(1.0, end - time.time()))

    log(f"Stopped. Cycles: {cycles}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
