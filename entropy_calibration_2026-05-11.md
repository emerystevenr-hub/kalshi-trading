# Entropy Detector — Calibration Report

**Run:** 2026-05-11 (scheduled task, autonomous)
**Detector active since:** 2026-05-09 (~2.0 days)
**File scanned:** `~/Documents/entropy_alerts.jsonl` (42,332 rows)
**Prior report:** `entropy_calibration_2026-05-10.md` (20,672 rows). Diff this run: **+19,152 rows since yesterday 18:00 UTC, +0 watch alerts.**

---

## Headline

**n_calibrated = 0 (unchanged). Phase 2 not on the table.** The three structural bugs flagged in yesterday's report are all still present after another ~21k records. Detector is producing noise at scale, not data.

Per decision logic: `n_calibrated < 15` → status update only, no ping, no Phase 2 recommendation. But the right call is not "let calibration accumulate" — it is **fix the pipeline, because calibration is mathematically impossible in the current configuration.**

---

## 1. Counts

| Metric | Value | Δ vs. 2026-05-10 |
|---|---|---|
| Total alerts | 42,332 | +21,660 |
| `alert_level = watch` | **0** | 0 |
| `alert_level = noise` | 42,332 (100%) | +21,660 |
| Days covered | 2026-05-09 → 2026-05-11 | +1 day |
| Per engine — T3b | 36,762 | +18,810 |
| Per engine — T3c | 5,570 | +2,850 |
| Per engine — **T3a** | **0** | 0 |
| Per direction — `flat` | 42,332 (100%) | +21,660 |
| Per direction — `rising_yes` | 0 | 0 |
| Per direction — `collapsing_yes` | 0 | 0 |

Per-engine watch breakdown: **N/A — no watch alerts on any engine.**

Daily volume: 5/9 = 6,916 · 5/10 = 21,660 · 5/11 = 13,756 (partial day, ~15:09 UTC). Throughput is steady-to-growing. The detector is alive — it just produces nothing usable.

---

## 2. Outcome attribution

Skipped. n_calibrated = 0. Nothing to score against `shadow_pnl/ledger.jsonl` (432KB, current) or Kalshi `/markets`.

---

## 3. Hit rate / hypothetical P&L

Skipped. n_calibrated = 0.

---

## 4. Decision call

**n_calibrated < 15 → status update only. No Phase 2 enable/disable recommendation.**

This is now the second consecutive run where the answer is "nothing to decide because no alerts qualify." The detector has burned ~42k records of compute and emitted zero signal. Without a pipeline fix, this report will be identical at 60k, 100k, 200k records.

---

## 5. Pipeline status — gates still stuck

Three independent gates are still hard-failing at 100%, exactly as called out yesterday:

**(a) Liquidity gate — still 0/42,332 pass.**
`liquidity_pass = True` count: 0. `volume_5min > 0` count: 0. `volume_baseline_median` is 0 across every row. After ~2 days this isn't an off-hours artifact — it's either the volume feed not wired into the entropy path, or a null-coalesce-to-zero upstream. **Highest priority fix.** Nothing else matters until volume populates.

**(b) Direction collapse — still flat on 100%.**
`yes_p_5min_start == yes_p_5min_end` on **42,332 / 42,332**. Delta is exactly 0.0 on every row across 2 days of active CPI/Jobless trading. This is the same-snapshot-twice bug from yesterday. Without movement, no row can be tagged `rising_yes` / `collapsing_yes`, so no row can be a watch alert by definition.

**(c) Cold-start baseline — partial improvement, still dominant.**
`baseline_std = 0` on 33,389 / 42,332 (78.9%), down from 83.2% yesterday. As expected, this is slowly self-resolving as the rolling window fills. Of the 8,943 records with non-zero baseline_std, **1,159 have |z| ≥ 2** (up from 483 yesterday) — including one at |z| = 28.75 and 453 at |z| > 3. Every one of those 1,159 was squashed by gates (a) and (b). The detector is finding entropy collapses; they just can't survive the pipeline.

**T3a — still silent.** Spec says T3a watches KXFEDDECISION + KXFED. Two days in, T3a has emitted 0 records of any kind. Either not wired into the entropy detector, not running, or running against a window with no instruments. Confirm.

---

## 6. False-positive concentration check

Threshold: >40% of noise alerts from a single ticker → recommend per-ticker volume calibration.

- **Top ticker:** KXCPIYOY-26JUN-T4.0 at 1.32% (tied across strike ladder — strikes are even-weighted).
- **Top event family:** KXCPIYOY-26MAY at 35.53% (down slightly from yesterday's 35.53% as KXCPIYOY-26JUN ramps). Still under 40%.
- Top T3c ticker concentration: KXJOBLESSCLAIMS-26MAY14-180000 at 10.0% of T3c (no T3c ticker exceeds 10%).

**No per-ticker volume calibration recommendation triggered.** The volume gate issue is universal, not ticker-specific.

---

## 7. Recommended next actions

Same as yesterday, escalated because two days have passed with no movement:

1. **Wire volume into the entropy detector.** Validate `volume_5min` is being read from the same tick source as the snipers, not defaulted to 0. Single fix unblocks Phase 1 defensive utility.
2. **Fix the snapshot-twice direction bug.** `yes_p_5min_start` should be the YES tick at `t − 300s`, `yes_p_5min_end` should be the tick at `t`. Verify these are not pulling from the same call.
3. **Confirm T3a is in the loop or remove the claim that it is.** Either bind T3a to its event tickers in the detector config, or update the runbook to say T3a is excluded.
4. **Hold off on Phase 2 logic, gating, and config drafts** until items 1–3 are resolved. There's no calibration data on which to base thresholds.
5. **Once gates pass, re-enable this scheduled task and let n accumulate.** With ~1,159 high-|z| candidates over 2 days under broken gates, throughput post-fix should reach n=15 within hours to a single trading session — not days.

---

**Bottom line:** Same call as yesterday. Detector is structurally non-functional, not under-trained. Calibration cannot start until pipeline fixes ship.
