# Entropy Detector Calibration — 2026-05-14

**Status: BLOCKED. Calibration is not progressing and cannot progress under current config.**

## The numbers

- Total alerts logged (since 2026-05-09): **108,740**
- Watch-level alerts: **0**
- Confirmed-level alerts: **0**
- All 108,740 alerts are `noise`.
- n_calibrated = **0 / 15** required for Phase 2 gate.

Per engine: T3b 94,630 — T3c 14,110 — T3a 0 (no alerts emitted).
Per day: 05-09: 6,916 / 05-10: 21,660 / 05-11: 21,736 / 05-12: 21,772 / 05-13: 22,436 / 05-14: 14,220.

## Root cause — the volume feed is dead

Every alert fails the liquidity gate. `liquidity_pass = False` on all 108,740 rows. Reason:

- `volume_5min = 0.0` on **100%** of alerts.
- `volume_baseline_median = 0.0` on **100%** of alerts.

No volume data is reaching the detector. The liquidity gate can never pass, so no alert can ever escalate above `noise`. Calibration will sit at n=0 indefinitely until the volume feed is fixed. This is the only thing that matters in this report.

## Secondary data-quality issue

8 alerts carry garbage z-scores (|z| up to ~9e14) — divide-by-near-zero when `entropy_baseline_std` is ~0. Add a floor on the std denominator (e.g. clamp to 1e-3) or drop rows where baseline_std < threshold. Low priority vs. the volume feed but it will pollute stats once volume is live.

## False-positive concentration

Not a problem. Top single ticker = 1.3% of noise; top event (KXCPIYOY-26MAY) = 35%, which is legitimate — it's the largest CPI market by strike count. No illiquid ticker gaming the volume gate. No per-ticker calibration needed.

## Settlement attribution

N/A — zero calibrated alerts to attribute. shadow_pnl/ledger.jsonl has only 6 T3b / 32 T3c rows and no close rows for these engines yet; nothing to score against regardless.

## The call

- **Phase 2: stays OFF.** Gate is n>=15, actual is n=0. Not close.
- **Action required: fix the volume_5min feed in the detector.** Until volume data flows, this scheduled task will report n=0 every day and the detector delivers zero defensive value (a `noise` alert blocks nothing).
- No operator ping triggered by the task's decision logic (n < 15 = status only). Flagging the feed break here because it's a silent failure — the detector looks alive (108K rows) but is functionally dead.

Next run will re-check. If volume is still 0.0 across the board tomorrow, treat the detector as down, not calibrating.
