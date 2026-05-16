# Terminal 1 — Phase 1 Backtest Report

**Run:** ce299064  
**Generated:** 2026-04-24T05:04:34.701690+00:00  
**Config:** min_edge=$0.05  stations=['NYC', 'ORD', 'LAX']  models=['gfs', 'hrrr', 'ecmwf_hres', 'aifs']  climo_window=30d

## Aggregate
- Total signals: **46**
- Total P&L: **$+3.06**
- Avg P&L per signal: $+0.0665
- Top station share of P&L: 42.2%
- Concentration: **edge is broad**

## By Station (ranked)

| Station | Signals | Hit Rate | Avg Edge | Total P&L |
|---|---:|---:|---:|---:|
| NYC | 17 | 11.8% | $+0.1427 | $+1.29 |
| LAX | 14 | 14.3% | $+0.1392 | $+1.23 |
| ORD | 15 | 6.7% | $+0.0912 | $+0.54 |

## P&L by Edge Threshold Bucket

| Station | 0.02-0.05 | 0.05-0.08 | 0.08-0.12 | 0.12+ |
|---|---:|---:|---:|---:|
| NYC | — | n=2, pnl=$-0.08, hit=0.0% | n=6, pnl=$+0.76, hit=16.7% | n=9, pnl=$+0.61, hit=11.1% |
| LAX | — | n=2, pnl=$-0.12, hit=0.0% | n=5, pnl=$+0.72, hit=20.0% | n=7, pnl=$+0.63, hit=14.3% |
| ORD | — | n=11, pnl=$+0.69, hit=9.1% | n=1, pnl=$-0.04, hit=0.0% | n=3, pnl=$-0.11, hit=0.0% |
