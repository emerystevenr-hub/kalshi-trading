"""Terminal 1 — Retrospective Phase 2 Calibration Simulator.

For every (station, historical_target_date) where we have BOTH ensemble
forecasts AND actual NWS temp, produce probability claims for every
plausible Kalshi-style bucket using the CURRENT calibrated σ-floor math.
Then check empirical hit rate vs. claimed probability.

Purpose: test calibration of our P(YES) NOW against historical data,
without waiting for real-time settlements. Produces a calibration curve.

Methodology:
  For each (station, target_date, metric=high|low) with ensemble + actual:
    For each 1°F bucket centered on integers from 20 to 110°F:
      our_p = P(actual ∈ bucket | ensemble_mean, calibrated_σ)
      actual_in_bucket = round(actual) == bucket_center
      record (our_p, actual_in_bucket)
  Bin by our_p deciles, compute empirical hit rate per bin.
  Well-calibrated: empirical hit rate ≈ our_p midpoint per bin.

Usage:
    python3 ~/Documents/terminal1_calibration_retrosim.py
"""

import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path.home() / "Documents"))
from terminal1_ensemble_backtest import (  # noqa: E402
    aggregate_daily,
    compute_bias,
    build_ensemble,
    bucket_probability,
    load_jsonl,
    DATA_DIR,
    MODELS,
    STATIONS,
)


BUCKET_RANGE = (20, 110)   # °F buckets to test
N_DECILES = 10


def main() -> int:
    print("=" * 80)
    print("TERMINAL 1 — RETROSPECTIVE CALIBRATION SIMULATION")
    print("=" * 80)

    # Load data
    forecasts = []
    for m in MODELS:
        for s in STATIONS:
            forecasts.extend(load_jsonl(DATA_DIR / f"forecasts_{m}_{s}.jsonl"))
    actuals = []
    for s in STATIONS:
        actuals.extend(load_jsonl(DATA_DIR / f"nws_actuals_{s}.jsonl"))

    print(f"Loaded {len(forecasts):,} forecasts, {len(actuals):,} actuals.")

    daily = aggregate_daily(forecasts)
    actuals_idx = {(a["station"], a["date_local"]): a for a in actuals}
    bias_map = compute_bias(daily, actuals_idx)
    ensemble = build_ensemble(daily, bias_map)
    print(f"Built {len(ensemble)} ensemble pairs, bias correction for "
          f"{len(bias_map)} cells.")
    print()

    # For each (station, target_date, metric) with ensemble AND actual,
    # walk every bucket, compute our_p, check realized outcome.
    predictions: List[dict] = []
    skipped_no_actual = 0
    skipped_no_ensemble_side = 0

    for (station, target_date), sides in ensemble.items():
        actual = actuals_idx.get((station, target_date))
        if actual is None:
            skipped_no_actual += 1
            continue
        for which in ("high", "low"):
            side = sides.get(which)
            if side is None:
                skipped_no_ensemble_side += 1
                continue
            actual_val = actual.get(f"{which}_f")
            if actual_val is None:
                continue
            mu = side["mean"]
            sigma = side["std"]
            actual_rounded = int(round(actual_val))

            # Walk 1°F buckets across the plausible range
            for bucket_center in range(BUCKET_RANGE[0], BUCKET_RANGE[1] + 1):
                lo = float(bucket_center)
                hi = lo + 1.0
                our_p = bucket_probability(mu, sigma, lo, hi, which=which)
                if our_p < 0.01:
                    continue   # skip ignorable tails to reduce noise
                hit = (actual_rounded == bucket_center)
                predictions.append({
                    "station": station,
                    "target_date": target_date,
                    "which": which,
                    "bucket_center": bucket_center,
                    "our_p": our_p,
                    "hit": hit,
                })

    print(f"Generated {len(predictions):,} (station,date,metric,bucket) predictions.")
    print(f"  skipped: {skipped_no_actual} no actual, "
          f"{skipped_no_ensemble_side} no ensemble side.")
    print()

    if not predictions:
        print("No predictions — insufficient data overlap. Exiting.")
        return 1

    # Bin by our_p decile, compute empirical hit rate
    bins: Dict[int, List[dict]] = defaultdict(list)
    for p in predictions:
        # decile 0 = [0.01, 0.10), decile 1 = [0.10, 0.20), ..., 9 = [0.90, 1.00]
        d = min(9, int(p["our_p"] * 10))
        bins[d].append(p)

    # Report
    print("-" * 80)
    print("CALIBRATION CURVE")
    print("-" * 80)
    print(f"{'decile':<10} {'range':<14} {'n':>8} {'mean_our_p':>12} "
          f"{'empirical_hit':>15} {'gap':>8} {'verdict':<30}")
    print("-" * 80)

    sum_n = 0
    sum_weighted_gap = 0.0
    for d in range(10):
        rows = bins.get(d, [])
        if not rows:
            continue
        n = len(rows)
        mean_p = mean([r["our_p"] for r in rows])
        empirical = sum(1 for r in rows if r["hit"]) / n
        gap = empirical - mean_p
        sum_n += n
        sum_weighted_gap += abs(gap) * n

        if n < 20:
            verdict = "(thin sample)"
        elif abs(gap) < 0.02:
            verdict = "calibrated ✓"
        elif gap > 0:
            verdict = f"UNDER-confident by {abs(gap):.3f}"
        else:
            verdict = f"OVER-confident by {abs(gap):.3f}"
        range_s = f"[{d*0.1:.1f}, {(d+1)*0.1:.1f})"
        print(f"{d:<10} {range_s:<14} {n:>8} {mean_p:>12.3f} "
              f"{empirical:>15.3f} {gap:>+8.3f} {verdict}")

    print("-" * 80)
    weighted_avg_gap = sum_weighted_gap / sum_n if sum_n > 0 else 0
    print(f"Total predictions: {sum_n:,}")
    print(f"Weighted mean |gap|: {weighted_avg_gap:.4f}  "
          f"(< 0.03 = well calibrated)")
    print()

    # Per-metric breakdown
    print("-" * 80)
    print("PER-METRIC CALIBRATION (high vs low)")
    print("-" * 80)
    for metric in ("high", "low"):
        metric_preds = [p for p in predictions if p["which"] == metric]
        if not metric_preds:
            continue
        total_p_claim = sum(p["our_p"] for p in metric_preds)
        total_hits = sum(1 for p in metric_preds if p["hit"])
        avg_p = mean([p["our_p"] for p in metric_preds])
        empirical_rate = total_hits / len(metric_preds)
        print(f"  {metric:<5}  n={len(metric_preds):>6}  "
              f"expected_hits={total_p_claim:>7.1f}  actual_hits={total_hits:>4}  "
              f"avg_p={avg_p:.3f}  empirical_rate={empirical_rate:.3f}  "
              f"calibration_ratio={total_hits/total_p_claim:.2f}")
    print()

    # Per-station breakdown
    print("-" * 80)
    print("PER-STATION CALIBRATION")
    print("-" * 80)
    for station in STATIONS:
        s_preds = [p for p in predictions if p["station"] == station]
        if not s_preds:
            continue
        total_p = sum(p["our_p"] for p in s_preds)
        total_hits = sum(1 for p in s_preds if p["hit"])
        ratio = total_hits / total_p if total_p > 0 else 0
        flag = ""
        if ratio < 0.7:
            flag = "← over-confident"
        elif ratio > 1.3:
            flag = "← under-confident"
        else:
            flag = "← calibrated"
        print(f"  {station:<5}  n={len(s_preds):>6}  "
              f"expected={total_p:>6.1f}  actual={total_hits:>4}  "
              f"ratio={ratio:>5.2f}  {flag}")

    print()
    print("=" * 80)
    print("INTERPRETATION")
    print("=" * 80)
    print("  Calibration ratio < 0.7 = ensemble over-confident (claims more hits than reality)")
    print("  Calibration ratio 0.7–1.3 = acceptable")
    print("  Calibration ratio > 1.3 = under-confident (could be more aggressive)")
    print()
    print("  If most deciles show |gap| < 0.03 → calibrated.")
    print("  If low deciles show positive gaps + high deciles show negative gaps → σ too narrow.")
    print("  Action: tighten or loosen σ floor in bucket_probability as needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
