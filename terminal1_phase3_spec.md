# Terminal 1 — Phase 3 Late-Execution Layer Spec

**Status:** DRAFT — gated on Phase 2 validation
**Created:** 2026-04-24
**Author:** context from Steve's late-night thesis + engineering translation

---

## 1. Thesis

Kalshi weather markets price with wide uncertainty until minutes before close. The actual atmosphere, observed via METAR and forecast via TAF, collapses uncertainty to ±1–1.5°F at **T-6 to T-13 hours** — 2× tighter than global NWP ensembles. The gap between "market's belief" and "reality-adjacent belief" in that window is tradable edge that retail and most bots don't capture.

**Target outcome:** 97% reality confidence vs. 90% market implied → per-contract edge of $0.05–0.15 after fees, compounded across 15–40 markets/day when scaled across 6 stations.

---

## 2. What changes vs. Phase 2

| | Phase 2 (current) | Phase 3 (late-execution) |
|---|---|---|
| Forecast sources | GFS + HRRR + ECMWF + AIFS | + **TAF** + **METAR** + station bias |
| Trade window | 12–24h before close | **6–13h** before close (separate engine tag) |
| Uncertainty (σ) | ~3.0°F | **~1.5°F** |
| Edge threshold | $0.08 | $0.05 (tighter because confidence is higher) |
| Position sizing | Flat 10 contracts | **Confidence-weighted** 5–25 contracts |
| Signal cadence | Every 30 min poll | Every 15 min poll during window |
| Engine tag in ledger | `T1` | `T1_late` — kept separate for A/B measurement |

---

## 3. Data sources (all free, all public HTTP)

### 3.1 METAR (observed)
- **Endpoint:** `https://aviationweather.gov/api/data/metar?ids={ICAO}&format=json&hours=6`
- **Cadence:** 20–60 min per station
- **Fields used:** `tmpf`, `dewpf`, `wdir`, `wspd`, `visib`, `wxString`, `obsTime`
- **Pull strategy:** fetch each target station at 15-min cadence during trade window
- **Storage:** `~/Documents/terminal1_data/metar_{station}.jsonl` append-only

### 3.2 TAF (airport forecast)
- **Endpoint:** `https://aviationweather.gov/api/data/taf?ids={ICAO}&format=json`
- **Cadence:** issued at 00Z / 06Z / 12Z / 18Z; occasional amendments
- **Fields used:** `fcsts` array — each has validTimeFrom / validTimeTo, tmpf_max, tmpf_min
- **Pull strategy:** fetch at 00Z/06Z/12Z/18Z + 30 min; cache until next issuance
- **Storage:** `~/Documents/terminal1_data/taf_{station}.jsonl`

### 3.3 Station bias (computed, not pulled)
Per (station, metric, hour-of-day) offset: `mean(NWS_actual_high - ensemble_mean_high)` over last 30 days.
- **Computed from:** existing `nws_actuals_*.jsonl` + `forecasts_*.jsonl`
- **Update cadence:** daily recompute at 06:00 UTC
- **Storage:** `~/Documents/terminal1_data/station_bias.json`

Schema:
```json
{
  "NYC": {
    "high": {"00": +1.8, "03": +1.9, "06": +1.7, "09": +1.2, ...},
    "low":  {"00": -0.3, "03": +0.1, ...}
  },
  "LAX": {...},
  ...
}
```

Hour-of-day because urban heat island is strongest at night (4–6am) and weakest in afternoon.

### 3.4 Depth probe (from existing weather logger)
Before firing any late-stage trade, read the most recent `kalshi_{station}_{date}.jsonl` row for the target ticker. Confirm:
- `yes_ask_size` ≥ target position size, OR
- `no_ask_size` ≥ target position size (derived from `no_bid_size`)
- Spread ≤ 5¢ (tight enough that edge survives)

Skip if depth insufficient.

---

## 4. Signal fusion model

For each (station, target_date, metric=high|low), produce a tightened point estimate `μ*` and uncertainty `σ*`:

```
μ* = 0.50 × TAF_estimate
   + 0.25 × METAR_extrapolation
   + 0.15 × bias_corrected_ensemble
   + 0.10 × climatology_prior

σ* = max(1.5, weighted_stdev_of_sources)
```

### 4.1 TAF_estimate
Parse the TAF forecast valid at target_date's expected high/low time. For daily high: typically 18Z–22Z UTC (afternoon local). For daily low: 08Z–12Z UTC (dawn local). Use the tmpf_max or tmpf_min from the TAF's PROB/TEMPO/FM segments straddling that window.

### 4.2 METAR_extrapolation
Linear regression on the last 6 hours of observed temps to project end-of-day. Simple `y = mx + b` using hour-offset as x. Point estimate at target_date's high/low hour.

### 4.3 Bias correction
For station S, metric M, hour H:
```
bias_corrected = ensemble_mean + station_bias[S][M][H_nearest]
```

### 4.4 Climatology_prior
30-day trailing mean of NWS actuals for that station + metric. Low weight (10%) because it's slow-moving and noisy.

---

## 5. Edge calculation

Same structure as Phase 2:
```
P(ticker_YES) = Normal_CDF_bucket(μ*, σ*, strike_lo, strike_hi)
mkt_P = (yes_bid + yes_ask) / 2
edge = P(ticker_YES) - mkt_P  (if YES side) or mkt_P - P(ticker_YES) (if NO side)
fee_est = 0.02 × contracts
ev_post_fee = edge × contracts - fee_est
```

Fire if:
- `edge ≥ $0.05`
- `ev_post_fee ≥ $0.30` (minimum per-trade payoff to matter)
- `T-13h ≤ hours_to_close ≤ T-6h`
- depth check passes

---

## 6. Confidence-weighted sizing

Unlike Phase 2's flat 10 contracts, Phase 3 sizes per confidence:

```
if |P - mkt_P| ≥ 0.20: size = 25 contracts
elif |P - mkt_P| ≥ 0.12: size = 15 contracts
else (edge ≥ 0.05): size = 10 contracts

# cap: size × entry_price ≤ 5% of engine bankroll
```

Applies natural Kelly-ish discipline without going full Kelly (which is fragile to calibration errors).

---

## 7. Separate engine tag for A/B measurement

Critical: log Phase 3 positions with `engine="T1_late"` not `engine="T1"`. This lets the shadow ledger and dashboard measure:

- **T1 (Phase 2 24h-lead):** hit rate, $/signal
- **T1_late (Phase 3 6-13h lead):** hit rate, $/signal
- **Combined:** total edge

If T1_late doesn't outperform T1 after 50+ settled positions, it's not adding value and should be killed. The separate tag makes this measurable.

---

## 8. Build plan (post Phase 2 GO)

| Stage | Work | Hours |
|---|---|---|
| 1. METAR puller | `terminal1_metar_puller.py` — fetch + append JSONL | 1.5 |
| 2. TAF puller | `terminal1_taf_puller.py` — same pattern | 1.5 |
| 3. Station bias computer | `terminal1_station_bias.py` — compute & write JSON daily | 2 |
| 4. Signal fusion module | function in `terminal1_phase3_late_executor.py` | 2 |
| 5. Late executor daemon | 15-min poll, depth check, fires to ShadowLedger | 2 |
| 6. Dashboard split | show T1 vs T1_late as separate rows | 0.5 |
| 7. Integration test + calibration sanity | dry-run on historical data | 2 |
| **Total** | | **11.5h** |

---

## 9. Gate conditions — do NOT build Phase 3 until these are true

1. **Phase 2 settled sample ≥ 30 trades.** 12 positions tonight is not enough.
2. **Phase 2 hit rate ≥ 70% of our_p.** If we say P=0.20 and hit rate is ≥14%, calibration is acceptable. If hit rate is <10%, fix Phase 2 first.
3. **Phase 2 total P&L ≥ 0 after fees.** Small positive is fine. Deeply negative = calibration is off.

If all three gates pass → build Phase 3.
If any fails → fix Phase 2 calibration first (likely by inflating ensemble σ).

---

## 10. Universal pattern reminder

Late-stage execution is a **cross-engine architectural pattern**. Once the weather late-layer works, clone it to:

- **T3a (Fed decisions):** T-6h to T-24h before FOMC announcement. Dot-plot signal + dealer positioning tighten uncertainty.
- **T3b (CPI):** T-2h when survey consensus + Cleveland Fed nowcast + private estimates sharpen.
- **T3c (GDP/Claims):** T-6h for Atlanta Fed GDPNow final release.

Same architecture: long-range forecast + late-window data source + calibration → tighten σ → fire into shadow ledger at tighter thresholds.

---

## 11. Open questions to resolve during build

1. Exact weights (0.50/0.25/0.15/0.10) — needs empirical tuning on held-out historical data.
2. TAF fallback when stale — how old is "too stale"? 6h? 12h?
3. Extreme weather regime detector — if a storm is moving through, all models + obs may fail simultaneously. Need a "skip trading today" flag for major weather events.
4. Time-of-day alignment — daily high/low times drift with season. Sep high is earlier than Jun high. Season-aware targeting.

---

*End of spec. Build upon Phase 2 GO.*
