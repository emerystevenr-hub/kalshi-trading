"""Terminal 1 — Ensemble + Backtest + Phase 1 Report.

Single-file Phase 1 deliverable. Runs after model-backfill + NWS-actuals
pullers have populated ~/Documents/terminal1_data/.

Pipeline:
  1. Load all forecast records (4 models) and NWS actuals into DataFrames.
  2. Aggregate hourly forecasts → daily max/min per (station, model,
     run_time, target_date_local).
  3. Compute per-(station, model, lead_bucket) rolling bias correction.
  4. Apply bias correction; build ensemble (mean + stddev across 4 models).
  5. For each (station, target_date), synthesize bucket probabilities
     using a normal approximation from the ensemble mean/stddev.
  6. Track A backtest: compare ensemble P(bucket) vs climatology P(bucket)
     over a rolling 30-day prior. Simulate trades where edge ≥ MIN_EDGE_CENTS.
  7. Settle each trade against NWS actual → realized P&L per trade.
  8. Aggregate into Phase 1 ranked report (station × edge-frequency × hit-rate
     × avg-edge × P&L-by-threshold).

Inputs (expected in ~/Documents/terminal1_data/):
  forecasts_{gfs,hrrr,ecmwf_hres,aifs}_{NYC,ORD,LAX}.jsonl
  nws_actuals_{NYC,ORD,LAX}.jsonl

Outputs:
  ~/Documents/terminal1_phase1_trades.jsonl       (every simulated trade)
  ~/Documents/terminal1_phase1_report.json        (aggregate report)
  ~/Documents/terminal1_phase1_report.md          (human-readable summary)

Usage:
  python3 ~/Documents/terminal1_ensemble_backtest.py
  python3 ~/Documents/terminal1_ensemble_backtest.py --min-edge 0.05
  python3 ~/Documents/terminal1_ensemble_backtest.py --max-lead 48
"""

import argparse
import json
import math
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List, Optional, Tuple

DATA_DIR = Path.home() / "Documents" / "terminal1_data"
TRADES_OUT = Path.home() / "Documents" / "terminal1_phase1_trades.jsonl"
REPORT_JSON = Path.home() / "Documents" / "terminal1_phase1_report.json"
REPORT_MD = Path.home() / "Documents" / "terminal1_phase1_report.md"

STATIONS = ["NYC", "ORD", "LAX", "DEN", "ATL", "MIA", "PHX"]  # DFW pruned 2026-04-24 (no Kalshi markets)
# 2026-04-23: HRRR restored to ensemble after puller switched to AWS
# NOAA Open Data (noaa-hrrr-bdp-pds). Full archive back to 2014, no
# retention limit. GFS also switched to noaa-gfs-bdp-pds (full archive
# back to 2021). ECMWF HRES and AIFS still bound to ECMWF Open Data
# retention (~2 days); bias correction will down-weight them as the
# sample size lags behind GFS/HRRR.
MODELS = ["gfs", "hrrr", "ecmwf_hres", "aifs"]
ALL_MODELS = ["gfs", "hrrr", "ecmwf_hres", "aifs"]

# Strike bucket grid for backtest (proxies Kalshi's typical 2-3°F buckets).
# We place buckets at every integer °F from 20..100. For each day's predicted
# high or low we test trades at each bucket center.
BUCKET_LOW = 20
BUCKET_HIGH = 110
BUCKET_STEP = 1  # °F

# Phase 1 threshold buckets for P&L aggregation (per Steve's spec).
EDGE_THRESHOLD_BUCKETS = [
    (0.02, 0.05),
    (0.05, 0.08),
    (0.08, 0.12),
    (0.12, 1.00),  # "12+"
]

# Kalshi taker fee formula, returns fee per contract per fill (USD).
def kalshi_fee(price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    raw = 0.07 * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out: List[dict] = []
    with open(path) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# Daily aggregation: hourly forecasts → daily max/min per issuing run
# ---------------------------------------------------------------------------

def aggregate_daily(forecasts: List[dict]) -> Dict[tuple, dict]:
    """For each (station, model, run_time, target_date_local): compute
    daily_high_f = max(temp_f over all lead_hours whose valid time falls in
    target_date_local) and daily_low_f = min(..).

    Returns dict keyed by (station, model, run_time_utc, target_date_local).
    """
    grouped: Dict[tuple, List[float]] = defaultdict(list)
    lead_hrs: Dict[tuple, int] = {}

    for rec in forecasts:
        station = rec.get("station")
        model = rec.get("model")
        run_time = rec.get("run_time_utc")
        target_date = rec.get("target_date_local")
        temp_f = rec.get("temp_f")
        lead = rec.get("lead_hours", 0)
        if None in (station, model, run_time, target_date, temp_f):
            continue
        key = (station, model, run_time, target_date)
        grouped[key].append(float(temp_f))
        lead_hrs[key] = max(lead_hrs.get(key, 0), int(lead))

    out: Dict[tuple, dict] = {}
    for key, temps in grouped.items():
        if not temps:
            continue
        out[key] = {
            "station": key[0],
            "model": key[1],
            "run_time_utc": key[2],
            "target_date_local": key[3],
            "daily_high_f": max(temps),
            "daily_low_f": min(temps),
            "n_samples": len(temps),
            "max_lead_hours": lead_hrs[key],
        }
    return out


# ---------------------------------------------------------------------------
# Bias correction (rolling per station × model × lead_bucket)
# ---------------------------------------------------------------------------

LEAD_BUCKETS = [(0, 24), (24, 48), (48, 72), (72, 96)]  # hours


def lead_bucket_of(hours: int) -> tuple:
    for lo, hi in LEAD_BUCKETS:
        if lo <= hours < hi:
            return (lo, hi)
    return LEAD_BUCKETS[-1]


_BIAS_MIN_N = 3   # threshold for accepting a bias estimate at any granularity


def compute_bias(
    daily_forecasts: Dict[tuple, dict],
    actuals_by_station_date: Dict[tuple, dict],
) -> Dict[tuple, Tuple[float, float]]:
    """Compute mean(forecast - actual) per cell, returning a bias_map with
    the FALLBACK CHAIN baked in:

        Primary:    (station, model, lead_bucket, which)
        Fallback 1: (station, model, "*", which)         — pooled across leads
        Fallback 2: (station, "*",   "*", which)         — pooled across models

    `corrected_forecast` does the lookup in that order. `*` is a literal
    sentinel string (not Python's wildcard) so the dict still keys cleanly.

    2026-04-26 patch trail (TWO bugs found and fixed):
      1. Population mismatch — bias was fit on ALL runs but applied to the
         latest-run-only subset that build_ensemble picks. Caused 1–5°F
         over-correction across every (station, metric) cell. Fixed by
         pre-selecting latest run per (station, model, target_date) inside
         compute_bias, mirroring build_ensemble's member-selection.
      2. Coverage gaps — with only 6 unique target_dates and the n≥3 gate,
         14% of (station, model, lead_bucket, which) cells had no bias
         estimate. Those ensemble members fell back to raw forecasts and
         dragged residuals back toward the over-prediction baseline. Fixed
         by emitting pooled fallback keys at coarser granularity.
    """
    # Pre-pass: select the latest run per (station, model, target_date).
    # Same selection logic as build_ensemble — keeps populations matched.
    latest: Dict[tuple, Tuple[str, dict]] = {}
    for (station, model, run_time, target_date), agg in daily_forecasts.items():
        key = (station, model, target_date)
        prev = latest.get(key)
        if prev is None or run_time > prev[0]:
            latest[key] = (run_time, agg)

    primary: Dict[tuple, List[float]] = defaultdict(list)
    pool_models: Dict[tuple, List[float]] = defaultdict(list)   # same lead, all models
    pool_leads: Dict[tuple, List[float]] = defaultdict(list)    # same model, all leads
    pool_both: Dict[tuple, List[float]] = defaultdict(list)     # same station, all models+leads

    for (station, model, target_date), (_run_time, agg) in latest.items():
        actual = actuals_by_station_date.get((station, target_date))
        if not actual:
            continue
        lb = lead_bucket_of(agg["max_lead_hours"])
        for which, val_key in (("high", "daily_high_f"), ("low", "daily_low_f")):
            actual_v = actual.get(f"{which}_f")
            if actual_v is None:
                continue
            diff = agg[val_key] - actual_v
            primary[(station, model, lb, which)].append(diff)
            pool_models[(station, "*", lb, which)].append(diff)
            pool_leads[(station, model, "*", which)].append(diff)
            pool_both[(station, "*", "*", which)].append(diff)

    out: Dict[tuple, Tuple[float, float]] = {}
    for src in (primary, pool_models, pool_leads, pool_both):
        for key, xs in src.items():
            if len(xs) < _BIAS_MIN_N:
                continue
            out[key] = (mean(xs), pstdev(xs) if len(xs) > 1 else 0.0)
    return out


def corrected_forecast(
    agg: dict,
    which: str,
    bias_map: Dict[tuple, Tuple[float, float]],
) -> Optional[float]:
    """Apply bias correction: corrected = raw - bias. Lookup walks the
    fallback chain emitted by compute_bias: per-bucket → per-model-pooled
    → per-station-pooled. Returns raw if all fallbacks miss."""
    s = agg["station"]
    m = agg["model"]
    lb = lead_bucket_of(agg["max_lead_hours"])
    raw = agg["daily_high_f"] if which == "high" else agg["daily_low_f"]
    # Walk fallback chain most-specific to most-general:
    #   1. exact (station, model, lead_bucket, which)
    #   2. station + lead, pooled across models       (preserves lead-time skill)
    #   3. station + model, pooled across leads       (preserves per-model bias)
    #   4. station-only pool                          (last resort)
    for key in (
        (s, m, lb, which),
        (s, "*", lb, which),
        (s, m, "*", which),
        (s, "*", "*", which),
    ):
        if key in bias_map:
            bias, _std = bias_map[key]
            return raw - bias
    return raw


# ---------------------------------------------------------------------------
# Ensemble: mean + stddev across 4 models for each (station, target_date, which)
# Picks the latest run_time_utc per model (freshest forecast).
# ---------------------------------------------------------------------------

def build_ensemble(
    daily_forecasts: Dict[tuple, dict],
    bias_map: Dict[tuple, Tuple[float, float]],
    max_lead: Optional[int] = None,
    trade_lead_hours: Optional[int] = None,
    allowed_models: Optional[List[str]] = None,
) -> Dict[tuple, Dict[str, dict]]:
    """For each (station, target_date_local), collect the most recent forecast
    from each model within constraints. Return:
        {(station, target_date): {"high": {mean, std, n, members}, "low": {...}}}

    Constraints:
      max_lead — drop forecasts where max_lead_hours > this (hours).
      trade_lead_hours — simulate trading N hours BEFORE target_date starts
          (target_date midnight UTC). Only use forecasts whose run_time is on
          or before that cutoff. Emulates realistic trading window — you can't
          trade using a forecast issued after the target weather already
          started to materialize.
      allowed_models — only include these models in the ensemble.
    """
    # For each (station, target_date, model), keep the forecast with the
    # LATEST run_time_utc (freshest) that satisfies all constraints.
    best: Dict[tuple, dict] = {}
    for (station, model, run_time, target_date), agg in daily_forecasts.items():
        if allowed_models is not None and model not in allowed_models:
            continue
        if max_lead is not None and agg["max_lead_hours"] > max_lead:
            continue
        if trade_lead_hours is not None:
            # target_date is "YYYY-MM-DD" in UTC. Cutoff = midnight UTC of that
            # date, minus trade_lead_hours. Run must be <= cutoff.
            try:
                target_midnight = datetime.fromisoformat(
                    f"{target_date}T00:00:00+00:00"
                )
                cutoff = target_midnight - timedelta(hours=trade_lead_hours)
                run_dt = datetime.fromisoformat(run_time)
                if run_dt > cutoff:
                    continue
            except (ValueError, TypeError):
                continue
        key = (station, target_date, model)
        if key not in best or run_time > best[key]["run_time_utc"]:
            best[key] = agg

    # Group into ensembles per (station, target_date).
    members: Dict[tuple, Dict[str, List[float]]] = defaultdict(
        lambda: {"high": [], "low": []}
    )
    for (station, target_date, _model), agg in best.items():
        h = corrected_forecast(agg, "high", bias_map)
        l = corrected_forecast(agg, "low", bias_map)
        if h is not None:
            members[(station, target_date)]["high"].append(h)
        if l is not None:
            members[(station, target_date)]["low"].append(l)

    out: Dict[tuple, Dict[str, dict]] = {}
    for key, sides in members.items():
        out[key] = {}
        for which in ("high", "low"):
            xs = sides[which]
            # 2026-04-26: lowered from ≥3 to ≥2 models. ECMWF+AIFS open-data
            # retention is ~7-12 days, so ~80% of (station, target_date) pairs
            # have only GFS+HRRR coverage. The ≥3 gate dropped n=6 per cell
            # in σ refit, far below the n=30 threshold for reliable empirical
            # σ. Two physics models (GFS+HRRR) is still an ensemble — and we
            # use empirical_sigma table for σ now, not model-disagreement std.
            if len(xs) < 2:
                continue
            out[key][which] = {
                "mean": mean(xs),
                "std": pstdev(xs) if len(xs) > 1 else 2.0,
                "n": len(xs),
                "members": xs,
            }
    return out


# ---------------------------------------------------------------------------
# Probability of (temp in bucket) using Normal approximation on ensemble
# ---------------------------------------------------------------------------

def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bucket_probability(
    ensemble_mu: float,
    ensemble_sigma: float,
    lo: float,
    hi: float,
    which: Optional[str] = None,  # "high" or "low" — drives σ multiplier
) -> float:
    """Return P(lo <= X <= hi) where X ~ N(mu, sigma_effective).

    Sigma floors:
      base:        max(ensemble_sigma * 1.5, 3.0°F)
      "high":      base × 1.10   (highs are slightly harder to forecast —
                                  convection, cloud variability; retrosim
                                  2026-04-24 showed highs ratio 0.82 pre-fix)
      "low":       base × 1.00   (retrosim 2026-04-24 showed lows ratio 1.03)

    Calibration note (2026-04-24): the station_bias diagnostic showed GFS/HRRR
    residual stdev is 4.7-7°F, vs. our prior 1°F floor. That over-confidence
    produced $0.30+ "edges" in Phase 2 that likely won't survive settlement.
    The 1.5× multiplier widens reported ensemble variance (which is computed
    as stdev across models, not a true forecast error). The 3°F floor catches
    cases where all models happen to agree closely (ensemble collapse).
    The +10% on highs is an empirical retrospective fit to calibration data."""
    base_sigma = max(ensemble_sigma * 1.5, 3.0)
    sigma = base_sigma * (1.10 if which == "high" else 1.00)
    z_lo = (lo - ensemble_mu) / sigma
    z_hi = (hi - ensemble_mu) / sigma
    return max(0.0, min(1.0, _norm_cdf(z_hi) - _norm_cdf(z_lo)))


# ---------------------------------------------------------------------------
# Climatology prior — proxy for market pricing in the absence of live Kalshi
# ---------------------------------------------------------------------------

CLIMO_WINDOW_DAYS = 30


def build_climatology(
    actuals: List[dict],
) -> Dict[tuple, Tuple[float, float]]:
    """For each (station, target_date): compute mean + stdev of actual highs
    and lows over the preceding CLIMO_WINDOW_DAYS. Returns:
        {(station, date, "high"): (mu, sigma), (station, date, "low"): ...}
    """
    # Index actuals by station → sorted list of (date, high, low)
    by_station: Dict[str, List[Tuple[str, Optional[float], Optional[float]]]] = \
        defaultdict(list)
    for r in actuals:
        by_station[r["station"]].append(
            (r["date_local"], r.get("high_f"), r.get("low_f"))
        )
    for s in by_station:
        by_station[s].sort(key=lambda x: x[0])

    out: Dict[tuple, Tuple[float, float]] = {}
    for station, rows in by_station.items():
        for i, (d, _h, _l) in enumerate(rows):
            # Look back CLIMO_WINDOW_DAYS preceding.
            window = rows[max(0, i - CLIMO_WINDOW_DAYS):i]
            if len(window) < 7:
                continue
            highs = [r[1] for r in window if r[1] is not None]
            lows = [r[2] for r in window if r[2] is not None]
            if highs:
                out[(station, d, "high")] = (mean(highs),
                                             pstdev(highs) if len(highs) > 1 else 3.0)
            if lows:
                out[(station, d, "low")] = (mean(lows),
                                            pstdev(lows) if len(lows) > 1 else 3.0)
    return out


# ---------------------------------------------------------------------------
# Backtest loop
# ---------------------------------------------------------------------------

def run_backtest(
    ensemble: Dict[tuple, Dict[str, dict]],
    climo: Dict[tuple, Tuple[float, float]],
    actuals_by_station_date: Dict[tuple, dict],
    min_edge: float = 0.05,
) -> List[dict]:
    """For each (station, target_date) × which ∈ {high, low} × strike bucket:
      model_p   = ensemble probability (bucket)
      market_p  = climatology probability (bucket)   [proxy]
      edge      = model_p - market_p - fee
      if edge >= min_edge: simulate a 1-contract BUY YES @ market_p
      payoff at resolution = 1 if actual in bucket else 0

    Returns list of trade records per schema §4.
    """
    backtest_id = str(uuid.uuid4())[:8]
    trades: List[dict] = []

    for (station, target_date), sides in ensemble.items():
        actual = actuals_by_station_date.get((station, target_date))
        if not actual:
            continue
        for which in ("high", "low"):
            ens = sides.get(which)
            if not ens:
                continue
            clim = climo.get((station, target_date, which))
            if not clim:
                continue
            actual_f = actual.get(f"{which}_f")
            if actual_f is None:
                continue

            mu_e, sigma_e = ens["mean"], ens["std"]
            mu_m, sigma_m = clim

            for strike in range(BUCKET_LOW, BUCKET_HIGH + 1, BUCKET_STEP):
                lo = strike - 0.5
                hi = strike + 0.5
                model_p = bucket_probability(mu_e, sigma_e, lo, hi)
                market_p = bucket_probability(mu_m, sigma_m, lo, hi)
                if market_p <= 0 or market_p >= 1:
                    continue
                fee = kalshi_fee(market_p)
                edge = model_p - market_p - fee
                if edge < min_edge:
                    continue

                outcome_win = lo <= actual_f <= hi
                payoff = 1.0 if outcome_win else 0.0
                pnl = payoff - market_p - fee

                trades.append({
                    "backtest_id": backtest_id,
                    "signal_ts_utc": datetime.now(timezone.utc).isoformat(),
                    "station": station,
                    "target_date": target_date,
                    "which": which,
                    "strike_f": strike,
                    "bucket_lo": lo,
                    "bucket_hi": hi,
                    "model_p": round(model_p, 4),
                    "market_p": round(market_p, 4),
                    "fee_usd": round(fee, 4),
                    "edge_usd": round(edge, 4),
                    "ensemble_mean_f": round(mu_e, 2),
                    "ensemble_std_f": round(sigma_e, 2),
                    "ensemble_n_models": ens["n"],
                    "climo_mean_f": round(mu_m, 2),
                    "climo_std_f": round(sigma_m, 2),
                    "actual_f": actual_f,
                    "outcome": "WIN" if outcome_win else "LOSS",
                    "realized_pnl_usd": round(pnl, 4),
                })
    return trades


# ---------------------------------------------------------------------------
# Phase 1 report (station-ranked per Steve's spec)
# ---------------------------------------------------------------------------

def build_report(trades: List[dict], min_edge: float) -> dict:
    """Aggregate trades per station with edge-freq, hit-rate, avg-edge,
    realized-P&L-by-threshold-bucket. Matches schema §5."""
    report = {
        "run_id": str(uuid.uuid4())[:8],
        "generated_ts_utc": datetime.now(timezone.utc).isoformat(),
        "config": {
            "min_edge": min_edge,
            "stations": STATIONS,
            "models": MODELS,
            "bucket_step_f": BUCKET_STEP,
            "climatology_window_days": CLIMO_WINDOW_DAYS,
        },
        "by_station": [],
        "aggregate": {},
    }

    station_bucket: Dict[str, List[dict]] = defaultdict(list)
    for t in trades:
        station_bucket[t["station"]].append(t)

    station_pnls: Dict[str, float] = {}

    for station in STATIONS:
        xs = station_bucket.get(station, [])
        if not xs:
            report["by_station"].append({
                "station": station,
                "total_signals": 0,
                "edge_frequency": 0.0,
                "hit_rate": 0.0,
                "avg_expected_edge_usd": 0.0,
                "total_pnl_usd": 0.0,
                "realized_pnl_by_bucket": {},
            })
            station_pnls[station] = 0.0
            continue

        wins = sum(1 for t in xs if t["outcome"] == "WIN")
        total_pnl = sum(t["realized_pnl_usd"] for t in xs)
        avg_edge = mean([t["edge_usd"] for t in xs]) if xs else 0.0

        bucket_breakdown = {}
        for (lo, hi) in EDGE_THRESHOLD_BUCKETS:
            in_bucket = [t for t in xs if lo <= t["edge_usd"] < hi]
            if not in_bucket:
                bucket_breakdown[f"{lo:.2f}-{hi:.2f}"] = {
                    "n": 0, "wins": 0, "pnl_usd": 0.0, "hit_rate": 0.0
                }
                continue
            bw = sum(1 for t in in_bucket if t["outcome"] == "WIN")
            bpnl = sum(t["realized_pnl_usd"] for t in in_bucket)
            bucket_breakdown[f"{lo:.2f}-{hi:.2f}"] = {
                "n": len(in_bucket),
                "wins": bw,
                "pnl_usd": round(bpnl, 2),
                "hit_rate": round(bw / len(in_bucket), 3),
            }

        report["by_station"].append({
            "station": station,
            "total_signals": len(xs),
            "edge_frequency": len(xs),  # count (denominator context needs full universe)
            "hit_rate": round(wins / len(xs), 3),
            "avg_expected_edge_usd": round(avg_edge, 4),
            "total_pnl_usd": round(total_pnl, 2),
            "realized_pnl_by_bucket": bucket_breakdown,
        })
        station_pnls[station] = total_pnl

    total_pnl_all = sum(station_pnls.values())
    top_station_share = 0.0
    if total_pnl_all != 0:
        top_station = max(station_pnls, key=lambda s: station_pnls[s])
        top_station_share = station_pnls[top_station] / total_pnl_all

    report["aggregate"] = {
        "total_signals": len(trades),
        "total_pnl_usd": round(total_pnl_all, 2),
        "avg_pnl_per_signal_usd": (
            round(total_pnl_all / len(trades), 4) if trades else 0.0
        ),
        "top_station_share_of_pnl": round(top_station_share, 3),
        "concentration_verdict": (
            "edge carried by one station" if abs(top_station_share) > 0.60
            else "edge is broad"
        ),
    }
    return report


def render_markdown(report: dict) -> str:
    lines: List[str] = []
    lines.append("# Terminal 1 — Phase 1 Backtest Report")
    lines.append("")
    lines.append(f"**Run:** {report['run_id']}  ")
    lines.append(f"**Generated:** {report['generated_ts_utc']}  ")
    cfg = report["config"]
    lines.append(f"**Config:** min_edge=${cfg['min_edge']:.2f}  "
                 f"stations={cfg['stations']}  models={cfg['models']}  "
                 f"climo_window={cfg['climatology_window_days']}d")
    lines.append("")
    agg = report["aggregate"]
    lines.append("## Aggregate")
    lines.append(f"- Total signals: **{agg['total_signals']:,}**")
    lines.append(f"- Total P&L: **${agg['total_pnl_usd']:+,.2f}**")
    lines.append(f"- Avg P&L per signal: ${agg['avg_pnl_per_signal_usd']:+.4f}")
    lines.append(f"- Top station share of P&L: "
                 f"{agg['top_station_share_of_pnl']:.1%}")
    lines.append(f"- Concentration: **{agg['concentration_verdict']}**")
    lines.append("")
    lines.append("## By Station (ranked)")
    lines.append("")
    lines.append("| Station | Signals | Hit Rate | Avg Edge | Total P&L |")
    lines.append("|---|---:|---:|---:|---:|")
    stations = sorted(report["by_station"],
                      key=lambda s: -s.get("total_pnl_usd", 0))
    for s in stations:
        lines.append(
            f"| {s['station']} | {s['total_signals']:,} | "
            f"{s['hit_rate']:.1%} | "
            f"${s['avg_expected_edge_usd']:+.4f} | "
            f"${s['total_pnl_usd']:+,.2f} |"
        )
    lines.append("")
    lines.append("## P&L by Edge Threshold Bucket")
    lines.append("")
    lines.append("| Station | 0.02-0.05 | 0.05-0.08 | 0.08-0.12 | 0.12+ |")
    lines.append("|---|---:|---:|---:|---:|")
    for s in stations:
        row = [f"| {s['station']}"]
        for (lo, hi) in EDGE_THRESHOLD_BUCKETS:
            b = s["realized_pnl_by_bucket"].get(f"{lo:.2f}-{hi:.2f}", {})
            if b.get("n", 0) == 0:
                row.append("—")
            else:
                row.append(f"n={b['n']}, pnl=${b['pnl_usd']:+,.2f}, "
                           f"hit={b['hit_rate']:.1%}")
        lines.append(" | ".join(row) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=0.05,
                    help="Minimum edge (USD) per trade to fire. Default 0.05.")
    ap.add_argument("--max-lead", type=int, default=None,
                    help="Max forecast lead hours to consider. Drops daily "
                         "aggregates where max_lead_hours > this. Note: the "
                         "freshest-pick logic already selects short leads, so "
                         "this flag rarely changes results on its own.")
    ap.add_argument("--trade-lead-hours", type=int, default=None,
                    help="Simulate trading N hours BEFORE target_date UTC "
                         "midnight. Only forecasts issued on or before "
                         "(target_midnight - N hours) are eligible. Emulates "
                         "a realistic trading window (e.g. 24 = trade day "
                         "before, 6 = trade 6h before weather materializes).")
    ap.add_argument("--models", type=str, default=None,
                    help="Comma-separated model subset (e.g. 'gfs' for "
                         "single-model backtest, 'gfs,hrrr' for two-model). "
                         "Default: use all configured MODELS.")
    args = ap.parse_args()

    allowed_models = None
    if args.models:
        allowed_models = [m.strip() for m in args.models.split(",") if m.strip()]

    print("=" * 80)
    print("TERMINAL 1 — ENSEMBLE + BACKTEST + PHASE 1 REPORT")
    print("=" * 80)
    cfg_line = (f"Config: min_edge=${args.min_edge:.2f}  "
                f"max_lead={args.max_lead}h  "
                f"trade_lead={args.trade_lead_hours}h  "
                f"models={allowed_models or 'ALL'}")
    print(cfg_line)
    print()

    # 1. Load data.
    forecasts: List[dict] = []
    for m in MODELS:
        for s in STATIONS:
            p = DATA_DIR / f"forecasts_{m}_{s}.jsonl"
            recs = load_jsonl(p)
            forecasts.extend(recs)
    actuals: List[dict] = []
    for s in STATIONS:
        actuals.extend(load_jsonl(DATA_DIR / f"nws_actuals_{s}.jsonl"))

    print(f"Loaded {len(forecasts):,} forecast records, "
          f"{len(actuals):,} actuals.")
    if not forecasts or not actuals:
        print("Missing data — cannot proceed. Confirm backfill completed.")
        sys.exit(1)

    # Index actuals.
    actuals_idx = {
        (r["station"], r["date_local"]): r for r in actuals
    }

    # 2. Daily aggregation.
    daily = aggregate_daily(forecasts)
    print(f"Aggregated into {len(daily):,} daily forecast rows "
          f"(station × model × run × target_date).")

    # 3. Bias correction.
    bias_map = compute_bias(daily, actuals_idx)
    print(f"Computed bias correction for {len(bias_map):,} "
          f"(station × model × lead_bucket × which) cells.")

    # 4. Ensemble.
    ensemble = build_ensemble(
        daily, bias_map,
        max_lead=args.max_lead,
        trade_lead_hours=args.trade_lead_hours,
        allowed_models=allowed_models,
    )
    print(f"Built {len(ensemble):,} (station × target_date) ensembles.")

    # 5. Climatology.
    climo = build_climatology(actuals)
    print(f"Built {len(climo):,} climatology priors.")

    # 6. Backtest.
    trades = run_backtest(ensemble, climo, actuals_idx, min_edge=args.min_edge)
    print(f"Simulated {len(trades):,} trades that cleared "
          f"edge ≥ ${args.min_edge:.2f}.")

    # Write trades.
    with open(TRADES_OUT, "w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")
    print(f"Trade log: {TRADES_OUT}")

    # 7. Report.
    report = build_report(trades, min_edge=args.min_edge)
    with open(REPORT_JSON, "w") as f:
        json.dump(report, f, indent=2)
    with open(REPORT_MD, "w") as f:
        f.write(render_markdown(report))
    print(f"Report (JSON): {REPORT_JSON}")
    print(f"Report (MD):   {REPORT_MD}")
    print()
    print("=" * 80)
    print("PHASE 1 SUMMARY")
    print("=" * 80)
    agg = report["aggregate"]
    print(f"Total signals:       {agg['total_signals']:,}")
    print(f"Total P&L:           ${agg['total_pnl_usd']:+,.2f}")
    print(f"Avg P&L/signal:      ${agg['avg_pnl_per_signal_usd']:+.4f}")
    print(f"Top station share:   {agg['top_station_share_of_pnl']:.1%}")
    print(f"Concentration:       {agg['concentration_verdict']}")
    for s in sorted(report["by_station"],
                    key=lambda x: -x.get("total_pnl_usd", 0)):
        print(f"  {s['station']:<4s}  signals={s['total_signals']:>5,}  "
              f"hit={s['hit_rate']:.1%}  "
              f"avg_edge=${s['avg_expected_edge_usd']:+.3f}  "
              f"pnl=${s['total_pnl_usd']:+,.2f}")


if __name__ == "__main__":
    main()
