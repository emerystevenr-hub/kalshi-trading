# Terminal 1 — Phase 1 Backtest Report

**Run:** 56c66a42  
**Generated:** 2026-04-23T22:37:50.985832+00:00  
**Config:** min_edge=$0.05  stations=['NYC', 'ORD', 'LAX']  models=['gfs', 'hrrr', 'ecmwf_hres', 'aifs']  climo_window=30d

## Aggregate
- Total signals: **81**
- Total P&L: **$+6.57**
- Avg P&L per signal: $+0.0811
- Top station share of P&L: 39.6%
- Concentration: **edge is broad**

## By Station (ranked)

| Station | Signals | Hit Rate | Avg Edge | Total P&L |
|---|---:|---:|---:|---:|
| LAX | 21 | 19.0% | $+0.1835 | $+2.60 |
| ORD | 34 | 8.8% | $+0.1176 | $+2.01 |
| NYC | 26 | 11.5% | $+0.1643 | $+1.97 |

## P&L by Edge Threshold Bucket

| Station | 0.02-0.05 | 0.05-0.08 | 0.08-0.12 | 0.12+ |
|---|---:|---:|---:|---:|
| LAX | — | n=4, pnl=$+0.75, hit=25.0% | n=4, pnl=$+1.77, hit=50.0% | n=13, pnl=$+0.08, hit=7.7% |
| ORD | — | n=9, pnl=$+0.77, hit=11.1% | n=13, pnl=$-0.35, hit=0.0% | n=12, pnl=$+1.58, hit=16.7% |
| NYC | — | n=6, pnl=$-0.24, hit=0.0% | n=4, pnl=$+0.82, hit=25.0% | n=16, pnl=$+1.39, hit=12.5% |
