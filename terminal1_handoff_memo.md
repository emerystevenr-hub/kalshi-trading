# Terminal 1 — Handoff Memo (2026-04-23 afternoon)

## Permissions

The next Cowork session has full permission to access and modify files in `/Users/stevenemery/Documents/` and subdirectories, including `/Users/stevenemery/Documents/terminal1_data/` and `/Users/stevenemery/Documents/archive/`. All files referenced in this memo are Steve's and under his control.

## Strategic Context (do not re-debate)

- **Terminal 1 = Kalshi Weather Ensemble Bot**, replacing the retired Kalshi 3-way/complement arb strategy (both dead — see kalshi_3way_survey and kalshi_complement_survey results in archive).
- **Polymarket is currently US-restricted (CFTC ruling).** All Polymarket code is archived — see Polymarket Reserve section below. Do not revive for execution in the US. If Polymarket becomes legal via CFTC reversal, state-by-state authorization, or any route, the archived stack is production-ready and can be reactivated in hours.
- **Target:** $40–80k Year 1 on $10–25k bankroll. Explicit non-goal: 50x/year. Cash-flow engine, not venture-return.
- **Catalyst (Terminal 2)** — in runoff, 4 positions (~$17 net profit, $102 exposure). Managed manually, no bot.

## Current State (as of 2026-04-23 ~09:30 PDT)

**Running processes:**
- Terminal 1 Kalshi logger — nohup caffeinate wrapped, PIDs 4526+4528, snapping 60 markets every 15 min
- 3 model backfill processes (gfs/ecmwf_hres/aifs) — started ~09:00 PDT, 30-day × 6-horizons × 2-cycles/day
- HRRR backfill **killed 2026-04-23** — NOMADS retention is ~2 days, so 30-day history is unobtainable from the current source path. Process was looping on 403s. See "HRRR — Phase 1 Exclusion" below.

**Retired / archived:** `polymarket_*`, `sniper*`, `kalshi_shadow_v4*` → ~/Documents/archive/

**Preliminary Phase 1 result (thin data — 44 trades):**
- Total P&L: +$3.49 across 3 stations
- Hit rates 6.7%–16.7% (low-probability bets, positive EV)
- Concentration 45.8% (broad, not single-station)
- Threshold-bucket behavior inconsistent across stations → noise, not signal yet

## File Inventory (Terminal 1 stack)

Primary (current):
- `~/Documents/terminal1_weather_spec.md` — spec, don't modify without justification
- `~/Documents/terminal1_data_schema.md` — canonical schemas for every JSONL
- `~/Documents/terminal1_kalshi_logger.py` — production logger
- `~/Documents/terminal1_model_pullers.py` — 4-model forecast puller
- `~/Documents/terminal1_nws_actuals.py` — ground truth puller
- `~/Documents/terminal1_ensemble_backtest.py` — bias correction + ensemble + backtest + Phase 1 report
- `~/Documents/terminal1_backfill.sh` — 30-day backfill launcher (currently running)
- `~/Documents/terminal1_status.sh` — dashboard
- `~/Documents/terminal1_ecmwf_signup.md` — ECMWF access doc (access already verified)

Generated outputs:
- `~/Documents/terminal1_data/` — all JSONL data files
- `~/Documents/terminal1_data/forecasts_{model}_{station}.jsonl` — model forecasts
- `~/Documents/terminal1_data/nws_actuals_{station}.jsonl` — ground truth (91 days/station)
- `~/Documents/terminal1_data/kalshi_{station}_{date}.jsonl` — live market snapshots
- `~/Documents/terminal1_phase1_trades.jsonl` — preliminary trade log
- `~/Documents/terminal1_phase1_report.md` — preliminary report

Logs:
- `~/Documents/terminal1_logger.log` — logger live output
- `~/Documents/terminal1_backfill_{model}.log` — per-model backfill progress

## Launch / Monitor Commands (reference)

```bash
# Dashboard
~/Documents/terminal1_status.sh

# Tail logger
tail -f ~/Documents/terminal1_logger.log

# Tail one model's backfill
tail -f ~/Documents/terminal1_backfill_gfs.log

# Kill all backfills (if needed)
pkill -f 'terminal1_model_pullers.*--backfill'

# Re-run backtest (after backfill completes)
python3 ~/Documents/terminal1_ensemble_backtest.py

# Sensitivity (only after full backfill):
python3 ~/Documents/terminal1_ensemble_backtest.py --min-edge 0.07
python3 ~/Documents/terminal1_ensemble_backtest.py --max-lead 48
```

## Immediate Next Actions (in order)

1. **Wait** for backfill to complete (~2-3 hours from start, started ~09:00 PDT).
2. Dashboard check — target ~300+ records per (model × station).
3. Re-run `terminal1_ensemble_backtest.py` on full dataset.
4. **Phase 1 go/no-go** (Task #10 — pending):
   - GO criteria: monotonic threshold behavior across all 3 stations + positive net
   - MAYBE: inconsistent threshold + net positive → tighter filters, Phase 2
   - KILL: inconsistent + net negative
5. If GO: plan Phase 2 (paper live, 14-day live signal logging no execution).
6. If MAYBE: run sensitivity cuts on the FULL dataset.
7. If KILL: honest conversation about capital redeployment.

## HRRR — Phase 1 Exclusion (2026-04-23)

HRRR excluded from Phase 1 ensemble due to retention/availability limits on the current source path (NOMADS keeps ~2 days of HRRR). Phase 1 runs with **gfs + aifs + ecmwf_hres (3 models)**.

**Evidence (from `terminal1_backfill_hrrr.log`):**
- Cycles ≥ 2026-04-22 → 200 OK, records written
- Cycles 2026-04-21 → ~2026-04-10 → all f12–f72 returned **404** (rolled off NOMADS retention)
- Cycles ≤ 2026-04-09 → **403 Forbidden** with 3-attempt retry ladder (~7 min wasted per cycle)
- Cumulative HRRR records stuck at 48 (vs. 300+ target)

**Code changes:**
- `terminal1_ensemble_backtest.py`: `MODELS = ["gfs", "ecmwf_hres", "aifs"]`; `ALL_MODELS` kept for reference.
- `terminal1_weather_spec.md`: Phase 1 amendment noted in Section 3.1.

**Phase 2 restoration path (when ready):**
Repoint `terminal1_model_pullers.py` HRRR branch from NOMADS to AWS `s3://noaa-hrrr-bdp-pds/` (NOAA Open Data, public, no auth, full archive back to 2014). URL pattern: `https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.YYYYMMDD/conus/hrrr.tHHz.wrfsfcfFF.grib2`.

**Why this is not a blocker:** Phase 1 answers "does the weather thesis have enough signal to justify continued work?" That question does not require HRRR. A 3-model ensemble (GFS + AIFS + ECMWF HRES) covers global NWP, ML-based, and European deterministic — three genuinely different edge sources. If the thesis fails at 3 models, adding a 4th regional model won't save it.

## Open Questions / Issues

- **Auto-reboot killed everything overnight.** Turn off macOS auto-updates (System Settings → General → Software Update → Automatic Updates). Or set up launchd for auto-restart on boot.
- **Climatology prior is a ceiling, not reality.** The real Phase 1 answer needs Track B (live Kalshi prices). Logger has been running since 2026-04-22 ~20:24 UTC; need 21+ days for Track B to be meaningful.
- **Bias correction requires ≥ 3 matched (forecast, actual) pairs per cell** before it kicks in. Early backtest runs before backfill completes will show many cells without bias data.

## Current Task State

- #1-7, #11-13 → COMPLETED
- #8 (ensemble/bias) → COMPLETED
- #9 (backtest engine + report) → COMPLETED
- #10 (Phase 1 go/no-go decision) → PENDING — blocked on backfill completion

## Polymarket Reserve (preserved, not retired)

**Why this section exists:** the Polymarket stack is dormant, not dead. If the CFTC ruling reverses, or Polymarket gets state-by-state authorization, or a routing workaround becomes legally available, we want to reactivate quickly. The code is production-grade — don't rebuild it from scratch.

**What's archived in ~/Documents/archive/:**

| File | What it does | Status |
|---|---|---|
| `polymarket_engine3_retired_2026-04-22.py` | Engine 3 v3.6 — 3-way Dutch arb scanner + complement arb + fragment detection. WS book indexing bug fixed (was reading WORST prices, now reads max(bids)/min(asks) defensively). 117/117 tests passing. | Working, shadow-tested. |
| `polymarket_executor_retired_2026-04-22.py` | ArbExecutor — concurrent IOC submission, WS fill reconciliation with dedup, recursive unwind loop with bounded retry ladder + wall-clock + mark-to-market loss ceiling, persistent state with crash recovery. 21/21 tests passing. | Production-grade execution. |
| `polymarket_mock_server_retired_2026-04-22.py` | Local aiohttp mock of Polymarket CLOB — scenario injection (fill_full/partial/reject/ack_no_fill/timeout/crash), persistent state, crash middleware. 26/26 tests passing. | Full test harness. |
| `polymarket_executor_test.py`, `polymarket_mock_server_test.py` | Integration test suites covering 5 priority execution scenarios + halt/recovery + boot-time reconciliation. | All green. |
| `sniper_retired_2026-04-23.py` + `sniper.log*` | Earlier parallel attempt — simpler 3-way Dutch scanner. Superseded by Engine 3 v3.6. | Kept for reference. |

**What we actually proved with these:**
- 3-way Dutch arb signals exist on Polymarket soccer/NBA — Engine 3 recorded 23 dutch signals, $259 in simulated P&L, during shadow runs.
- Execution state machine handles the hard case: partial fills + venue crash + recovery without residual exposure.
- Full crash/halt/recovery invariants hold under injected failure.

**Reactivation checklist if Polymarket opens up:**
1. Verify legal/regulatory status first. Do not skip.
2. `mv ~/Documents/archive/polymarket_*.py ~/Documents/` to restore.
3. Validate Polymarket API contracts haven't changed (their CLOB has evolved — recheck WS subscription format, order schema, fee structure).
4. Run the mock server integration tests first (`polymarket_mock_server_test.py`, `polymarket_executor_test.py`) — all should still pass against mock.
5. Canary with $100 capital, observe for 24h, scale gradually.
6. Target: 3-way Dutch arb on soccer + NBA + NFL multi-leg markets. Complement arb as secondary.

**Strategic positioning:** Kalshi weather is Terminal 1. If Polymarket reopens, it becomes Terminal 3 (Polymarket arb executor) — complementary, different edge source, uncorrelated with weather outcomes. Diversification win.

## Operator Preferences (Steve's style)

- First principles, outcomes > activity, direct, no fluff
- No corporate tone, no soft language
- Strong positions backed by reasoning
- One thing at a time — don't flood with parallel commands
- Caffeinate everything long-running
