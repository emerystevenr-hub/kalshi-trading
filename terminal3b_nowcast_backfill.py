"""Terminal 3b — Cleveland Fed Nowcast Historical Backfill + σ Fit.

Pulls the full Cleveland Fed inflation-nowcasting archive (~150 target months
back to 2013-7) from their public JSON endpoint, extracts the daily CPI YoY
nowcast trajectory for each target month, pairs each daily nowcast with the
final BLS actual, computes lead time at PUBLISH (not scrape), and fits
empirical residual σ per lead bucket.

Per spec §4.1 — this is the one-shot script that ships us calibrated σ on
day zero, no live-N validation needed.

Output:
    ~/Documents/terminal3b_data/nowcast_history.jsonl   — frozen pair archive
    ~/Documents/terminal3b_data/empirical_sigma.json    — σ table consumed by paper trader

Usage:
    python3 ~/Documents/terminal3b_nowcast_backfill.py

Re-runnable; output files are overwritten on each run.

Cleveland Fed JSON quirks:
  - Each chart represents ONE target month (subcaption like "2026-4")
  - The chart's "categories.category[*].label" are MM/DD strings; year is
    inferred (BLS releases CPI ~mid-month FOLLOWING target month, so
    nowcast trajectory dates fall in target_month or target_month+1).
  - dataset[0] = CPI YoY nowcast values (one per category), some empty
  - dataset[4] = Actual CPI YoY (single non-null at release, post-release)
  - All values are strings of the form "3.55850998734835" (high precision)
"""

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List, Optional, Tuple

import requests


CFED_URL = (
    "https://www.clevelandfed.org/-/media/files/webcharts/"
    "inflationnowcasting/nowcast_year.json"
)
DATA_DIR = Path.home() / "Documents" / "terminal3b_data"
HISTORY_PATH = DATA_DIR / "nowcast_history.jsonl"
SIGMA_PATH = DATA_DIR / "empirical_sigma.json"
LOG_PATH = DATA_DIR / "nowcast_backfill.log"

REQUEST_TIMEOUT = 60
SCHEMA_VERSION = "v1"

# Lead buckets (days to BLS release)
LEAD_BUCKETS = [
    ("le3",   0,   3),
    ("4to7",  4,   7),
    ("8to14", 8,   14),
    ("gt14",  15,  365),
]
SIGMA_FLOOR = 0.05         # % YoY — never tighter than this
MIN_N_PER_BUCKET = 10      # below this, fall back to global


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


# --------------------------------------------------------------------------
# BLS CPI release date approximation
# --------------------------------------------------------------------------
# BLS CPI is released between the 10th and 15th of the month FOLLOWING the
# target month. For lead-time accounting we use the 13th as a reasonable
# canonical mid-point — the exact day shifts by a day or two but our σ
# bucketing is wide enough (4d, 7d, etc.) that it doesn't matter.
BLS_RELEASE_DAY_OF_MONTH = 13


def bls_release_date(target_year: int, target_month: int) -> date:
    """Return canonical BLS release date for a target (year, month)."""
    # Release is in the FOLLOWING month
    release_year = target_year + (1 if target_month == 12 else 0)
    release_month = 1 if target_month == 12 else target_month + 1
    return date(release_year, release_month, BLS_RELEASE_DAY_OF_MONTH)


# --------------------------------------------------------------------------
# Date label parsing
# --------------------------------------------------------------------------

def parse_publish_date(label_mmdd: str, target_year: int, target_month: int,
                       file_publish_year: int) -> Optional[date]:
    """Convert an MM/DD category label into a full date.

    Heuristic: the nowcast trajectory for target_month runs in the weeks
    leading up to and through BLS release in target_month+1. So a label
    "MM/DD" should resolve to either target_month's year or target_month+1's
    year. We pick whichever produces a date within ±60 days of the canonical
    release date.

    Falls back to the file_publish_year (Cleveland Fed's stamped publish
    year on the JSON itself) if neither candidate is plausible.
    """
    try:
        mm, dd = label_mmdd.strip().split("/")
        mm, dd = int(mm), int(dd)
    except (ValueError, AttributeError):
        return None

    release_dt = bls_release_date(target_year, target_month)
    candidate_years = {target_year, target_year + 1, file_publish_year}
    best = None
    best_dist = 9999
    for y in candidate_years:
        try:
            cand = date(y, mm, dd)
        except ValueError:
            continue
        dist = abs((cand - release_dt).days)
        if dist < best_dist and dist <= 60:
            best = cand
            best_dist = dist
    return best


# --------------------------------------------------------------------------
# Chart parsing
# --------------------------------------------------------------------------

def parse_target_month(subcaption: str) -> Optional[Tuple[int, int]]:
    """'2026-4' → (2026, 4)."""
    if not subcaption or "-" not in subcaption:
        return None
    try:
        y, m = subcaption.split("-", 1)
        return int(y), int(m)
    except ValueError:
        return None


def _to_float(s) -> Optional[float]:
    if s is None or s == "" or s == "null":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


_TOOLTEXT_DATE_RE = None  # compiled lazily


def _extract_label_from_tooltext(tooltext: str) -> Optional[str]:
    """Tooltext format: "CPI Inflation{br}11/01{br}5.94693{br}" — second
    {br}-segment is MM/DD. More reliable than the categories array, which
    sometimes has more entries than the data series."""
    if not tooltext:
        return None
    parts = tooltext.split("{br}")
    if len(parts) < 2:
        return None
    label = parts[1].strip()
    # MM/DD shape sanity check
    if "/" not in label or len(label) > 7:
        return None
    return label


def extract_pairs(chart: dict, file_publish_year: int) -> List[dict]:
    """For one chart (one target month), return a list of records:
        { target_year, target_month, publish_date, days_to_release,
          nowcast_yoy, actual_yoy_final }
    One record per non-null daily nowcast point.
    """
    sub = chart.get("chart", {}).get("subcaption", "")
    tm = parse_target_month(sub)
    if not tm:
        return []
    target_year, target_month = tm

    ds = chart.get("dataset", [])
    if len(ds) < 5:
        return []
    nowcast_data = ds[0].get("data") or []   # CPI YoY nowcast trajectory
    actual_data = ds[4].get("data") or []    # Actual CPI YoY (single non-null after release)

    # Final actual: last non-null value in the actual series
    actual_final: Optional[float] = None
    for d in reversed(actual_data):
        f = _to_float(d.get("value"))
        if f is not None:
            actual_final = f
            break

    release_dt = bls_release_date(target_year, target_month)

    out: List[dict] = []
    for d in nowcast_data:
        nv = _to_float(d.get("value"))
        if nv is None:
            continue
        label = _extract_label_from_tooltext(d.get("tooltext", ""))
        if not label:
            continue
        pdt = parse_publish_date(label, target_year, target_month, file_publish_year)
        if pdt is None:
            continue
        dtr = (release_dt - pdt).days
        if dtr < 0:
            continue   # post-release nowcast revision — exclude from σ fit
        out.append({
            "_schema_version": SCHEMA_VERSION,
            "target_year": target_year,
            "target_month": target_month,
            "subcaption": sub,
            "label_mmdd": label,
            "publish_date": pdt.isoformat(),
            "release_date": release_dt.isoformat(),
            "days_to_release": dtr,
            "nowcast_yoy": nv,
            "actual_yoy_final": actual_final,
            "residual": (actual_final - nv) if actual_final is not None else None,
        })
    return out


# --------------------------------------------------------------------------
# σ fit
# --------------------------------------------------------------------------

def lead_bucket(dtr: int) -> str:
    for label, lo, hi in LEAD_BUCKETS:
        if lo <= dtr <= hi:
            return label
    return "gt14"


def fit_sigma(pairs: List[dict]) -> dict:
    """Fit per-bucket σ from pairs that have both nowcast and actual."""
    by_bucket: Dict[str, List[float]] = defaultdict(list)
    all_resids: List[float] = []
    for r in pairs:
        if r["residual"] is None:
            continue
        b = lead_bucket(r["days_to_release"])
        by_bucket[b].append(r["residual"])
        all_resids.append(r["residual"])

    out = {
        "version": SCHEMA_VERSION,
        "fit_at": datetime.now(timezone.utc).isoformat(),
        "sigma_floor": SIGMA_FLOOR,
        "min_n_per_bucket": MIN_N_PER_BUCKET,
        "by_lead_bucket": {},
        "global": {},
    }

    for label, lo, hi in LEAD_BUCKETS:
        rs = by_bucket.get(label, [])
        if not rs:
            out["by_lead_bucket"][label] = {
                "lead_range_days": [lo, hi],
                "n": 0,
                "fallback": True,
            }
            continue
        std = pstdev(rs) if len(rs) > 1 else 0.0
        out["by_lead_bucket"][label] = {
            "lead_range_days": [lo, hi],
            "n": len(rs),
            "mean_residual": round(mean(rs), 4),
            "raw_std": round(std, 4),
            "std_resid": round(max(std, SIGMA_FLOOR), 4),
            "min_resid": round(min(rs), 3),
            "max_resid": round(max(rs), 3),
            "fallback": len(rs) < MIN_N_PER_BUCKET,
        }

    if all_resids:
        gstd = pstdev(all_resids) if len(all_resids) > 1 else 0.0
        out["global"] = {
            "n": len(all_resids),
            "mean_residual": round(mean(all_resids), 4),
            "raw_std": round(gstd, 4),
            "std_resid": round(max(gstd, SIGMA_FLOOR), 4),
        }
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def fetch_cfed_archive() -> List[dict]:
    log(f"Fetching {CFED_URL} ...")
    r = requests.get(
        CFED_URL,
        timeout=REQUEST_TIMEOUT,
        proxies={"http": None, "https": None},
    )
    r.raise_for_status()
    log(f"  ok: {len(r.content):,} bytes")
    return r.json()


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    archive = fetch_cfed_archive()
    log(f"Got {len(archive)} charts (one per target month).")

    # The Cleveland Fed JSON has one stamped _comment for the whole file —
    # use it as the file_publish_year hint for date inference.
    first_comment = archive[0].get("chart", {}).get("_comment", "") if archive else ""
    file_publish_year = datetime.now(timezone.utc).year
    try:
        file_publish_year = int(first_comment.split("-")[0])
    except (ValueError, IndexError, AttributeError):
        pass
    log(f"File publish year (from _comment): {file_publish_year}")

    all_pairs: List[dict] = []
    targets_with_actual = 0
    targets_without_actual = 0
    for chart in archive:
        pairs = extract_pairs(chart, file_publish_year)
        if not pairs:
            continue
        all_pairs.extend(pairs)
        if pairs[0]["actual_yoy_final"] is not None:
            targets_with_actual += 1
        else:
            targets_without_actual += 1

    log(f"Extracted {len(all_pairs):,} (publish_date, nowcast, actual) tuples.")
    log(f"  target months with actual: {targets_with_actual}")
    log(f"  target months without actual yet: {targets_without_actual}  "
        f"(future or pending release — excluded from σ fit)")

    # Persist the full history (always — useful for downstream debugging)
    with open(HISTORY_PATH, "w") as f:
        for r in all_pairs:
            f.write(json.dumps(r) + "\n")
    log(f"Wrote {HISTORY_PATH}")

    # Restrict σ fit to last 24 months of TARGET MONTHS for relevance
    # (data drift — old structural relationships less informative).
    today = datetime.now(timezone.utc).date()
    cutoff_target = date(today.year - 2, today.month, 1)
    filtered = [
        r for r in all_pairs
        if r["residual"] is not None
        and date(r["target_year"], r["target_month"], 1) >= cutoff_target
    ]
    log(f"\nσ-fit input: {len(filtered):,} pairs from target months ≥ {cutoff_target}")

    sigma_table = fit_sigma(filtered)
    with open(SIGMA_PATH, "w") as f:
        json.dump(sigma_table, f, indent=2)

    log(f"\n{'='*60}")
    log("EMPIRICAL σ TABLE (CPI YoY nowcast residuals)")
    log(f"{'='*60}")
    log(f"  {'bucket':<8} {'days':<8} {'n':>4} {'mean':>8} {'std':>7} {'range':>16}")
    for label, lo, hi in LEAD_BUCKETS:
        c = sigma_table["by_lead_bucket"].get(label, {})
        if c.get("n", 0) == 0:
            log(f"  {label:<8} {lo}-{hi:<6} {'(no data)':>4}")
            continue
        rng = f"{c['min_resid']:+.2f}..{c['max_resid']:+.2f}"
        flag = "  ← fallback (n<min)" if c.get("fallback") else ""
        log(f"  {label:<8} {lo}-{hi:<6} {c['n']:>4} "
            f"{c['mean_residual']:>+7.3f}% {c['std_resid']:>6.3f}% {rng:>16}{flag}")

    g = sigma_table.get("global", {})
    if g:
        log(f"\n  global: n={g['n']}  mean_residual={g['mean_residual']:+.3f}%  "
            f"std_resid={g['std_resid']:.3f}%")
        if abs(g["mean_residual"]) >= 0.05:
            log(f"\n  ⚠  GLOBAL MEAN RESIDUAL ≥ 0.05% — bias correction needed in Phase 1.5")
            log(f"     (per spec §4.2: trigger condition met)")

    log(f"\nWrote {SIGMA_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
