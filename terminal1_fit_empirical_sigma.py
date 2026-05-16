"""Terminal 1 — Empirical σ Refit.

Replaces the heuristic `max(ensemble_std * 1.5, 3.0) * (1.10 if high else 1.0)`
with empirical residual std fit per (station, which_metric).

Methodology:
  1. Build the same daily ensemble as the backtest (bias-corrected, multi-model).
  2. For every (station, target_date) where we have BOTH ensemble and a
     matching nws actual, compute residual = actual - ensemble_mean.
  3. Fit population stdev of residuals per (station, which_metric).
     This stdev is what a calibrated normal CDF should use as σ — by
     construction it absorbs whatever bias the bias-correction missed plus
     intrinsic atmospheric variability.

Output:
  ~/Documents/terminal1_data/empirical_sigma.json
  {
    "version": "v1",
    "fit_at": "<iso-utc>",
    "by_station_metric": {
      "ATL_high": {"n": 12, "mean_resid": -2.5, "std_resid": 4.1, "min_resid": -7.7, "max_resid": 3.0},
      ...
    },
    "global_high": {"n": ..., "std_resid": ...},
    "global_low":  {"n": ..., "std_resid": ...},
    "fallback_high_sigma": <float>,
    "fallback_low_sigma":  <float>
  }

Paper trader loads this and uses by_station_metric[<station>_<metric>].std_resid
as σ; falls back to global_<metric> if the cell has < MIN_N samples.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

sys.path.insert(0, str(Path.home() / "Documents"))
from terminal1_ensemble_backtest import (  # noqa: E402
    aggregate_daily,
    compute_bias,
    build_ensemble,
    load_jsonl,
    DATA_DIR,
    MODELS,
    STATIONS,
)


OUT_PATH = DATA_DIR / "empirical_sigma.json"
MIN_N_PER_CELL = 5   # below this, fall back to global
SIGMA_FLOOR = 1.0    # safety floor (degrees F)
ENSEMBLE_VERSION = "v1"


def main() -> int:
    forecasts = []
    actuals = []
    for s in STATIONS:
        for m in MODELS:
            p = DATA_DIR / f"forecasts_{m}_{s}.jsonl"
            forecasts.extend(load_jsonl(p))
        p = DATA_DIR / f"nws_actuals_{s}.jsonl"
        actuals.extend(load_jsonl(p))

    print(f"Loaded {len(forecasts):,} forecast rows, {len(actuals):,} actual rows")

    daily = aggregate_daily(forecasts)
    print(f"Aggregated → {len(daily):,} daily forecast rows")

    actuals_idx = {(a["station"], a["date_local"]): a for a in actuals}
    bias_map = compute_bias(daily, actuals_idx)
    print(f"Bias correction ready for {len(bias_map)} cells")

    ensemble = build_ensemble(daily, bias_map)
    print(f"Built ensembles for {len(ensemble)} (station, target_date) pairs")

    residuals_by_cell: dict = defaultdict(list)
    residuals_global = {"high": [], "low": []}

    for (station, target_date), ens in ensemble.items():
        actual = actuals_idx.get((station, target_date))
        if actual is None:
            continue
        for metric in ("high", "low"):
            ens_side = ens.get(metric)
            actual_val = actual.get(f"{metric}_f")
            if ens_side is None or actual_val is None:
                continue
            mu = ens_side.get("mean")
            if mu is None:
                continue
            resid = float(actual_val) - float(mu)
            residuals_by_cell[(station, metric)].append(resid)
            residuals_global[metric].append(resid)

    print(f"\nMatched (forecast, actual) residual pairs:")
    print(f"  global high: n={len(residuals_global['high'])}")
    print(f"  global low:  n={len(residuals_global['low'])}")

    out = {
        "version": ENSEMBLE_VERSION,
        "fit_at": datetime.now(timezone.utc).isoformat(),
        "min_n_per_cell": MIN_N_PER_CELL,
        "sigma_floor": SIGMA_FLOOR,
        "by_station_metric": {},
        "global_high": {},
        "global_low": {},
    }

    for (station, metric), resids in sorted(residuals_by_cell.items()):
        if not resids:
            continue
        std = pstdev(resids) if len(resids) > 1 else 0.0
        cell = {
            "n": len(resids),
            "mean_resid": round(mean(resids), 3),
            "std_resid": round(max(std, SIGMA_FLOOR), 3),
            "raw_std": round(std, 3),
            "min_resid": round(min(resids), 2),
            "max_resid": round(max(resids), 2),
        }
        out["by_station_metric"][f"{station}_{metric}"] = cell

    for metric in ("high", "low"):
        rs = residuals_global[metric]
        if not rs:
            continue
        std = pstdev(rs) if len(rs) > 1 else 0.0
        out[f"global_{metric}"] = {
            "n": len(rs),
            "mean_resid": round(mean(rs), 3),
            "std_resid": round(max(std, SIGMA_FLOOR), 3),
            "raw_std": round(std, 3),
        }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\nFit table:")
    print(f"  {'cell':<10} {'n':>4} {'mean':>7} {'std':>7} {'range':>14}")
    for key, c in sorted(out["by_station_metric"].items()):
        rng = f"{c['min_resid']:+.1f}..{c['max_resid']:+.1f}"
        n_flag = "" if c["n"] >= MIN_N_PER_CELL else " (fallback)"
        print(f"  {key:<10} {c['n']:>4} {c['mean_resid']:>+7.2f} "
              f"{c['std_resid']:>7.2f} {rng:>14}{n_flag}")
    print()
    print(f"  global_high  n={out['global_high'].get('n','?')}  "
          f"std={out['global_high'].get('std_resid','?')}")
    print(f"  global_low   n={out['global_low'].get('n','?')}  "
          f"std={out['global_low'].get('std_resid','?')}")
    print(f"\nWrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
