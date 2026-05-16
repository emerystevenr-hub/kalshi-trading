# Terminal 3c — Initial Jobless Claims Engine

_Version 0.1 — 2026-05-02_
_First paper trade gate: KXJOBLESSCLAIMS-26MAY07 (settles 2026-05-07 ~13:55 UTC)._

## Strategic objective

Trade Kalshi `KXJOBLESSCLAIMS-{week}-{strike}` weekly markets using a
locally-computed nowcast from FRED ICSA history. Edge source: retail flow
on Kalshi anchors to recent weekly print noise; trailing-trend nowcast
plus seasonality is more accurate at near-week horizons.

Phase 1: Initial Claims only (weekly = 52 prints/year, fastest cadence
of any economic data on Kalshi). Phase 2 adds GDP via Atlanta Fed GDPNow
(quarterly = 4 prints/year, slower).

## Market universe

**Series:** `KXJOBLESSCLAIMS`
**First event:** `KXJOBLESSCLAIMS-26MAY07` (week ending May 2, 2026)
**Strike grammar:** `KXJOBLESSCLAIMS-{YYMMMDD}-{strike}` where strike is
the claims threshold (e.g., `-200000` = "≥200,000 claims").

Live snapshot 2026-05-02 (sample, yes_bid):

| Strike | yes_bid | Implied P(≥X) | Open interest |
|---|---|---|---|
| 180k | 0.82 | 82% | 13 (illiquid) |
| 190k | 0.67 | 67% | 14 |
| 195k | 0.54 | 54% | 297 |
| **200k** | 0.60 | 60% | 1138 (peak) |
| 205k | 0.23 | 23% | 1421 |
| 210k | 0.19 | 19% | 952 |

**Implied market mean ≈ ~199-202k** (50% probability strike). Note:
non-monotonic at 195k→200k may be a stat-arb opportunity — flag for
the structural arb scanner (Phase 2).

## Data sources

### Forecast (μ): trailing 4-week ICSA mean + trend adjustment
Pull weekly ICSA data from FRED:
```
https://fred.stlouisfed.org/graph/fredgraph.csv?id=ICSA&cosd=2024-01-01
```
No API key needed for the CSV endpoint.

Nowcast formula (Phase 1):
```
μ = mean(last 4 weeks)
σ = pstdev(last 26 weeks of week-over-week changes)
```

### Actuals (truth): same FRED ICSA series
Released Thursdays at 8:30 AM ET (12:30 UTC). DOL is the ground truth;
FRED mirrors it within ~1 hour.

### Kalshi snapshots
Public `/markets?event_ticker=KXJOBLESSCLAIMS-{event}&limit=200`. Same
pattern as T3b's logger.

## Forecast model

Phase 1 — naive but defensible:
```
μ = trailing 4-week mean
σ = pstdev of week-over-week deltas (last 26 weeks)
P(claims ≥ X) = 1 - Φ((X - μ) / σ)
```

Empirical σ from FRED historical: weekly claims week-over-week σ runs
~5k-15k in stable regimes, 20k+ during shocks (covid, hurricanes).
Initial fit will use 26-week trailing σ; refit weekly as new prints land.

## Edge calculation + entry rules

```
our_p = P(claims ≥ strike) per the model
market_p = (yes_bid + yes_ask) / 2

if our_p > market_p: side = YES, entry = yes_ask, edge = our_p - market_p
if our_p < market_p: side = NO,  entry = 1.0 - yes_bid, edge = market_p - our_p
```

Filters:
- `min_edge = $0.10` (CPI-style, claims markets have similar tightness)
- `volume_24h ≥ 50 OR open_interest ≥ 200`
- `yes_bid ∈ [0.10, 0.90]` (avoid pathological tails)
- `our_p ∈ [0.05, 0.95]` (clip from σ-too-tight artifacts)
- `hours_to_close ∈ [4, 144]` (4h-6d trade window)
- `not already_open(ticker)`
- Cluster cap: max 5 strikes per event

Side selection asymmetry (T1 lesson):
- YES side requires edge ≥ table+$0.05 AND yes_bid in [0.15, 0.85]

Position sizing:
- Default 25 contracts per fire
- Cost basis cap ≤ $150 per weekly event

## Calibration framework

Reuse T1/T3b infrastructure: shadow_pnl ledger with `engine="T3c"` tag,
side-aware `_model_win_prob` from t1_settlement_analysis, mirror the
SHIP/ITERATE/KILL gate.

Decision gate (locked):
- **SHIP**: 6 settled prints, side-aware YES cal ∈ [0.7, 1.3], P&L > 0
- **ITERATE**: 6 prints, cal outside band but ≥ 0.3 → refit σ
- **KILL**: cal < 0.3 OR P&L < -10% on cost

At weekly cadence, 6 prints = 6 weeks. **First decision gate ~mid-June.**

## File layout

```
~/Documents/
  terminal3c_claims_spec.md            (this doc)
  terminal3c_kalshi_logger.py          (KXJOBLESSCLAIMS market snaps)
  terminal3c_claims_data.py            (FRED ICSA puller)
  terminal3c_paper_trader.py           (signal generation + opens)
  terminal3c_settlement_reconciler.py  (Kalshi-finalized close)
  terminal3c_data/
    kalshi_KXJOBLESSCLAIMS-{event}_{date}.jsonl
    icsa_history.jsonl                  (FRED weekly data)
    paper_trader.log
    settlement_reconciler.log
```

## Phase plan

| Window | Milestone |
|---|---|
| **2026-05-02** | Spec sign-off (this doc); build all 4 scripts |
| 2026-05-03 to 05-06 | Logger snapping; paper trader generating signals; opens accumulate |
| **2026-05-07 12:30 UTC** | DOL releases week-ending-May-2 ICSA data |
| 2026-05-07 14:00 UTC | Kalshi finalizes; reconciler closes positions |
| 2026-05-08 morning | First T3c settlement_analysis run (n=2-5 positions, 1 print) |
| Weekly through mid-June | 5 more prints accumulate |
| **2026-06-13** | First decision gate (6 prints settled) |

## Decision criteria — locked 2026-05-02

- [ ] Phase 1 attacks claims only (GDP comes Phase 2)
- [ ] Forecast model: 4-week trailing mean + 26-week WoW σ
- [ ] min_edge $0.10, cluster cap 5 strikes/event, contracts 25
- [ ] Cost basis ≤ $150/event, ≤ $900 across the 6-week validation
- [ ] Engine tag `T3c`, reuse shadow_pnl + side-aware cal infra
- [ ] Decision gate after 6 prints (~June 13)

Deviation requires a spec amendment before code lands.
