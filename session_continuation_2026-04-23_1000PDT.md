# Session Continuation — 2026-04-23 ~10:00 PDT

## When You Come Back, Run These First

```bash
~/Documents/terminal1_status.sh
```

**If `gfs`, `aifs`, and `ecmwf_hres` all show ≥300 records/station:**
```bash
python3 ~/Documents/terminal1_ensemble_backtest.py
```
Then make the Phase 1 go / maybe / kill call from `~/Documents/terminal1_phase1_report.md`.

**If still running:** check back in 30 min. ecmwf_hres is the slowest (~6h ETA from 09:00 PDT, so finishing ~15:00 PDT).

---

## What Changed This Session

1. **HRRR killed.** NOMADS retention is ~2 days, historical backfill impossible from current source. Excluded from Phase 1 ensemble. Phase 2 restoration path documented (AWS `s3://noaa-hrrr-bdp-pds/`). Full writeup in `terminal1_handoff_memo.md` under "HRRR — Phase 1 Exclusion".
2. **`terminal1_ensemble_backtest.py` now uses `MODELS = ["gfs", "ecmwf_hres", "aifs"]`.** `ALL_MODELS` preserved for reference.
3. **Weather backfill continuing** with 3 models. Logger still healthy.

## Terminal 3 (Macro Engine) — Liquidity Audit Done, Divergence Survey Pending

**Liquidity audit result: macro markets on Kalshi are MUCH more tradable than predicted.**

Real numbers from `~/Documents/terminal3_data/kalshi_macro_depth_2026-04-23_1700.jsonl`:

| Series | Markets | Avg Real TOB | Median Spread | Aggregate TOB $ |
|---|---|---|---|---|
| KXFEDDECISION | 75 | **19,121** | **6¢** | **$1.43M** |
| KXFED | 120 | 5,456 | 8¢ | $655k |
| KXCPI | 68 | 693 | 6¢ | $47k |
| KXPAYROLLS | 97 | 438 | 9¢ | $42k |
| KXCPIYOY | 56 | 528 | 7¢ | $30k |
| KXGDP | 12 | 977 | **2¢** | $12k |
| KXJOBLESSCLAIMS | 10 | 453 | 7¢ | $4.5k |
| **TOTAL** | **438** | | | **~$2.2M** |

**Implication: Terminal 3 splits into 3 sub-engines:**
- 3a. **Fed Rate Decisions** (KXFEDDECISION + KXFED) — the big one. Deep, tight, 15+ forward events.
- 3b. **Macro Nowcast** (KXCPI, KXCPIYOY, KXPAYROLLS) — moderate depth, CF nowcast divergence edge.
- 3c. **Surgical** (KXGDP, KXJOBLESSCLAIMS) — small but tight, precision plays.

**Divergence survey (Q1: "is there edge to trade?") — blocked on Cleveland Fed historical archive.**

Current state:
- Have: `QuarterlyAnnualizedPercentChange-2026-q2.csv` (17 daily values in Q2 2026)
- Need: per-quarter CSVs back to ~2023, plus Monthly MoM and Monthly YoY files for each period
- URL pattern likely: `.../QuarterlyAnnualizedPercentChange-YYYY-qN.csv`, `.../MonthlyPercentChange-YYYY-mmm.csv`, `.../AnnualPercentChange-YYYY-mmm.csv`

Next step: either batch-download historical CSVs by URL pattern, or start a nowcast logger and build history prospectively (3–6 months to enough data).

## Updated Portfolio Plan ($500k/yr path)

| # | Engine | Bankroll | Annual Target |
|---|---|---|---|
| 1 | Kalshi weather | $20k | $60–100k |
| 2 | Kalshi Fed decisions | $40k | $100–200k |
| 3 | Kalshi macro nowcast | $15k | $40–80k |
| 4 | Kalshi surgical GDP/claims | $10k | $20–40k |
| 5 | Polymarket 3-way arb (legal reopen) | $25k | $60–120k |
| 6 | Catalyst/event contracts | $15k | $40–80k |

$125k bankroll → $320–620k/yr range. Fed markets do the heavy lifting.

## Files Created This Session

- `terminal3_kalshi_macro_depth.py` — liquidity snapshot script (working)
- `terminal3_cleveland_fed_pull.py` — nowcast puller (needs URL update or local-file mode)
- `terminal3_diag_orderbook.py` — diagnostic (used once, kept for reference)
- `terminal3_survey_plan.md` — survey methodology
- `terminal3_data/kalshi_macro_depth_2026-04-23_1700.jsonl` — real liquidity data
- `terminal3_data/QuarterlyAnnualizedPercentChange-2026-q2.csv` — CF nowcast Q2 2026

## Open Tasks

- [ ] Weather backfill completion check + Phase 1 backtest (14:30 PDT trigger)
- [ ] Phase 1 go/maybe/kill call
- [ ] Cleveland Fed historical archive — monthly MoM/YoY + past quarters back to 2023
- [ ] If Phase 1 = GO: plan Phase 2 paper-trading
- [ ] Queue Terminal 5 survey (catalyst/event contracts)
- [ ] Queue Terminal 6 survey (niche international sports)

## What NOT to Do

- Don't restart weather backfill. It's working.
- Don't rebuild anything mid-run.
- Don't commit capital to Terminal 3 until divergence survey confirms edge.
- Don't stack new engine surveys until Phase 1 has a verdict.
