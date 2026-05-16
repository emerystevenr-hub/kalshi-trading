# Terminal 1 — Phase 1 Backtest Report

**Run:** fe07745d  
**Generated:** 2026-04-23T22:38:08.740604+00:00  
**Config:** min_edge=$0.10  stations=['NYC', 'ORD', 'LAX']  models=['gfs', 'hrrr', 'ecmwf_hres', 'aifs']  climo_window=30d

## Aggregate
- Total signals: **48**
- Total P&L: **$+3.79**
- Avg P&L per signal: $+0.0790
- Top station share of P&L: 38.5%
- Concentration: **edge is broad**

## By Station (ranked)

| Station | Signals | Hit Rate | Avg Edge | Total P&L |
|---|---:|---:|---:|---:|
| ORD | 17 | 11.8% | $+0.1594 | $+1.46 |
| NYC | 17 | 11.8% | $+0.2149 | $+1.34 |
| LAX | 14 | 14.3% | $+0.2377 | $+0.99 |

## P&L by Edge Threshold Bucket

| Station | 0.02-0.05 | 0.05-0.08 | 0.08-0.12 | 0.12+ |
|---|---:|---:|---:|---:|
| ORD | — | — | n=5, pnl=$-0.12, hit=0.0% | n=12, pnl=$+1.58, hit=16.7% |
| NYC | — | — | n=1, pnl=$-0.05, hit=0.0% | n=16, pnl=$+1.39, hit=12.5% |
| LAX | — | — | n=1, pnl=$+0.91, hit=100.0% | n=13, pnl=$+0.08, hit=7.7% |
