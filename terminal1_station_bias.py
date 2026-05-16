"""Terminal 1 — Station Bias Calculator + Diagnostic.

Purpose: surface systematic residual bias per (station, metric, month) that
REMAINS AFTER the ensemble's per-model bias correction. If a station has a
~2°F residual in a particular direction after the existing correction, that's
real station-specific bias (urban heat island, microclimate, etc.) that
Phase 2 isn't capturing.

Also prints diagnostic report showing:
  - Forecast mean error (ME) per station/metric (bias direction)
  - Mean absolute error (MAE) (spread of misses)
  - RMSE
  - Seasonal breakdown
  - Residual distribution

Outputs:
    ~/Documents/terminal1_data/station_bias.json — structured lookup
    stdout — human-readable diagnostic table

Usage:
    python3 ~/Documents/terminal1_station_bias.py

Re-run whenever new data lands (new day of actuals + fresh forecasts).
"""

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path.home() / "Documents"))
from terminal1_ensemble_backtest import (  # noqa: E402
    aggregate_daily,
    compute_bias,
    build_ensemble,
    corrected_forecast,
    load_jsonl,
    DATA_DIR,
    MODELS,
    STATIONS,
)


OUTPUT_PATH = DATA_DIR / "station_bias.json"
MIN_SAMPLES = 10
SIGNIFICANCE_THRESHOLD_F = 0.5   # residuals < 0.5°F aren't worth correcting


def compute_residuals_per_model(daily_forecasts, bias_map, actuals_idx):
    """For each (station, model, target_date) where actual exists, compute
    residual of the bias-corrected single-model forecast. This gives us WAY
    more data than ensemble-level residuals (which drop dates with <3 models).

    Residual = actual - bias_corrected_forecast
      positive  → model under-predicts (actual was higher than we thought)
      negative  → model over-predicts

    We keep each model's residual separately so we can verify station bias
    is consistent ACROSS models (real station effect) vs. only in one model
    (model-specific artifact).

    Also keeps only the "freshest" run per (station, model, target_date) to
    avoid double-counting multiple runs forecasting the same day.
    """
    # Pick freshest run per (station, model, target_date)
    best = {}
    for (station, model, run_time, target_date), agg in daily_forecasts.items():
        key = (station, model, target_date)
        if key not in best or run_time > best[key]["run_time_utc"]:
            best[key] = agg

    rows = []
    for (station, model, target_date), agg in best.items():
        actual = actuals_idx.get((station, target_date))
        if actual is None:
            continue
        for which in ("high", "low"):
            actual_val = actual.get(f"{which}_f")
            if actual_val is None:
                continue
            forecast = corrected_forecast(agg, which, bias_map)
            if forecast is None:
                continue
            residual = actual_val - forecast
            try:
                month = int(target_date.split("-")[1])
            except (ValueError, IndexError):
                month = 0
            rows.append({
                "station": station,
                "model": model,
                "target_date": target_date,
                "month": month,
                "which": which,
                "forecast": forecast,
                "actual": actual_val,
                "residual": residual,
                "abs_residual": abs(residual),
            })
    return rows


def compute_residuals(ensemble, actuals_idx):
    """LEGACY — ensemble-level residuals. Kept for reference. Use
    compute_residuals_per_model for the bias analysis."""
    rows = []
    for (station, target_date), sides in ensemble.items():
        actual = actuals_idx.get((station, target_date))
        if actual is None:
            continue
        for which in ("high", "low"):
            side = sides.get(which)
            if side is None:
                continue
            actual_val = actual.get(f"{which}_f")
            if actual_val is None:
                continue
            residual = actual_val - side["mean"]
            try:
                month = int(target_date.split("-")[1])
            except (ValueError, IndexError):
                month = 0
            rows.append({
                "station": station,
                "target_date": target_date,
                "month": month,
                "which": which,
                "ensemble_mean": side["mean"],
                "ensemble_std": side["std"],
                "ensemble_n_models": side["n"],
                "actual": actual_val,
                "residual": residual,
                "abs_residual": abs(residual),
            })
    return rows


def summarize_by_cell(rows, cell_keys):
    """Group residuals by tuple(cell_keys) and compute summary stats."""
    groups = defaultdict(list)
    for r in rows:
        key = tuple(r[k] for k in cell_keys)
        groups[key].append(r["residual"])

    summaries = {}
    for key, resids in groups.items():
        if len(resids) < 2:
            summaries[key] = {
                "n": len(resids),
                "mean_residual": mean(resids) if resids else 0,
                "stdev_residual": None,
                "mae": mean([abs(r) for r in resids]) if resids else 0,
                "rmse": math.sqrt(mean([r*r for r in resids])) if resids else 0,
            }
            continue
        summaries[key] = {
            "n": len(resids),
            "mean_residual": round(mean(resids), 3),
            "stdev_residual": round(stdev(resids), 3),
            "mae": round(mean([abs(r) for r in resids]), 3),
            "rmse": round(math.sqrt(mean([r*r for r in resids])), 3),
        }
    return summaries


def main() -> int:
    # Load everything
    print("=" * 80)
    print("TERMINAL 1 — STATION BIAS DIAGNOSTIC")
    print("=" * 80)
    print(f"Generated: {datetime.utcnow().isoformat()}Z")
    print()

    forecasts = []
    for m in MODELS:
        for s in STATIONS:
            forecasts.extend(load_jsonl(DATA_DIR / f"forecasts_{m}_{s}.jsonl"))
    actuals = []
    for s in STATIONS:
        actuals.extend(load_jsonl(DATA_DIR / f"nws_actuals_{s}.jsonl"))

    print(f"Loaded {len(forecasts):,} forecast records, {len(actuals):,} actuals")

    daily = aggregate_daily(forecasts)
    actuals_idx = {(a["station"], a["date_local"]): a for a in actuals}
    bias_map = compute_bias(daily, actuals_idx)
    ensemble = build_ensemble(daily, bias_map)

    print(f"Built {len(ensemble)} (station, target_date) ensembles after model-level bias correction.")
    print()

    # Use PER-MODEL residuals, not ensemble residuals (ensembles require ≥3 models,
    # which drops most historical dates due to ECMWF/AIFS retention limits).
    rows = compute_residuals_per_model(daily, bias_map, actuals_idx)
    print(f"Found {len(rows)} (station, model, target_date, metric) residual points "
          f"across {len(MODELS)} models.")
    print()

    # Diagnostic: per-model overall residuals (sanity — do some models bias more?)
    by_model = summarize_by_cell(rows, ["model", "which"])
    print("-" * 80)
    print("PER (MODEL, METRIC) — bias after per-model correction (should be ~0)")
    print("-" * 80)
    print(f"{'model':<12} {'metric':<6} {'n':>4} {'mean_resid':>12} {'stdev':>8} {'MAE':>7}")
    for (model, metric), s in sorted(by_model.items()):
        stdev_str = f"{s['stdev_residual'] or 0:.2f}"
        print(f"{model:<12} {metric:<6} {s['n']:>4} {s['mean_residual']:>+12.3f} "
              f"{stdev_str:>8} {s['mae']:>7.2f}")
    print()

    # Per (station, metric) overall — pooled across models
    by_station_metric = summarize_by_cell(rows, ["station", "which"])
    print("-" * 80)
    print("PER (STATION, METRIC) — residual = actual - ensemble_mean (°F)")
    print("-" * 80)
    print(f"{'station':<6} {'metric':<6} {'n':>4} {'mean_resid':>12} {'stdev':>8} {'MAE':>7} {'RMSE':>7} {'verdict':<40}")
    for (station, metric), s in sorted(by_station_metric.items()):
        verdict = ""
        if s["n"] < MIN_SAMPLES:
            verdict = "(too few samples)"
        elif abs(s["mean_residual"]) < SIGNIFICANCE_THRESHOLD_F:
            verdict = "calibrated (no action)"
        elif s["mean_residual"] > 0:
            verdict = f"ensemble UNDER-predicts by {s['mean_residual']:.2f}°F"
        else:
            verdict = f"ensemble OVER-predicts by {abs(s['mean_residual']):.2f}°F"
        print(f"{station:<6} {metric:<6} {s['n']:>4} {s['mean_residual']:>+12.3f} "
              f"{s['stdev_residual'] or 0:>8.2f} {s['mae']:>7.2f} {s['rmse']:>7.2f}  {verdict}")

    print()

    # Per (station, metric, month) — seasonal
    by_station_month = summarize_by_cell(rows, ["station", "which", "month"])
    print("-" * 80)
    print("PER (STATION, METRIC, MONTH) — significant cells only (|mean| ≥ 0.5°F, n ≥ 5)")
    print("-" * 80)
    sig = [(k, v) for k, v in by_station_month.items()
           if v["n"] >= 5 and abs(v["mean_residual"]) >= SIGNIFICANCE_THRESHOLD_F]
    if sig:
        print(f"{'station':<6} {'metric':<6} {'month':<6} {'n':>4} {'mean_resid':>12} {'stdev':>8}")
        for (station, metric, month), s in sorted(sig):
            print(f"{station:<6} {metric:<6} {month:<6} {s['n']:>4} {s['mean_residual']:>+12.3f} "
                  f"{s['stdev_residual'] or 0:>8.2f}")
    else:
        print("(no significant seasonal cells — residuals noise-dominated)")
    print()

    # Build the output JSON
    bias_output = {
        "generated_utc": datetime.utcnow().isoformat() + "Z",
        "methodology": (
            "Residual of (actual - bias_corrected_ensemble) after existing per-model "
            "bias correction. Only station/metric cells where |mean_residual| >= "
            f"{SIGNIFICANCE_THRESHOLD_F}°F and n >= {MIN_SAMPLES} are recorded. Others "
            "are treated as noise (no correction applied)."
        ),
        "by_station_metric": {},
        "by_station_metric_month": {},
        "sample_sizes": {},
    }

    for (station, metric), s in by_station_metric.items():
        if s["n"] < MIN_SAMPLES or abs(s["mean_residual"]) < SIGNIFICANCE_THRESHOLD_F:
            continue
        bias_output["by_station_metric"].setdefault(station, {})[metric] = {
            "offset_f": s["mean_residual"],
            "n_samples": s["n"],
            "uncertainty_f": s["stdev_residual"],
        }

    for (station, metric, month), s in by_station_month.items():
        if s["n"] < 5 or abs(s["mean_residual"]) < SIGNIFICANCE_THRESHOLD_F:
            continue
        bias_output["by_station_metric_month"].setdefault(station, {}).setdefault(metric, {})[str(month)] = {
            "offset_f": s["mean_residual"],
            "n_samples": s["n"],
        }

    for (station, metric), s in by_station_metric.items():
        bias_output["sample_sizes"].setdefault(station, {})[metric] = s["n"]

    with open(OUTPUT_PATH, "w") as f:
        json.dump(bias_output, f, indent=2)
    print(f"Wrote {OUTPUT_PATH}")
    print()

    # Final summary
    n_significant = sum(len(v) for v in bias_output["by_station_metric"].values())
    print("-" * 80)
    print("SUMMARY")
    print("-" * 80)
    print(f"Significant station-level biases found: {n_significant} / {len(by_station_metric)}")
    if n_significant > 0:
        print("Phase 2 paper trader can apply these offsets on top of the existing bias correction.")
        print("Effect: tightens forecast → tighter probabilities → more/better signals.")
    else:
        print("No systematic station bias detected beyond what per-model correction captures.")
        print("Phase 2 calibration is doing its job at the station level.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
