# Terminal 3 — Macro Nowcast Survey Plan

**Purpose:** before committing to build a Terminal 3 (macro nowcast engine), run a survey that answers two gating questions with real data.

## Gating Questions

**Q1. Is there persistent pricing divergence between Cleveland Fed CPI Nowcast and Kalshi CPI market-implied forecasts at T-3/T-1/T-0 days to release?**
- If Kalshi already discounts to the Fed nowcast → **no edge, Terminal 3 is dead.**
- If divergence exists and is directionally correct (Fed nowcast predicts realized CPI better than Kalshi implied) → **Terminal 3 is real.**

**Q2. What is the actual top-of-book + depth on Kalshi macro markets (CPI, PCE, jobs, GDP, Fed decisions)?**
- Depth sets the capital ceiling. If TOB size is <$500 per market, Terminal 3 caps at low-five-figures.
- If TOB size is $2k+ with meaningful 5¢-deep depth, Terminal 3 scales meaningfully.

## Scripts Delivered

| File | Purpose | Run time | Output |
|---|---|---|---|
| `terminal3_kalshi_macro_depth.py` | Snapshot current orderbook depth across all macro event families | ~2 min | `terminal3_data/kalshi_macro_depth_<stamp>.jsonl` + stdout summary |
| `terminal3_cleveland_fed_pull.py` | Download Cleveland Fed nowcast historical XLSX | ~30 sec | `terminal3_data/cleveland_fed_nowcast.jsonl` + raw XLSX |

## Data Gaps (be honest about these)

1. **Bloomberg consensus forecasts are paywalled.** Without consensus numbers, we can't directly compare "is Kalshi anchored on consensus or on Cleveland Fed?" Alternatives:
   - Trading Economics has free rate-limited consensus
   - Philadelphia Fed SPF is free but quarterly, too coarse
   - For the survey, we can proxy: if Kalshi-implied ≈ last BLS release trend, it's anchored on consensus-ish; if Kalshi-implied ≈ Cleveland Fed nowcast, it's already arbed.
2. **Kalshi doesn't expose historical orderbook depth.** The depth audit is point-in-time. If you want longitudinal depth over release cycles, build a macro logger (forthcoming) and run it for 2 CPI cycles (~8 weeks) before drawing conclusions.
3. **Kalshi CPI history on /trades and /candlesticks** IS available for settled markets — we just haven't pulled it yet. Next script to build: `terminal3_kalshi_cpi_history.py`.

## Recommended Execution Order

1. **Today (5 min of your time):** Run `terminal3_kalshi_macro_depth.py`. Paste the summary. Gives us Q2's immediate answer.
2. **Today (1 min of your time):** Run `terminal3_cleveland_fed_pull.py`. Confirms we have the historical nowcast data for later analysis.
3. **After Phase 1 GO decision (later today or tomorrow):** I build `terminal3_kalshi_cpi_history.py` + `terminal3_nowcast_divergence.py` to join the three data sources and answer Q1 with numbers.
4. **Phase 2 build decision:** only after Q1 and Q2 both answer YES. Until then Terminal 3 is hypothesis, not build.

## What a "GO" looks like

- **Depth:** median TOB YES size ≥ 1000 contracts ($1k notional) AND depth within 5¢ ≥ 2000 contracts AND ≥ 5 active markets per release → deployable.
- **Divergence:** median absolute gap between Cleveland Fed implied probability and Kalshi implied probability at T-1 ≥ 3¢ AND gap sign predicts realized outcome ≥ 55% of the time → tradable edge.
- Both conditions must hold. Either alone = fantasy.

## What a "KILL" looks like

- **Depth:** median TOB <$300 OR fewer than 3 active macro event families with markets → capital ceiling too low regardless of edge.
- **Divergence:** median absolute gap <1¢ OR directional hit rate <52% → Kalshi already priced it in.

## Pre-emptive reminders

- Do not build the Terminal 3 engine until survey completes.
- Do not reallocate capital from Phase 1 to Terminal 3 until Phase 1 has a verdict.
- The $500k scaling fantasy from the previous pitch is market-depth-limited on Kalshi. This survey will produce the real ceiling number.
