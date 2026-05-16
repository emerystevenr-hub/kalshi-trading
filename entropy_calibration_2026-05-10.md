# Entropy Detector — Calibration Report

**Run:** 2026-05-10 (scheduled task, autonomous)
**Detector active since:** 2026-05-09
**File scanned:** `~/Documents/entropy_alerts.jsonl` (20,672 rows)

---

## Headline

**n_calibrated = 0. Phase 2 not on the table. The detector is producing 100% noise — and the cause is a pipeline bug, not a quiet market.**

Per decision logic: `n_calibrated < 15` → status update only, no ping. But the underlying reason there are zero watch-level alerts is structural, and Phase 1 is not actually defensive in its current state.

---

## 1. Counts

| Metric | Value |
|---|---|
| Total alerts | 20,672 |
| `alert_level = watch` | **0** |
| `alert_level = noise` | 20,672 (100%) |
| Days covered | 2026-05-09 → 2026-05-10 (1.5d) |
| Per engine | T3b: 17,952 · T3c: 2,720 · **T3a: 0** |
| Per direction | flat: 20,672 · rising_yes: 0 · collapsing_yes: 0 |

**Per-engine watch breakdown:** N/A — no watch alerts exist on any engine.

---

## 2. Outcome attribution

Skipped. n_calibrated = 0. Nothing to score against `shadow_pnl/ledger.jsonl` or Kalshi settles.

---

## 3. Hit rate / hypothetical P&L

Skipped. n_calibrated = 0.

---

## 4. Decision call

**n_calibrated < 15 → status update only. No Phase 2 enable/disable recommendation.**

But this isn't a "wait and let calibration accumulate" situation. The detector cannot generate watch alerts in its current state. See section 5.

---

## 5. Why every alert is noise — pipeline diagnostic

Three independent gates are stuck, each of which alone forces `noise`:

**(a) Liquidity gate hard-fails 100% of the time.**
`liquidity_pass = True` on **0 / 20,672** records. Every record has `volume_5min = 0.0` AND `volume_baseline_median = 0.0`. Either the volume feed is not wired into the entropy detector, the field is null-coalesced to 0 upstream, or these CPI/Jobless contracts genuinely don't trade in 5-min windows (unlikely — KXCPIYOY-26MAY is a flagship contract). Fix this first; nothing else matters until volume populates.

**(b) Direction collapses to "flat" on 100% of records.**
`yes_p_5min_start == yes_p_5min_end` on **all 20,672** rows (delta exactly zero, not even rounding noise). YES prices do not stay literally flat across every 5-min window for 1.5 days on actively-listed CPI strikes. This is a snapshot bug — most likely both endpoints are being read from the same tick instead of `t-300s` and `t`. Without movement, the detector cannot tag `rising_yes` / `collapsing_yes`, so even a clean entropy collapse with passing liquidity would still log as `flat` → noise.

**(c) Cold-start baseline.**
`baseline_std = 0.0` on 17,200 / 20,672 (83%). Z-scores collapse to 0 when std is 0, so most "alerts" are mechanical placeholders. The other 17% have non-zero baseline_std and produced **483 candidates with |z| ≥ 2** (largest |z| = 5.29 on KXCPIYOY-26APR-T3.6). All 483 were squashed by gates (a) and (b). This issue self-resolves as the rolling baseline accumulates; (a) and (b) do not.

**T3a silence:** spec says T3a scanner runs on KXFEDDECISION + KXFED, but it has emitted 0 entropy alerts. Confirm T3a is wired into the entropy detector — currently it isn't contributing.

---

## 6. False-positive concentration check

Threshold: >40% of noise alerts from a single ticker → recommend per-ticker volume calibration.

- **Top ticker:** KXCPIYOY-26JUN-T4.0 at 1.32% — well under threshold (CPI strikes are even-weighted across the strike ladder).
- **Top event family:** KXCPIYOY-26MAY at 35.53% — under 40% but close. Worth watching as June/July CPI events spin up.

**No per-ticker volume calibration recommendation triggered.** The volume gate problem is universal (100% hard-fail), not a single-ticker issue.

---

## 7. Recommended next actions (sharp)

1. **Fix the volume ingest into the entropy detector.** `volume_5min` should not be 0 on every CPI/Jobless tick. Likely a missing field mapping when the detector reads the order book snapshot. Until this is fixed, Phase 1 is *not* defensive — fading-collapse entries are not being blocked because nothing reaches watch level.
2. **Fix the price-delta sampling.** `yes_p_5min_start == yes_p_5min_end` on 100% of records is a snapshot bug, not market behavior. Confirm the 5-min lookback is reading the historical tick, not the current tick twice.
3. **Wire T3a into the entropy detector** or remove "T3a" from the watch surface in the calibration spec — currently the spec asserts coverage that isn't happening.
4. **Re-run calibration only after (1) and (2) ship.** The 1.5 days of "noise" so far do not count toward the n=15 calibration target — they're synthetic flat outputs from broken gates.

Until those three fixes land, this scheduled task will keep returning `n_calibrated = 0` regardless of how long it runs.
