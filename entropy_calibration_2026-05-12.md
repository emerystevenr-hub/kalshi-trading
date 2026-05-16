# Entropy Detector — Calibration Report
**Run:** 2026-05-12 (auto) · **Window:** 2026-05-09 16:05 UTC → 2026-05-12 15:09 UTC (~71h)

## Headline
**n_calibrated = 0.** Status update only. No Phase 2 recommendation.
Detector is firing but **100% of alerts are being suppressed at noise level by the liquidity gate.** Calibration cannot progress under current configuration.

## Counts
- Total alerts logged: **63,992**
- Watch-level: **0**
- Noise-level: 63,992 (100%)
- liquidity_pass=true: **0**

## Per-engine
- T3b: 55,572 (86.8%)
- T3c: 8,420 (13.2%)
- **T3a: 0** — not present in alert stream at all. Either disabled, writing elsewhere, or broken.

## Per-direction
- flat: 63,992 (100%)
- rising_yes / rising_no / fading_collapse: **0**

Direction classifier never triggered. Cross-checked: `yes_p_5min_start == yes_p_5min_end` on **every single alert (63,992 / 63,992)**. The 5-min price snapshot is either being captured twice from the same source or the start/end pull is racing.

## Why nothing promoted
The entropy signal IS moving. 412 alerts have |z| ≥ 5; the extreme was **z = −87.54** on KXCPIYOY-26MAY-T4.1 (2026-05-11 22:48 UTC). Despite that:
- `volume_5min == 0.0` on **all 63,992 rows**
- `volume_baseline_median == 0.0` on the rows checked
- `liquidity_pass == false` everywhere

Two possible causes — both block Phase 2 indefinitely:
1. **Volume not being populated** from the market data feed (most likely — zero on every row including high-traffic CPI strikes is implausible).
2. **Strike-ladder markets simply don't print volume in 5-min windows pre-event**, in which case the volume gate is the wrong filter for T3b/T3c and needs to be replaced with a bid/ask staleness or quote-update-count gate.

## Coverage
All alerts concentrated in 4 events:
- KXCPIYOY-26MAY: 22,734 (35.5%)
- KXCPIYOY-26APR: 19,366 (30.3%)
- KXCPIYOY-26JUN: 13,472 (21.1%)
- KXJOBLESSCLAIMS-26MAY14: 8,420 (13.2%)

86.9% of the surface is CPI-YoY strike ladders. Not a false-positive concentration problem (nothing's a positive), but a coverage problem if T3a was meant to expand the watch list.

## Per-day volume
- May 09 (partial): 6,916
- May 10: 21,660
- May 11: 21,736
- May 12 (partial): 13,680

Steady ~21.7k/day. Detector is healthy at the firing layer.

## Calls (no operator ping per spec)
1. **Fix the volume feed or replace the gate.** Until `volume_5min` populates non-zero or the gate is rewritten, n_calibrated stays at 0 forever.
2. **Audit the 5-min price snapshot.** `yes_p_5min_start == yes_p_5min_end` on 100% of rows means direction classification is dead-on-arrival. The detector cannot emit `fading_collapse` direction even if liquidity passed — Phase 1 defensive logic is also not actually triggering.
3. **Verify T3a is wired in.** Zero alerts in 71 hours from the only engine intended for kalshi-market read-only watching.
4. **Phase 2 status: blocked.** Not by hit rate, not by data — by detector plumbing. Re-run this report 24h after the gate fix.

## Files
- Alerts: `~/Documents/entropy_alerts.jsonl` (63,992 rows, 33 MB)
- Ledger: `~/Documents/shadow_pnl/ledger.jsonl` (1,003 rows; 23 tagged T3* but all from core nowcast/edge logic, not entropy detector)
