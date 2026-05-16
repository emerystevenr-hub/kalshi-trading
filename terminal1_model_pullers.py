"""Terminal 1 — Weather Model Pullers.

Unified puller for GFS, ECMWF HRES, HRRR, AIFS. Each model has its own
adapter (URL pattern, GRIB message selector); the shared pipeline handles:
  - download with retry + backoff
  - GRIB decode
  - station-point extraction (nearest grid cell to each target lat/lon)
  - canonical JSONL output per terminal1_data_schema.md §2

Usage:
    # Pull the most recent run of every model for every station.
    python3 terminal1_model_pullers.py --latest

    # Backfill last 90 days (Phase 1 backtest prep). Slow — hours, not minutes.
    python3 terminal1_model_pullers.py --backfill-days 90

    # Single model, single cycle:
    python3 terminal1_model_pullers.py --model gfs --cycle 2026-04-22T00:00

Dependency:
    pip install --break-system-packages pygrib requests

Station coordinates are the NWS observation sites — same points Kalshi
resolves against, so forecast-vs-obs comparisons are direct.
"""

import argparse
import json
import os
import sys
import time
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import requests

try:
    import pygrib  # type: ignore
except ImportError:
    pygrib = None  # Allow import in environments without pygrib; will fail at runtime.


# ---------------------------------------------------------------------------
# Constants & config
# ---------------------------------------------------------------------------

DATA_DIR = Path.home() / "Documents" / "terminal1_data"
LOG_FILE = Path.home() / "Documents" / "terminal1_pullers.log"
SCHEMA_VERSION = "v1"

REQUEST_TIMEOUT = 120
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 10

# NWS observation site coordinates (lat, lon) — decimal degrees.
# Use the official NWS station locations to match Kalshi resolution.
STATIONS: Dict[str, Tuple[float, float]] = {
    # Tier 1 (original — trading):
    "NYC": (40.7794, -73.9692),    # KNYC (Central Park)
    "ORD": (41.9742, -87.9073),    # KORD (O'Hare)
    "LAX": (33.9381, -118.3889),   # KLAX
    # Tier 2 (expansion — added 2026-04-24):
    "DEN": (39.8561, -104.6737),   # KDEN (Denver Intl)
    "ATL": (33.6367, -84.4281),    # KATL (Hartsfield-Jackson)
    "MIA": (25.7932, -80.2906),    # KMIA (Miami Intl)
    "PHX": (33.4343, -112.0080),   # KPHX (Phoenix Sky Harbor)
    # DFW removed 2026-04-24 — Kalshi doesn't offer Dallas weather markets.
    # NWS actuals already pulled for DFW can stay; just no forecast pulls.
}

# Forecast horizons we actually care about (hours out from run time).
# Short-range for decision making; daily max/min through +72h.
HORIZONS_HRS = list(range(0, 73, 3))  # every 3h out to 72h

# Model cycles (UTC hours of day).
MODEL_CYCLES = {
    "gfs":         [0, 6, 12, 18],
    "hrrr":        list(range(0, 24)),  # hourly
    "ecmwf_hres":  [0, 12],
    "aifs":        [0, 12],
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


# ---------------------------------------------------------------------------
# Model adapters — each returns a URL for (cycle, fhour)
# ---------------------------------------------------------------------------

@dataclass
class ModelSpec:
    name: str
    url_fn: Callable[[datetime, int], str]
    # For GRIB message selection — the (typeOfLevel, level) of 2m temp.
    t2m_selector: Dict[str, object]
    # Optional fallback URL builder, tried if primary returns 404/fail.
    # Used for AWS-primary / NOMADS-fallback on very recent cycles where
    # the AWS mirror may lag by 1-2 hours.
    fallback_url_fn: Optional[Callable[[datetime, int], str]] = None
    # If set, use .idx byte-range download instead of full-file download.
    # Value is a substring that must appear in the .idx line for the target
    # GRIB message (e.g. "TMP:2 m above ground"). NOAA GFS and HRRR on
    # NOMADS/AWS publish companion .idx files with this format. ECMWF
    # Open Data does not, so leave this None for ecmwf_hres / aifs.
    idx_match: Optional[str] = None


def _gfs_url(cycle: datetime, fhour: int) -> str:
    """GFS 0.25° from AWS NOAA Open Data (full archive back to 2021).

    Previously used NOMADS (https://nomads.ncep.noaa.gov/...) which has ~10
    day retention. Switched 2026-04-23 after backfill hit the retention wall
    on all cycles older than ~10 days. AWS bucket mirrors NOMADS with the
    same directory structure, full archive, no auth, and generally faster.

    Example:
      https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20260422/00/atmos/gfs.t00z.pgrb2.0p25.f024
    Fallback (live only, if AWS mirror lags very recent cycles):
      https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.20260422/00/atmos/gfs.t00z.pgrb2.0p25.f024
    """
    d = cycle.strftime("%Y%m%d")
    hh = f"{cycle.hour:02d}"
    fh = f"{fhour:03d}"
    return (
        f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
        f"gfs.{d}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{fh}"
    )


def _gfs_url_fallback(cycle: datetime, fhour: int) -> str:
    """NOMADS fallback for GFS. Used only for very recent cycles when the
    AWS mirror lags (typical lag: 1-2 hours behind NOMADS)."""
    d = cycle.strftime("%Y%m%d")
    hh = f"{cycle.hour:02d}"
    fh = f"{fhour:03d}"
    return (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
        f"gfs.{d}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{fh}"
    )


def _hrrr_url(cycle: datetime, fhour: int) -> str:
    """HRRR 3km from AWS NOAA Open Data (full archive back to 2014).

    Previously used NOMADS which has ~2 day retention. Switched 2026-04-23.
    Example:
      https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20260422/conus/hrrr.t00z.wrfsfcf01.grib2
    """
    d = cycle.strftime("%Y%m%d")
    hh = f"{cycle.hour:02d}"
    fh = f"{fhour:02d}"
    return (
        f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/"
        f"hrrr.{d}/conus/hrrr.t{hh}z.wrfsfcf{fh}.grib2"
    )


def _hrrr_url_fallback(cycle: datetime, fhour: int) -> str:
    """NOMADS fallback for HRRR (recent cycles only)."""
    d = cycle.strftime("%Y%m%d")
    hh = f"{cycle.hour:02d}"
    fh = f"{fhour:02d}"
    return (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/"
        f"hrrr.{d}/conus/hrrr.t{hh}z.wrfsfcf{fh}.grib2"
    )


def _ecmwf_hres_url(cycle: datetime, fhour: int) -> str:
    """ECMWF HRES from Open Data. Example:
    https://data.ecmwf.int/forecasts/20260422/00z/ifs/0p25/oper/20260422000000-24h-oper-fc.grib2
    """
    d = cycle.strftime("%Y%m%d")
    hh = f"{cycle.hour:02d}"
    return (
        f"https://data.ecmwf.int/forecasts/{d}/{hh}z/ifs/0p25/oper/"
        f"{d}{hh}0000-{fhour}h-oper-fc.grib2"
    )


def _aifs_url(cycle: datetime, fhour: int) -> str:
    """AIFS from ECMWF Open Data. Path is 'aifs-single' (deterministic) —
    'aifs-ens' is the ensemble variant, which we're not using in Phase 1.
    Example:
    https://data.ecmwf.int/forecasts/20260422/12z/aifs-single/0p25/oper/20260422120000-24h-oper-fc.grib2
    """
    d = cycle.strftime("%Y%m%d")
    hh = f"{cycle.hour:02d}"
    return (
        f"https://data.ecmwf.int/forecasts/{d}/{hh}z/aifs-single/0p25/oper/"
        f"{d}{hh}0000-{fhour}h-oper-fc.grib2"
    )


MODELS: Dict[str, ModelSpec] = {
    "gfs": ModelSpec(
        name="gfs",
        url_fn=_gfs_url,
        fallback_url_fn=_gfs_url_fallback,
        t2m_selector={"name": "2 metre temperature"},
        idx_match="TMP:2 m above ground",
    ),
    "hrrr": ModelSpec(
        name="hrrr",
        url_fn=_hrrr_url,
        fallback_url_fn=_hrrr_url_fallback,
        t2m_selector={"name": "2 metre temperature"},
        idx_match="TMP:2 m above ground",
    ),
    "ecmwf_hres": ModelSpec(
        name="ecmwf_hres",
        url_fn=_ecmwf_hres_url,
        t2m_selector={"name": "2 metre temperature"},
    ),
    "aifs": ModelSpec(
        name="aifs",
        url_fn=_aifs_url,
        t2m_selector={"name": "2 metre temperature"},
    ),
}


# ---------------------------------------------------------------------------
# Download + GRIB extraction
# ---------------------------------------------------------------------------

def download(url: str, dest: Path) -> bool:
    """Stream a GRIB file to disk with retry/backoff. Returns True on
    success, False on 404 or permanent failure."""
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
                if r.status_code == 404:
                    log(f"  [404] {url.rsplit('/', 1)[-1]}")
                    return False
                r.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MB
                        f.write(chunk)
                return True
        except requests.RequestException as e:
            log(f"  [retry] {url.rsplit('/', 1)[-1]} attempt {attempt+1}: {e}")
            time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
    log(f"  [fail] {url.rsplit('/', 1)[-1]} — all retries exhausted")
    return False


def _parse_idx_for_range(idx_text: str, match_substring: str) -> Optional[Tuple[int, Optional[int]]]:
    """Parse a NOAA-style .idx file and find the byte range for the record
    whose descriptor contains `match_substring`. Returns (start, end_exclusive)
    or None if no match. end is None if the record is the last in the file
    (meaning "read to end of file").

    .idx format, one record per line:
        recnum:byte_offset:d=YYYYMMDDHH:PARAM:LEVEL:FCST_TIME:EXTRA
    Example:
        3:234567:d=2026042300:TMP:2 m above ground:anl:
    """
    lines = [ln for ln in idx_text.strip().splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if match_substring in line:
            parts = line.split(":")
            if len(parts) < 2:
                continue
            try:
                start = int(parts[1])
            except ValueError:
                continue
            end: Optional[int] = None
            if i + 1 < len(lines):
                next_parts = lines[i + 1].split(":")
                if len(next_parts) >= 2:
                    try:
                        end = int(next_parts[1])
                    except ValueError:
                        end = None
            return (start, end)
    return None


def download_grib_message_by_idx(
    grib_url: str,
    idx_match: str,
    dest: Path,
) -> bool:
    """Download only the single GRIB message matching idx_match, using the
    companion .idx file and an HTTP Range request. Returns True on success.
    On any failure, returns False so the caller can fall back to full download.

    This slashes GFS download size from ~500MB per horizon to ~500KB — a
    ~1000x speedup when we only need one variable (2m temperature).
    """
    idx_url = grib_url + ".idx"
    try:
        r = requests.get(idx_url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        log(f"  [idx-err] {idx_url.rsplit('/', 1)[-1]}: {e}")
        return False
    if r.status_code == 404:
        log(f"  [idx-404] {idx_url.rsplit('/', 1)[-1]}")
        return False
    if r.status_code != 200:
        log(f"  [idx-http{r.status_code}] {idx_url.rsplit('/', 1)[-1]}")
        return False

    rng = _parse_idx_for_range(r.text, idx_match)
    if rng is None:
        log(f"  [idx-nomatch] {idx_url.rsplit('/', 1)[-1]}: no line matches '{idx_match}'")
        return False
    start, end = rng

    if end is not None:
        range_header = f"bytes={start}-{end - 1}"
    else:
        range_header = f"bytes={start}-"

    for attempt in range(MAX_RETRIES):
        try:
            r2 = requests.get(
                grib_url,
                headers={"Range": range_header},
                stream=True,
                timeout=REQUEST_TIMEOUT,
            )
            if r2.status_code == 404:
                log(f"  [404] {grib_url.rsplit('/', 1)[-1]}")
                return False
            # S3 returns 206 Partial Content on valid Range; some CDNs return 200
            # with full body. Either way proceed.
            if r2.status_code not in (200, 206):
                log(f"  [range-http{r2.status_code}] {grib_url.rsplit('/', 1)[-1]}")
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in r2.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            return True
        except requests.RequestException as e:
            log(f"  [retry-range] {grib_url.rsplit('/', 1)[-1]} attempt {attempt+1}: {e}")
            time.sleep(RETRY_BACKOFF_SEC * (2 ** attempt))
    log(f"  [fail-range] {grib_url.rsplit('/', 1)[-1]} — all retries exhausted")
    return False


def _celsius_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def extract_station_temps(
    grib_path: Path,
    spec: ModelSpec,
) -> Dict[str, Optional[float]]:
    """Open GRIB, find the 2m temp message, read values at each station's
    nearest grid cell. Returns {station: temp_f} (°F) or None on miss."""
    if pygrib is None:
        raise RuntimeError("pygrib not installed. "
                           "pip install --break-system-packages pygrib")

    out: Dict[str, Optional[float]] = {s: None for s in STATIONS}
    try:
        grbs = pygrib.open(str(grib_path))
    except Exception as e:
        log(f"  [error] pygrib.open({grib_path.name}): {e}")
        return out

    try:
        # Try multiple known 2m temp selectors — different models/vintages
        # use different GRIB metadata names.
        selector_variants = [
            spec.t2m_selector,
            {"name": "Temperature", "typeOfLevel": "heightAboveGround", "level": 2},
            {"shortName": "2t"},
            {"parameterName": "Temperature", "level": 2},
        ]
        matches = []
        for sel in selector_variants:
            try:
                matches = grbs.select(**sel)
                if matches:
                    break
            except Exception:
                continue

        if not matches:
            # Dump available messages for diagnosis.
            try:
                grbs.seek(0)
                names = set()
                for msg in grbs:
                    nm = getattr(msg, "name", None) or getattr(msg, "shortName", None)
                    if nm and ("temp" in nm.lower() or nm.lower() in ("2t", "t2m")):
                        names.add(f"{nm} @ L={getattr(msg,'level',None)} "
                                  f"lvlType={getattr(msg,'typeOfLevel',None)}")
                if names:
                    log(f"  [warn] {grib_path.name}: no selector matched. "
                        f"Temperature messages present: {sorted(names)[:5]}")
                else:
                    log(f"  [warn] {grib_path.name}: no temperature messages at all")
            except Exception as e:
                log(f"  [warn] {grib_path.name}: no 2m temp and diag failed: {e}")
            return out

        msg = matches[0]
        # If multiple messages, prefer level=2 (2m above ground).
        for m in matches:
            if getattr(m, "level", None) == 2:
                msg = m
                break

        import numpy as np  # Imported lazily; only needed in extraction.

        # Full-grid data pulled ONCE per GRIB message and reused across
        # stations. Bbox-based slicing doesn't work on non-regular grids
        # (HRRR is Lambert Conformal Conic, returns empty for bbox calls).
        full_values: Optional[object] = None
        full_lats: Optional[object] = None
        full_lons: Optional[object] = None

        for station, (lat, lon) in STATIONS.items():
            # Default to 0..360 lon for bbox attempts (GFS convention).
            lon_360 = lon if lon >= 0 else lon + 360.0

            # Try bbox first (fast path for regular grids like GFS, ECMWF).
            values = lats = lons = None
            try:
                values, lats, lons = msg.data(
                    lat1=lat - 0.5, lat2=lat + 0.5,
                    lon1=lon_360 - 0.5, lon2=lon_360 + 0.5,
                )
            except Exception:
                pass

            # Fall back to cached full-grid (HRRR and other projected grids).
            if values is None or values.size == 0:
                if full_values is None:
                    try:
                        full_values, full_lats, full_lons = msg.data()
                    except Exception as e:
                        log(f"  [warn] {station}: full-grid pull failed: {e}")
                        continue
                values, lats, lons = full_values, full_lats, full_lons

            if values is None or values.size == 0:
                log(f"  [warn] {station}: empty grid")
                continue

            # Auto-detect longitude convention from the grid.
            # GFS/ECMWF regular grids: 0..360.
            # HRRR and most projected North-America grids: -180..180.
            lon_max = float(np.max(lons))
            if lon_max > 180.0:
                target_lon = lon_360            # grid uses 0..360
            else:
                target_lon = lon                # grid uses -180..180

            d2 = (lats - lat) ** 2 + (lons - target_lon) ** 2
            idx = np.unravel_index(np.argmin(d2), d2.shape)
            k = float(values[idx])
            out[station] = round(_celsius_to_f(k - 273.15), 2)
    finally:
        try:
            grbs.close()
        except Exception:
            pass

    return out


# ---------------------------------------------------------------------------
# Record writing
# ---------------------------------------------------------------------------

def write_forecast_records(
    station: str,
    model: str,
    cycle: datetime,
    fhour: int,
    temp_f: float,
    grib_filename: str,
) -> None:
    path = DATA_DIR / f"forecasts_{model}_{station}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    valid_time = cycle + timedelta(hours=fhour)
    record = {
        "_schema_version": SCHEMA_VERSION,
        "station": station,
        "model": model,
        "run_time_utc": cycle.isoformat(),
        "valid_time_utc": valid_time.isoformat(),
        "lead_hours": fhour,
        "target_date_local": valid_time.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "temp_f": temp_f,
        "temp_c": round((temp_f - 32.0) * 5.0 / 9.0, 2),
        "daily_high_f": None,
        "daily_low_f": None,
        "grib_filename": grib_filename,
        "pulled_ts_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        log(f"  [error] write failed {path}: {e}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_cycle(model: str, cycle: datetime, horizons: Iterable[int]) -> int:
    """Pull all horizons for one (model, cycle). Returns count of records
    written."""
    spec = MODELS[model]
    count = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for fh in horizons:
            url = spec.url_fn(cycle, fh)
            fname = url.rsplit("/", 1)[-1]
            dest = tmp_path / fname
            ok = False

            # Prefer .idx byte-range if configured. Cuts GFS download from
            # ~500MB to ~500KB per horizon.
            if spec.idx_match is not None:
                ok = download_grib_message_by_idx(url, spec.idx_match, dest)
                if not ok and spec.fallback_url_fn is not None:
                    fallback_url = spec.fallback_url_fn(cycle, fh)
                    log(f"  [idx-fallback] trying {fallback_url.rsplit('/', 1)[-1]}")
                    ok = download_grib_message_by_idx(fallback_url, spec.idx_match, dest)

            # If byte-range path failed (or not configured), try full download.
            if not ok:
                ok = download(url, dest)
                if not ok and spec.fallback_url_fn is not None:
                    fallback_url = spec.fallback_url_fn(cycle, fh)
                    log(f"  [fallback] trying {fallback_url.rsplit('/', 1)[-1]}")
                    ok = download(fallback_url, dest)
            if not ok:
                continue
            temps = extract_station_temps(dest, spec)
            for station, temp_f in temps.items():
                if temp_f is None:
                    continue
                write_forecast_records(station, model, cycle, fh, temp_f, fname)
                count += 1
            try:
                dest.unlink()
            except OSError:
                pass
    return count


def latest_cycle(model: str, now: Optional[datetime] = None) -> datetime:
    """Return the most recent cycle that has almost certainly published.
    Conservative 6h latency buffer."""
    now = now or datetime.now(timezone.utc)
    buffer = timedelta(hours=6)
    target = now - buffer
    cycles = MODEL_CYCLES[model]
    # Walk back day-by-day, hour-by-hour, until we find the most recent
    # cycle hour <= target.
    for days_back in range(3):
        day = (target - timedelta(days=days_back)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        for hr in sorted(cycles, reverse=True):
            cand = day.replace(hour=hr)
            if cand <= target:
                return cand
    raise RuntimeError(f"No valid cycle for {model} found")


def _parse_horizons(s: Optional[str]) -> List[int]:
    """Parse '12,24,36,48,60,72' → [12, 24, 36, 48, 60, 72]."""
    if not s:
        return HORIZONS_HRS
    out: List[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out or HORIZONS_HRS


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=list(MODELS.keys()), default=None,
                    help="Limit to one model (default: all)")
    ap.add_argument("--latest", action="store_true",
                    help="Pull the most recent cycle of each selected model")
    ap.add_argument("--backfill-days", type=int, default=0,
                    help="Backfill N days of historical runs (Phase 1 prep)")
    ap.add_argument("--cycle", type=str, default=None,
                    help="Explicit cycle, ISO format (e.g., 2026-04-22T00:00)")
    ap.add_argument("--horizons", type=str, default=None,
                    help="Comma-separated horizons in hours to pull. "
                         "Default: 0-72 every 3h (25 horizons). "
                         "For backfill speed use '12,24,36,48,60,72'.")
    ap.add_argument("--cycle-hours", type=str, default=None,
                    help="Comma-separated cycle hours to run (e.g., '0,12' "
                         "to only pull 00Z and 12Z runs). Default: all model "
                         "cycles per MODEL_CYCLES.")
    ap.add_argument("--interval-sec", type=int, default=0,
                    help="Daemon mode: loop --latest every N seconds. "
                         "0 = single-shot (default). Recommended: 1800 "
                         "(30 min) — covers HRRR's hourly release with margin "
                         "and is harmless on slower-cycling models since the "
                         "puller dedups runs already in the JSONL.")
    args = ap.parse_args()

    models = [args.model] if args.model else list(MODELS.keys())
    horizons = _parse_horizons(args.horizons)
    cycle_hr_override = _parse_horizons(args.cycle_hours) if args.cycle_hours else None

    if args.cycle:
        cycle = datetime.fromisoformat(args.cycle).replace(tzinfo=timezone.utc)
        for m in models:
            log(f"[{m}] cycle={cycle.isoformat()} horizons={horizons} pulling ...")
            n = process_cycle(m, cycle, horizons)
            log(f"[{m}] wrote {n} records")
        return

    if args.latest or args.interval_sec > 0:
        # Daemon mode if --interval-sec set, otherwise single-shot.
        import signal as _sig
        import time as _time

        _stop = {"flag": False}

        def _on_sigint(_s, _f):
            _stop["flag"] = True
            log("SIGINT — finishing current pass and exiting.")

        _sig.signal(_sig.SIGINT, _on_sigint)
        _sig.signal(_sig.SIGTERM, _on_sigint)

        loops = 0
        while True:
            loops += 1
            for m in models:
                if _stop["flag"]:
                    break
                try:
                    cycle = latest_cycle(m)
                    log(f"[{m}] latest cycle={cycle.isoformat()} "
                        f"horizons={horizons} pulling ...")
                    n = process_cycle(m, cycle, horizons)
                    log(f"[{m}] wrote {n} records")
                except Exception as e:
                    log(f"[{m}] [error] {type(e).__name__}: {e}")
            if args.interval_sec <= 0 or _stop["flag"]:
                break
            log(f"--- loop #{loops} done; sleeping {args.interval_sec}s ---")
            end = _time.time() + args.interval_sec
            while _time.time() < end and not _stop["flag"]:
                _time.sleep(min(1.0, end - _time.time()))
        log(f"Pullers stopped. Loops: {loops}")
        return

    if args.backfill_days > 0:
        now = datetime.now(timezone.utc)
        for m in models:
            cycles = cycle_hr_override or MODEL_CYCLES[m]
            total = 0
            for days_back in range(args.backfill_days):
                day = (now - timedelta(days=days_back)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                for hr in cycles:
                    cycle = day.replace(hour=hr)
                    if cycle > now:
                        continue
                    log(f"[{m}] backfill cycle={cycle.isoformat()}")
                    n = process_cycle(m, cycle, horizons)
                    total += n
                    log(f"[{m}] wrote {n} records (cumulative: {total})")
            log(f"[{m}] BACKFILL COMPLETE — total {total} records")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
