# Entropy Collapse Detector — Calibration Status

**Date:** 2026-05-16
**Detector start:** 2026-05-09 (7 full days + partial today)
**Log:** `~/Documents/entropy_alerts.jsonl` (153,392 records, 79 MB)

---

## Headline

**n_calibrated = 0. Zero watch-level alerts in 8 days.**

Per spec rules: `n_calibrated < 15` → status update only, no Phase-2 ping.

But this is not "waiting for more data." The detector is broken upstream. Three plumbing failures are preventing any record from clearing the watch gate.

---

## The Numbers

### Total alerts by level
| level | count |
|---|---|
| noise | 153,392 |
| watch | **0** |
| collapse | 0 |

### Per engine
| engine | records | watch alerts |
|---|---|---|
| T3a | **0** | 0 |
| T3b | 133,822 | 0 |
| T3c | 19,570 | 0 |

### Per direction
| direction | count |
|---|---|
| flat | 153,392 |
| rising_yes / falling_yes | 0 |

### Daily volume (records logged)
| date | T3b | T3c |
|---|---|---|
| 2026-05-09 | 6,006 | 910 |
| 2026-05-10 | 18,810 | 2,850 |
| 2026-05-11 | 18,876 | 2,860 |
| 2026-05-12 | 18,922 | 2,850 |
| 2026-05-13 | 19,596 | 2,840 |
| 2026-05-14 | 19,665 | 2,880 |
| 2026-05-15 | 19,596 | 2,631 |
| 2026-05-16 (partial) | 12,351 | 1,749 |

---

## Three Feeder Bugs

These three together explain why n_calibrated is stuck at zero. None of them are detector-logic problems — they are upstream data fields the detector relies on.

### 1. T3a is silent
Spec says detector watches T3a/T3b/T3c. **T3a has logged 0 records in 8 days.** Either the T3a engine never registered with the detector, the engine isn't running, or its event-bus topic is misnamed.

### 2. `volume_5min` and `volume_baseline_median` are always 0
- Records with `volume_5min > 0`: **0 / 153,392**
- Records with `volume_baseline_median > 0`: **0 / 153,392**
- Records with `liquidity_pass = true`: **0 / 153,392**

The liquidity gate cannot pass when both volume signals are stuck at zero. This is what prevents noise → watch escalation regardless of how strong the entropy z-score is. **7,214 records have |z| ≥ 2.0** (some as extreme as z = -5.29), and every single one was classified noise solely because liquidity_pass = false.

The volume feeder is either disconnected from the Kalshi book stream or writing to the wrong field.

### 3. `yes_p_5min_start` always equals `yes_p_5min_end`
- Records with start ≠ end: **0 / 153,392**

Every alert reports zero price movement across its 5-minute window. That is statistically impossible across 153K samples on live markets. The price-snapshot endpoint is returning the same value for both ends of the window, which is why `direction` is permanently "flat" — the direction-classifier needs movement to label rising_yes or falling_yes.

---

## False-positive Concentration Check

Spec flag threshold: any single ticker > 40% of noise alerts in |z|≥2 set.

| ticker | share of |z|≥2 records |
|---|---|
| KXCPIYOY-26MAY-T4.1 | 2.80% |
| KXCPIYOY-26MAY-T4.2 | 2.74% |
| KXCPIYOY-26MAY-T4.3 | 2.44% |
| KXJOBLESSCLAIMS-26MAY14-205000 | 2.40% |
| KXJOBLESSCLAIMS-26MAY14-200000 | 2.40% |

**No single ticker exceeds 3%.** No per-ticker volume calibration needed.

By event family, KXCPIYOY-26MAY accounts for 27.5% of |z|≥2 noise — concentrated but not pathological, and consistent with that family having the most strike rungs in the watch set.

---

## P&L / Outcome Attribution

Skipped. With zero watch-level alerts, there is nothing to attribute. The shadow ledger (`shadow_pnl/ledger.jsonl`, 1,230 rows) shows active T1 weather trades but no T3-family entries tied to entropy alerts, as expected for Phase 1 defensive-only.

---

## The Call

**Do not enable Phase 2.** Not because the detector is failing — because we have no signal to evaluate.

**Fix order, ranked by leverage:**

1. **Wire volume_5min to the live book stream.** This is the single binding constraint. Until it's non-zero, the detector cannot produce a watch alert no matter what entropy does. Highest priority.
2. **Fix yes_p_5min sampling.** The start/end snapshot is reading from a stale cache. Without real price deltas, `direction` will stay flat and Phase 2 (which gates on direction) is structurally impossible.
3. **Bring T3a online.** Spec calls for three engines; one is missing. Check the engine registry and event topic.

After all three are fixed, restart the calibration window. The entropy math itself is producing reasonable z-scores (7,214 |z|≥2 events, peaks around -5), so once the gates work, watch alerts should start landing within days.

**Re-run this report 7 days after the feeder fixes ship.** If n_calibrated still < 15 at that point, the watch-level thresholds themselves are too tight and need to be revisited.

---

*Generated autonomously by scheduled task `entropy-detector-calibration`.*
