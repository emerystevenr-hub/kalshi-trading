# Terminal 3b — CPI Nowcast Engine Spec (Phase 1)

**Bot identifier:** `T3b_CPI_Nowcast`

_Version 0.3 — 2026-04-26 (post second red-team)_
_First settlement gate: 2026-05-13 BLS CPI release for April 2026._

**Changelog v0.2 → v0.3** (Steve red-team #2, 2026-04-26):
- §4.1 Backfill script: logs `days_to_release` at *nowcast publish time* (not scrape time) — avoids look-ahead bias
- §4.1 Backfill summary prints `mean_residual` per lead bucket; if ≥0.05% bias correction lands in Phase 1.5
- §4.4 (NEW) Stale-nowcast safety: skip event if Cleveland Fed nowcast >48h old
- §5.1 Hard cutoff: no trades at >7 days lead, period. Structural arb is the only exception (scan-only in Phase 1, so moot for now)
- §5.2 YES guardrail adds `yes_bid ≤ 0.85` (no heavy-favorite YES — post-fee edge is tiny)
- §5.3 Volume filter scales to ≥200 24h-volume in 1–3 day window (liquidity ramps near print)
- §6.5 Arb scanner logs `persistence_seconds` per violation — critical for Phase 2 execution decision
- §10.4 Government shutdown handling: reconciler must not auto-close on `expected_expiration_time`; use `latest_expiration_time` as hard ceiling
- §13 (NEW) Logging discipline — every accept/reject is coded for later analysis

**Open questions resolved** (closing v0.2 carryovers):
- YES guardrail position size: full 25 contracts (the entry rule is already restrictive enough; adding a size penalty on top is over-correction)
- Atlanta Fed divergence threshold: 0.30% (3× typical noise; tighten in Phase 2 if regime stress days are too rare to be informative)

## 1. Strategic objective

Capture edge from the structural gap between **professional nowcasts** and **retail-driven Kalshi pricing** on monthly CPI prints.

The thesis is well-documented: on near-cast horizons (≤30 days to release), the Cleveland Fed Inflation Nowcast and similar institutional models have measurably lower forecast error than Bloomberg/Reuters consensus surveys, which are themselves better than aggregate retail flow on prediction markets. Kalshi CPI markets are populated by retail traders with low awareness of these nowcasts. Edge = systematic exploitation of that information gap.

This is calendar-driven, not opportunistic. There are 12 CPI prints per year. Phase 1 attacks one (May 13). Phases 2+ extend to monthly cadence.

## 2. Market universe (Phase 1)

**Series:** `KXCPIYOY` (Kalshi Y/Y CPI inflation, monthly events)
**Phase 1 event:** `KXCPIYOY-26APR` (April 2026 CPI, releases 2026-05-13)
**Strike grammar:** `KXCPIYOY-{YYMMM}-T{X.X}` where T = threshold_above

Live snapshot taken 2026-04-26 16:33 UTC (sample, prices in YES bid):

| Strike | Yes bid | Implied P(CPI > X%) |
|---|---|---|
| 2.3% | 0.98 | 98% |
| 3.4% | 0.94 | 94% |
| 3.5% | 0.75 | 75% |
| **3.6% (modal)** | **0.38** | **38%** |
| 3.7% | 0.16 | 16% |
| 3.8% | 0.06 | 6% |
| 4.0% | 0.02 | 2% |

**Implied market mean ≈ 3.55%, σ ≈ 0.10% (very tight).**

23 active strikes total. Some sub-3.5% strikes show non-monotonic pricing (>2.5%@0.98 but >2.6%@0.99) — those are **structural arbs** and a free Phase-1 sanity check on our infrastructure.

**Close time:** ~24h before BLS release. For 26APR event: `2026-05-12T12:29:00Z` (Mon morning before Tue 8:30 AM ET release).

## 3. Data sources

### 3.1 Forecast (μ): Cleveland Fed Inflation Nowcast
- URL: `https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting`
- Cadence: daily updates, multiple horizons (next-month CPI YoY is the relevant one)
- Format: HTML page renders a table of nowcasts; need to scrape OR find their CSV download
- **Build action item:** locate and lock onto a stable endpoint (CSV, JSON, or HTML row regex). Fallback path: NY Fed Weekly Economic Index, Atlanta Fed GDPNow alternatives.
- Free, no auth.

### 3.2 Consensus survey (calibration check, not forecast input)
- Investing.com economic calendar (`investing.com/economic-calendar/cpi-733`) — has consensus alongside actuals
- TradingEconomics (`tradingeconomics.com/united-states/inflation-cpi`) — backup
- Used as a calibration check: how does our nowcast-driven forecast compare to consensus, and how does each compare to actual? If nowcast doesn't beat survey on residuals, the thesis fails.

### 3.3 Actuals (truth): BLS API
- Endpoint: `https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0`
- Series: CUUR0000SA0 = CPI-U All Items, NSA. _(YoY computation is `(month_index / month_index_12_ago - 1) × 100`, rounded to 1 decimal — same rounding Kalshi uses to settle.)_
- Verified working (probe pulled Mar 2026 = 330.213 cleanly).
- Free, no auth required for ≤25 queries/day. Register a free key for higher limits during Phase 2.

### 3.4 Kalshi snapshots
- Public endpoint: `https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker={EVENT}&limit=200`
- No auth needed for read.
- Returns full bid/ask/volume/OI per strike.

## 4. Forecast model (Phase 1)

```
μ = Cleveland Fed CPI YoY nowcast (most recent value)
σ = empirical residual std as f(days_to_release)
P(CPI > strike) = 1 - Φ((strike - μ) / σ)
```

### 4.1 σ as a function of lead time

T1's lesson: empirical σ beats heuristic. Apply that from day one.

**Pre-launch (before May 13)**: backfill 24 months of `(Cleveland Fed nowcast value, BLS actual)` pairs by scraping the Cleveland Fed archive. For each pair record `days_to_release` **at the time the nowcast was PUBLISHED** (not at scrape time — avoids look-ahead bias by ensuring our σ fit reflects what we'd actually have known in real time). Fit `σ_residual` per lead bucket:

| Bucket | Pairs needed | Initial σ if backfit fails |
|---|---|---|
| `≤3 days` | ~24 | 0.10% × 1.0 = 0.10% |
| `4–7 days` | ~24 | 0.10% × 1.2 = 0.12% |
| `8–14 days` | ~24 | 0.10% × 1.5 = 0.15% |
| `>14 days` | ~24 | 0.10% × 2.0 = 0.20% |

Save table to `~/Documents/terminal3b_data/empirical_sigma.json` (mirrors T1 layout).

**Post-launch refit**: after each new BLS print, append the (μ, actual) pair across all the lead points we observed and re-fit. By print 3 we have the full empirical curve, no heuristic backstop needed.

**Backfill summary output (mandatory)**: the backfill script must print `mean_residual` and `n` per lead bucket at the end of its run, not just `std_residual`. We need to see signed bias up-front, not discover it three prints in.

### 4.2 Bias correction

**None in Phase 1 by default.** But the backfill summary above will tell us immediately if it's needed:
- If any bucket shows `|mean_residual| ≥ 0.05%` → add static bias correction to Phase 1.5 (do not wait for live data)
- If all buckets show `|mean_residual| < 0.05%` → no correction needed; revisit after print 1

T1's lesson: don't pretend bias is zero when the data says otherwise.

### 4.3 Forecast cross-check (anomaly only, not blended)

Pull **Atlanta Fed Sticky-Price CPI** as a sanity check. If `|Atlanta_Sticky_YoY - Cleveland_Fed_nowcast| > 0.30%`, flag as DIVERGENCE and skip trades that day. They use different methodologies; large divergence = regime stress + low confidence in either point estimate.

### 4.4 Stale nowcast safety

If the most recent Cleveland Fed nowcast value is **>48 hours old** at signal-generation time, **skip the entire event** — no positions opened across any strike. Cleveland Fed updates daily; >48h staleness implies their pipeline is down or they paused publication. We don't trade on cached forecasts during such periods.

This is a hard gate, not a soft warning. Logged with `reject_reason=stale_nowcast`.

## 5. Edge calculation + entry rules

```
our_p = P(CPI > strike) per the model
market_p = (yes_bid + yes_ask) / 2

if our_p > market_p: side = YES, entry = yes_ask, edge = our_p - market_p
if our_p < market_p: side = NO,  entry = 1 - yes_bid, edge = market_p - our_p
```

### 5.1 Lead-time-scaled edge filter

Edge bar grows with σ (which grows with lead time):

| `days_to_release` | min_edge | Position size |
|---|---|---|
| **>7 days** | **NO TRADES** (paper trader returns no signals) | — |
| 4–7 days | $0.20 | 12 contracts |
| 1–3 days | $0.10 | 25 contracts (full size) |
| < 24 hours | NO NEW POSITIONS | — |

Rationale: at >7 days lead, σ is too wide for any directional CPI bet to clear noise after fees. The proven CPI edge window is the final 7 days. Last 24h is efficient pricing — retail caught up, no edge worth chasing. Structural arb (§6.5) is the only thing that ever fires outside the 1–7 day window, and in Phase 1 it's scan-only anyway.

### 5.2 Asymmetric YES-side guardrail (T1 lesson encoded)

T1 calibration showed YES on extreme tails was the failure mode (cal=0.35). T3b explicitly bans the equivalent setup:

**NO side**: standard edge filter applies.

**YES side requires ALL of:**
- Edge ≥ table threshold + $0.05
- Strike within ±0.3% of nowcast μ (no far-tail YES bets)
- `yes_bid ≥ $0.10` (no penny bets on extreme tails — the T1 failure mode)
- `yes_bid ≤ $0.85` (no heavy-favorite YES — post-fee edge is tiny when paying $0.85+ for a $1.00 payoff)

Rationale: Cleveland Fed nowcasts are documented to be better calibrated on the high-inflation side than on tails. The $0.10–0.85 band is the zone where YES has meaningful asymmetric upside. Outside it, fees + uncertainty eat the edge.

### 5.3 Filters (universal, scaled by lead time)

- **Liquidity floor**, scales with lead time:
  - 4–7 days lead: `volume_24h ≥ 50 OR open_interest ≥ 500`
  - 1–3 days lead: `volume_24h ≥ 200 AND open_interest ≥ 500` (liquidity is supposed to ramp near print — if it didn't, the strike is dead and we don't want fills there)
- `not already_open(ticker)` — dedupe
- `not divergence_flag_today` — skip if Atlanta Fed Sticky disagrees with Cleveland Fed by >0.30%
- `not stale_nowcast` — skip if Cleveland Fed nowcast >48h old (§4.4)

### 5.4 Cluster cap

**Max 7 strikes per (event_ticker)**, top-N by edge.

Rationale: every CPI strike is correlated to the same outcome — holding 10 strikes is one bet expressed 10 ways, not 10 independent bets. 7 strikes covers above-mean / at-mean / below-mean expression without inflating concentration risk on a single inflation print.

**Cross-event cap:** 1 active CPI event at a time during Phase 1.

## 6. Position sizing

```
DEFAULT_CONTRACTS = 25  (full size, used at 1–3 days lead)
SHORT_LEAD_CONTRACTS = 12  (used at 4–7 days lead)
fee_estimate = 0.02 / contract
```

At Kalshi price points (3.5% bucket = $0.75 YES bid): 25 contracts ≈ $18.75 cost basis. With 7 strikes max per event, **total Phase-1 cost basis cap ≤ $200/print, ≤ $750 across the full Apr→Jul paper validation window**. Doesn't compete with T1 for bankroll.

## 6.5 Structural arb scanner (Phase 1 = scan-only)

Separate module: `terminal3b_arb_scanner.py`. Different edge type, deterministic, doesn't need calibration math.

**Scope:** for each CPI event, scan the strike grid for monotonicity violations:

- "Above X%" probabilities should be monotonically decreasing as X increases
- Any inversion (e.g., `P(>2.5%) < P(>2.6%)`) is a stat-arb opportunity
- Spread between adjacent strikes should be ≥ 0 (probability mass cannot be negative)

**Phase 1 behavior:**
- Snap every 5 minutes alongside the main logger
- Log every detected violation to `terminal3b_data/arb_scan.jsonl` with fields:
  - `ts_utc`
  - `event_ticker`
  - `strike_pair` (e.g., `["T2.5", "T2.6"]`)
  - `prices` (yes_bid/yes_ask for each leg)
  - `violation_magnitude_cents`
  - `persistence_seconds` — accumulated time the same violation has been observable across consecutive snaps (resets when violation closes; this tells us if mis-pricings are persistent enough to execute on)
  - `liquidity_min` — min volume_24h across the two legs (filter out ghost-orderbook violations)
- **Do NOT fire shadow trades.** Phase 1 builds the dataset; we don't yet know if these arbs are real (could be stale orderbook data on illiquid strikes) or how long they persist.

**Phase 2 decision** (after first print): if violations are real, persistent (`persistence_seconds ≥ 300`), and ≥$0.02 wide → build atomic two-leg execution module. If they're transient sub-second flickers → archive the scanner. The `persistence_seconds` field is the single most important data point for this decision.

## 7. Calibration framework

**Reuse T1 infrastructure unchanged.** Tag all opens `engine="T3b"`. The shadow_pnl ledger schema, side-aware `_model_win_prob`, settlement_analysis, calibration_trend cron — all compatible with engine filter.

**Engine-specific reports:** patch `t1_settlement_analysis.py` to optionally filter by engine, OR fork to `t3b_settlement_analysis.py`. Decision: **fork**. Different print cadence means daily/weekly trend tables aren't comparable across engines; clean separation reads better.

**Decision criteria (locked Phase 1):**
- **SHIP** to real money: after **3 settled prints** (≥30 settlements assuming ~10 strikes/print), side-aware YES cal ∈ [0.7, 1.3], P&L > 0
- **ITERATE**: 3 prints, cal outside band but ≥0.3 → refit σ from empirical residuals
- **KILL**: cal < 0.3 OR P&L < -10% on cost basis

## 8. File layout

```
~/Documents/
  terminal3b_cpi_spec.md                    (this doc)
  terminal3b_kalshi_logger.py               (KXCPIYOY market snapshots)
  terminal3b_nowcast_puller.py              (Cleveland Fed primary)
  terminal3b_atlanta_sticky_puller.py       (Atlanta Fed Sticky CPI cross-check)
  terminal3b_nowcast_backfill.py            (24-month historical backfill, run ONCE)
  terminal3b_bls_actuals.py                 (BLS CPI-U actuals)
  terminal3b_paper_trader.py                (signal generation + shadow opens)
  terminal3b_arb_scanner.py                 (structural arb detection, scan-only)
  terminal3b_settlement_reconciler.py       (close on print)
  t3b_settlement_analysis.py                (engine-filtered cal report)
  terminal3b_data/
    kalshi_KXCPIYOY-26APR_{YYYY-MM-DD}.jsonl
    nowcast_cleveland_fed.jsonl              (append-only, all observed values)
    nowcast_atlanta_sticky.jsonl             (cross-check series)
    nowcast_history.jsonl                    (24-month backfill, frozen)
    bls_cpi_yoy.jsonl                        (BLS actual on print day)
    empirical_sigma.json                     (lead-time-bucketed σ, refit each print)
    arb_scan.jsonl                           (structural arb violations, Phase 1 = log-only)
```

Engine tag in shadow_pnl ledger: `"engine": "T3b"`.

## 9. Phase plan

| Window | Milestone | Owner |
|---|---|---|
| **Apr 27** | Spec sign-off (this doc) | Steve review |
| Apr 27–28 | `terminal3b_kalshi_logger.py` written + first snapshot of KXCPIYOY-26APR | Code |
| Apr 28 | `terminal3b_nowcast_puller.py` — locate stable Cleveland Fed endpoint, write parser | Code |
| Apr 29 | `terminal3b_bls_actuals.py` — confirmed against BLS API, scheduled poll for 5/13 release | Code |
| Apr 30 | `terminal3b_paper_trader.py` — first dry-run, log signals only | Code |
| May 1–11 | Paper trader runs daily; positions accumulate; reconciler waits | Daemon |
| **May 13 8:30 ET** | BLS releases April CPI; reconciler closes positions within 60 min; settlement_analysis runs | Auto |
| May 13 PM | First T3b calibration report (n=10ish, single-print sample, no decision yet) | Steve review |
| Jun 11 | Second print (KXCPIYOY-26MAY) | Daemon |
| Jul 15 | Third print (KXCPIYOY-26JUN). **First decision gate.** | Steve decision |

## 10. Open questions / risks

1. **Cleveland Fed endpoint stability.** No public CSV/JSON API documented. Will scrape the HTML rendering and validate row format on every pull; alert if format changes. Backup plan: NY Fed Underlying Inflation Gauge (UIG) — different methodology but useful as cross-check.
2. **σ from 24-month backfit.** Mostly addressed by §4.1, but: if Cleveland Fed archive has gaps (publication holidays, methodology revisions) the per-bucket n could be uneven. Backfill summary will show n; cells with n<10 fall back to global per-metric σ.
3. **Settlement timing.** BLS releases at 08:30 ET; Kalshi resolves within hours. Reconciler must run within the 5–12h window after release for fast settlement and visibility before next-month strikes open.
4. **Government shutdown / BLS delay handling.** Per Kalshi market rules, BLS delays extend market expiration. Reconciler must NOT auto-close positions on `expected_expiration_time` (e.g., 2026-05-12T14:00:00Z for 26APR) — it must use `latest_expiration_time` (which Kalshi pushes forward on delay) as the hard ceiling. While a position is between expected and latest, leave it open with a flag `_delayed_settlement=true`. Reconciler exits with no action; settlement waits for Kalshi to publish a result.
5. **CPI market liquidity.** Some 26APR strikes show 0 volume_24h. The lead-scaled volume filter in §5.3 handles this for trade entry; the arb scanner's `liquidity_min` field handles it for arb logging.
6. **Correlation across strikes.** Trading 7 strikes on one print is a single bet on whether CPI prints high vs low. P&L is correlated, not independent. Cluster cap of 7 is the *single-event* max risk, not 7× a single position. Size accordingly.
7. **Extreme retail flow on print day.** Volume can 10× in the final 60 minutes before close. We deliberately exclude this window (§5.1: no new trades <24h before close) — but existing positions ride through. Don't try to add or trim during the print-day spike.

## 11. Operator preferences (inherited from Steve's main spec)

- First principles, outcomes > activity, direct, no fluff
- One thing at a time
- Caffeinate everything long-running
- Empirical over heuristic
- Don't ship to real money without ≥3 calibration data points

## 13. Logging discipline

Every accept/reject decision in the paper trader is logged with a structured reason code, not a free-text string. This makes post-print analysis trivial (grep + count by code).

Reason codes (universe):
- `accept_yes`, `accept_no` — opened the position
- `reject_edge_below_min` — edge under lead-scaled threshold
- `reject_lead_too_long` — >7 days to release
- `reject_lead_too_short` — <24h to release
- `reject_yes_outside_band` — strike outside μ±0.3% on YES side
- `reject_yes_bid_low` — yes_bid <$0.10
- `reject_yes_bid_high` — yes_bid >$0.85
- `reject_volume_low` — below lead-scaled volume floor
- `reject_already_open` — dedupe
- `reject_cluster_cap` — 7 strikes already on this event
- `reject_divergence` — Atlanta Fed Sticky disagrees with Cleveland Fed by >0.30%
- `reject_stale_nowcast` — Cleveland Fed nowcast >48h old

Log line format (mirrors T1):
```
[ts] ticker  side  strike  edge=$X.XXX  reason=<code>  detail=<extra context>
```

## 14. Sign-off checklist (v0.3)

Before code starts, Steve confirms:

- [x] Phase 1 attacks CPI only (not jobs, retail sales, GDP)
- [x] First print = 26APR (release 2026-05-13)
- [x] Forecast = Cleveland Fed nowcast μ + Atlanta Fed Sticky-Price as divergence flag (skip if |Δ| > 0.30%)
- [x] σ = lead-time-bucketed empirical residual std, **24-month backfill before first trade, with `days_to_release` recorded at PUBLISH time** (no look-ahead)
- [x] Backfill summary prints `mean_residual` per bucket; trigger Phase 1.5 bias correction if any |mean_residual| ≥ 0.05%
- [x] Edge filter scaled: NO TRADE >7d / $0.20 (4–7d) / $0.10 (1–3d) / NO TRADE <24h
- [x] YES guardrail: edge ≥ table+$0.05, strike within μ±0.3%, $0.10 ≤ yes_bid ≤ $0.85
- [x] Volume floor scales: ≥50 (4–7d) / ≥200 (1–3d)
- [x] Cluster cap = **7** strikes per event
- [x] Cost basis cap ≤ **$200 per event**, ≤ $750 across the Apr→Jul validation window
- [x] Stale-nowcast safety: skip event if Cleveland Fed nowcast >48h old
- [x] Structural arb scanner = scan-only in Phase 1; logs `persistence_seconds` for Phase 2 decision
- [x] Government shutdown handling: reconciler uses `latest_expiration_time`, not `expected_expiration_time`
- [x] Logging: every decision logged with structured reason code (§13)
- [x] Engine tag T3b, reuse shadow_pnl + calibration_trend infra
- [x] Decision gate after 3 prints (~July 15)

Deviation from any of these requires a spec amendment before code lands.
