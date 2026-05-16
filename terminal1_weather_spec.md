# Terminal 1 — Weather Ensemble Bot Spec

**Status:** Draft v0.1
**Date:** 2026-04-22
**Owner:** Steve
**Replaces:** Engine 1 (`kalshi_shadow_v4.py`, retired)

---

## 1. Mission

Automated, US-legal bot on Kalshi weather markets (KXHIGH / KXLOW). Structural edge: multi-model NWP ensemble vs. retail pricing. Low frequency, wallet-flat daily, no latency war. Target Year 1: $40–80k net PnL on $10–25k deployed capital. Stretch: $100k+ if edge holds across seasons.

The explicit non-goal: 50x. This is a cash-flow engine, not a venture-return engine.

---

## 2. Venue and Instruments

- **Venue:** Kalshi (`api.elections.kalshi.com/trade-api/v2`)
- **Markets:** `KXHIGH*` (daily high temp) and `KXLOW*` (daily low temp) for named NWS stations
- **Priority stations** (ranked by Kalshi liquidity):
  1. KNYC / KLGA (New York)
  2. KORD (Chicago)
  3. KLAX (Los Angeles)
  4. KDEN (Denver)
  5. KATL (Atlanta)
  6. KMIA (Miami)
  7. KPHX (Phoenix)
  8. KDFW (Dallas)
- **Contract shape:** Binary YES/NO on whether that day's high/low lands in a specified temperature bucket (e.g., `KXHIGHNY-26APR22-B72`)
- **Resolution source:** Official NWS observation at the station
- **Resolution time:** Typically 8pm–midnight ET same day
- **Fees:** Kalshi taker: `$0.07 × p × (1-p)` ceil to cent, per contract, per fill

---

## 3. Data Sources

### 3.1 Forecast Models (Ensemble Inputs)

| Model | Source | Cost | Runs/Day | Resolution | Horizon |
|-------|--------|------|----------|------------|---------|
| GFS | NOAA NOMADS | Free | 4 (00/06/12/18 UTC) | 13 km | 0–384 h |
| ECMWF HRES | ECMWF Open Data | Free | 2 (00/12 UTC) | 9 km | 0–240 h |
| HRRR | NOAA NOMADS | Free | 24 (hourly) | 3 km | 0–48 h | *(see note)* |
| AIFS | ECMWF Open Data | Free | 2 (00/12 UTC) | 28 km | 0–240 h |
| GEFS | NOAA NOMADS | Free | 4 | 25 km | 30 members |
| ECMWF ENS | ECMWF Open Data | Free | 2 | 18 km | 50 members |

**Launch set (4 models):** GFS, ECMWF HRES, HRRR, AIFS. Add GEFS/ENS in Phase 2 for uncertainty bands.

**Phase 1 amendment (2026-04-23):** HRRR is **excluded from the Phase 1 ensemble** due to NOMADS retention limits (~2 days). The 30-day backfill is not obtainable from the current source path — the NOMADS backfill returned 404 on cycles older than ~48 hours and 403 on cycles older than ~2 weeks. Phase 1 runs with **GFS + ECMWF HRES + AIFS (3 models)**. HRRR can be restored in Phase 2 by repointing the puller at `s3://noaa-hrrr-bdp-pds/` (AWS NOAA Open Data, full archive, no auth). This is not a blocker for the Phase 1 go/no-go decision: we are testing whether the weather thesis has enough signal to justify continued work, not building the final ensemble.

### 3.2 Ground Truth / Climatology

- **NWS METAR** hourly obs (NOAA, free)
- **ASOS 1-minute data** for verification (NCEI, free)
- **1991–2020 Climate Normals** (NCEI, free) for seasonal baseline

### 3.3 Market Data

- **REST:** `/markets`, `/events`, `/portfolio` (authenticated)
- **WS:** `market_ticker_v2`, `orderbook_snapshot`, `orderbook_delta`
- **Poll cadence:** Snapshot book at T-4h, T-2h, T-1h, T-30m, T-15m before close

---

## 4. Ensemble Construction

### 4.1 Bias Correction (MOS-style)

- Maintain rolling 90-day log of `(forecast, actual)` pairs per `(station, model, lead_time)` cell
- Compute per-cell bias: `mean(forecast - actual)`
- Apply bias correction before ensemble combination

### 4.2 Skill Weighting

- Weight each model by 30-day rolling CRPS at that station/lead-time
- Constraints: no single model weight > 0.50; min weight 0.10 if included
- Re-weight Sunday overnight

### 4.3 Output: Probability Distribution

- For each `(station, day, strike_bucket)`: produce `model_p = P(high lands in bucket)`
- Method: fit empirical KDE over bias-corrected ensemble member temps, integrate over each bucket
- **Minimum 4 models** must have fresh runs (< 6h old) to produce a signal

---

## 5. Edge Gate

Fire a trade ONLY when all of:

```
edge = model_p - market_ask - fee_per_contract(market_ask)
edge ≥ MIN_EDGE_CENTS
```

Launch value: **`MIN_EDGE_CENTS = 0.05`** (5 cents of expected profit per $1 of payoff).

All of these must also hold:
- Ensemble direction agreement: ≥ 3 of 4 primary models on same side of the strike
- Forecast horizon ≤ 72 h (longer = model skill too weak)
- Market daily volume ≥ $500
- Book depth: ≥ $50 quoted at the ask
- Time to resolution: 30 min ≤ TTR ≤ 48 h
- Ensemble spread ≤ 5°F (if members disagree too much, pass)

---

## 6. Execution Rules

- **Order type:** IOC taker at or below current ask
- **No maker quoting.** That was Engine 1's failure mode.
- **One-shot entry** per signal per market. No scaling in.
- **No averaging down.**
- **Exit:** Hold to resolution. No mid-life exits — removes path-dependency risk and time-of-day slippage.
- **Exception:** NWS issues a revised-station-data advisory for the target day → flat at any price.

---

## 7. Position Sizing

- **Max per market:** $500 (5% of $10k bankroll)
- **Max concurrent positions:** 10
- **Max total open notional:** $5,000 (50% of bankroll)
- **Sizing rule:** `position = min($500, 0.25 × kelly_optimal × bankroll)`
  - Quarter-Kelly to survive model miscalibration
- **Bankroll update:** End-of-day after all resolutions settle

---

## 8. Fire Discipline (Hard Anti-Trade Rules)

Skip the signal if ANY of:
- Any required model pull failed in last 6 h
- < 60 days of (forecast, actual) pairs at this station for this model (insufficient calibration)
- Ensemble spread > 5°F
- NWS Special Weather Statement active at station
- |ensemble_mean − actual| > 8°F on any of last 3 days at this station (regime break)
- Bankroll drawdown > 20% from peak (manual review gate)
- It's the first run after a model version change or major Kalshi fee schedule change

---

## 9. Risk Bounds / Kill Switches

| Trigger | Action |
|---------|--------|
| Daily realized loss ≥ $300 | Halt new entries. Existing positions hold to resolution. |
| Weekly realized loss ≥ $800 | Full halt. Manual re-enable. |
| Monthly realized loss ≥ $2,000 | Strategy review. Consider kill. |
| Drawdown from peak ≥ 30% | Full halt. Manual revive only. |
| Model pipeline uptime < 95% rolling 7d | Halt until fixed. |

---

## 10. Paper-Trade Validation Protocol

Mandatory. No capital until all phases pass.

### Phase 1 — Backtest (5 days of build time)
- Historical forecasts (last 90 days) × historical Kalshi closes (public)
- Measure: hit rate, edge captured, Sharpe, max DD, calibration reliability diagram
- **Pass bar:** Sharpe ≥ 1.5, calibration ±5% across buckets, EV positive after fees

### Phase 2 — Paper Live (14 days)
- Full pipeline in real time, signals logged, **no execution**
- **Pass bar:** ≥ 20 clean signals, paper P&L positive, pipeline uptime ≥ 95%

### Phase 3 — Micro Capital (14 days)
- $1,000 bankroll, $50 max position, 5 concurrent max
- Compare realized vs. paper P&L
- **Pass bar:** Realized ≥ 70% of paper (execution quality sanity check)

### Phase 4 — Full Capital
- Scale to $10k → $25k over 30 days
- Weekly P&L review, monthly strategy audit

---

## 11. Go / No-Go Gates

**GO to next phase:**
- All pass bars met
- No unresolved systems failures > 5 min

**KILL (any one):**
- Phase 1 Sharpe < 0.8
- Phase 3 realized < 50% of paper
- Pipeline reliability < 95% at any phase

---

## 12. Telemetry & Observability

Log to `~/Documents/terminal1_logs/` (rotating daily):
- Every signal fired, skipped, or rejected with reason code
- Every order submitted + fill status + realized slippage vs. quoted
- Per-model pull status, latency, freshness
- Bankroll curve, drawdown, Sharpe rolling
- Daily summary written to `terminal1_daily_YYYY-MM-DD.json`

Monitor alerts (email/SMS on):
- Any kill-switch trigger
- Model pull failure > 2 consecutive runs
- Kalshi auth failure
- Drawdown ≥ 15% from peak

---

## 13. Open Questions (need answers before code)

1. **Bankroll at launch:** $10k / $15k / $25k?
2. **ECMWF Open Data account:** free signup required. You handle or I document?
3. **Compute:** Local Mac mini, or small cloud box (Hetzner $20/mo, AWS $30–60/mo)? GRIB files are ~15 GB/day total pull + processing.
4. **AI model:** AIFS (ECMWF-operational, simpler) vs GraphCast (more setup). Recommend **AIFS**.
5. **Build timeline:** 2–3 days focused = Phase 1 backtest deliverable. Acceptable?
6. **Station scope:** Start with 3 stations (NYC/ORD/LAX) and expand, or all 8 from day one? Recommend **start with 3** — tighter feedback loop.

---

## 14. What This Spec Is NOT

- Not a venture-return engine. If 50x is the actual goal, this isn't it — that's Fornix / SimpleBooks.
- Not a maker strategy. Purely taker, IOC only.
- Not a news/event bot. Purely physics-model-driven.
- Not a scaled-up strategy. Kalshi weather liquidity caps realistic capital at $25–50k. Beyond that, slippage eats the edge.

---

## 15. Success Definition

**By Day 90:** Phase 3 complete, micro capital deployed, realized P&L ≥ 70% of paper.
**By Day 180:** Full capital deployed, positive cumulative P&L, Sharpe ≥ 1.0 realized.
**By Day 365:** $40–80k net PnL, no kill-switch triggers requiring manual revive more than twice in the year.
