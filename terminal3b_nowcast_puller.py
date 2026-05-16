"""Terminal 3b — Live Cleveland Fed Nowcast Puller.

Fetches the Cleveland Fed nowcast_year.json daily and appends the LATEST
CPI YoY nowcast for each open target month to a canonical JSONL.

Idempotent: if the latest nowcast value for a target month is unchanged
since last pull, no new row is appended.

Output:
    ~/Documents/terminal3b_data/nowcast_cleveland_fed.jsonl
        { _schema_version, target_year, target_month, subcaption,
          publish_date, observed_ts_utc, cpi_yoy, core_cpi_yoy,
          pce_yoy, core_pce_yoy }

Usage:
    # One-shot (typical — schedule daily via launchd / cron):
    python3 ~/Documents/terminal3b_nowcast_puller.py

    # Daemon (every 6h, useful during high-cadence periods near release):
    nohup caffeinate -is python3 ~/Documents/terminal3b_nowcast_puller.py \\
        --daemon --interval-sec 21600 > /dev/null 2>&1 &
"""

import argparse
import json
import signal
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests


CFED_URL = (
    "https://www.clevelandfed.org/-/media/files/webcharts/"
    "inflationnowcasting/nowcast_year.json"
)
DATA_DIR = Path.home() / "Documents" / "terminal3b_data"
OUT_PATH = DATA_DIR / "nowcast_cleveland_fed.jsonl"
LOG_PATH = DATA_DIR / "nowcast_puller.log"
SCHEMA_VERSION = "v1"
REQUEST_TIMEOUT = 60

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
    log("SIGINT — exiting after current pull.")


def _to_float(s) -> Optional[float]:
    if s is None or s == "" or s == "null":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _last_non_null(data: list) -> Tuple[Optional[float], Optional[str]]:
    """Return (value, label_mmdd) of the most recent non-null point in a
    Cleveland Fed series — using the per-point tooltext for the date label."""
    for d in reversed(data or []):
        v = _to_float(d.get("value"))
        if v is not None:
            tooltext = d.get("tooltext", "") or ""
            parts = tooltext.split("{br}")
            label = parts[1].strip() if len(parts) >= 2 and "/" in parts[1] else None
            return v, label
    return None, None


def parse_target_month(subcaption: str) -> Optional[Tuple[int, int]]:
    if not subcaption or "-" not in subcaption:
        return None
    try:
        y, m = subcaption.split("-", 1)
        return int(y), int(m)
    except ValueError:
        return None


def infer_publish_date(label: Optional[str], target_year: int, target_month: int) -> Optional[str]:
    """MM/DD label → ISO date. Picks the year (target_year or +1) that's
    closest to the BLS release window (~13th of target_month + 1)."""
    if not label or "/" not in label:
        return None
    try:
        mm, dd = label.split("/")
        mm, dd = int(mm), int(dd)
    except ValueError:
        return None
    rel_year = target_year + (1 if target_month == 12 else 0)
    rel_month = 1 if target_month == 12 else target_month + 1
    canonical = date(rel_year, rel_month, 13)
    best, best_dist = None, 9999
    for y in (target_year, target_year + 1):
        try:
            cand = date(y, mm, dd)
        except ValueError:
            continue
        d = abs((cand - canonical).days)
        if d < best_dist and d <= 60:
            best, best_dist = cand, d
    return best.isoformat() if best else None


def existing_latest_per_target() -> Dict[Tuple[int, int], dict]:
    """Build {(target_year, target_month): last_record} from the existing
    output, so we can dedupe new pulls if the value hasn't changed."""
    out: Dict[Tuple[int, int], dict] = {}
    if not OUT_PATH.exists():
        return out
    try:
        with open(OUT_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = (r.get("target_year"), r.get("target_month"))
                if k[0] is None or k[1] is None:
                    continue
                if k not in out or r.get("publish_date", "") > out[k].get("publish_date", ""):
                    out[k] = r
    except OSError:
        pass
    return out


def pull_once() -> int:
    """Pull Cleveland Fed JSON, write any new latest values per target month.
    Returns count of new rows appended."""
    log("Fetching Cleveland Fed nowcast JSON...")
    try:
        r = requests.get(
            CFED_URL,
            timeout=REQUEST_TIMEOUT,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log(f"  [error] fetch failed: {e}")
        return 0
    archive = r.json()
    log(f"  ok: {len(archive)} charts.")

    # Collect latest per target month from this fetch
    latest_records: List[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for chart in archive:
        sub = chart.get("chart", {}).get("subcaption", "")
        tm = parse_target_month(sub)
        if not tm:
            continue
        target_year, target_month = tm
        ds = chart.get("dataset", [])
        if len(ds) < 4:
            continue
        cpi_v, cpi_label = _last_non_null(ds[0].get("data") or [])
        if cpi_v is None:
            continue   # no nowcast yet for this target

        pdt = infer_publish_date(cpi_label, target_year, target_month)
        rec = {
            "_schema_version": SCHEMA_VERSION,
            "target_year": target_year,
            "target_month": target_month,
            "subcaption": sub,
            "label_mmdd": cpi_label,
            "publish_date": pdt,
            "observed_ts_utc": now_iso,
            "cpi_yoy": cpi_v,
            "core_cpi_yoy": _last_non_null(ds[1].get("data") or [])[0] if len(ds) > 1 else None,
            "pce_yoy": _last_non_null(ds[2].get("data") or [])[0] if len(ds) > 2 else None,
            "core_pce_yoy": _last_non_null(ds[3].get("data") or [])[0] if len(ds) > 3 else None,
        }
        latest_records.append(rec)

    log(f"  latest nowcast extracted for {len(latest_records)} target months.")

    # Dedup vs existing — append only if value changed for that target
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    prior = existing_latest_per_target()
    new_count = 0
    with open(OUT_PATH, "a") as f:
        for r in latest_records:
            k = (r["target_year"], r["target_month"])
            prev = prior.get(k)
            if prev is not None:
                same_pdt = prev.get("publish_date") == r["publish_date"]
                same_val = abs((prev.get("cpi_yoy") or 0) - r["cpi_yoy"]) < 1e-6
                if same_pdt and same_val:
                    continue
            f.write(json.dumps(r) + "\n")
            new_count += 1

    log(f"  appended {new_count} new/changed rows.")
    return new_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daemon", action="store_true", help="loop forever")
    ap.add_argument("--interval-sec", type=int, default=21600,
                    help="daemon poll interval (default 6h)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    log(f"T3b Cleveland Fed Puller starting. daemon={args.daemon} "
        f"interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            pull_once()
        except Exception as e:
            log(f"[error] pull_once: {type(e).__name__}: {e}")
        if not args.daemon or _STOP:
            break
        end = time.time() + args.interval_sec
        while time.time() < end and not _STOP:
            time.sleep(min(1.0, end - time.time()))

    log(f"Puller stopped. Loops: {loops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
