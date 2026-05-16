# Terminal 1 — Phase 1 Backtest Report

**Run:** 35f2e775  
**Generated:** 2026-04-24T00:15:31.050484+00:00  
**Config:** min_edge=$0.05  stations=['NYC', 'ORD', 'LAX']  models=['gfs', 'hrrr', 'ecmwf_hres', 'aifs']  climo_window=30d

## Aggregate
- Total signals: **51**
- Total P&L: **$+3.73**
- Avg P&L per signal: $+0.0731
- Top station share of P&L: 53.4%
- Concentration: **edge is broad**

## By Station (ranked)

| Station | Signals | Hit Rate | Avg Edge | Total P&L |
|---|---:|---:|---:|---:|
| LAX | 16 | 18.8% | $+0.1270 | $+1.99 |
| NYC | 17 | 11.8% | $+0.1410 | $+1.29 |
| ORD | 18 | 5.6% | $+0.0882 | $+0.45 |

## P&L by Edge Threshold Bucket

| Station | 0.02-0.05 | 0.05-0.08 | 0.08-0.12 | 0.12+ |
|---|---:|---:|---:|---:|
| LAX | — | n=4, pnl=$+0.65, hit=25.0% | n=6, pnl=$+0.70, hit=16.7% | n=6, pnl=$+0.64, hit=16.7% |
| NYC | — | n=2, pnl=$-0.08, hit=0.0% | n=6, pnl=$-0.24, hit=0.0% | n=9, pnl=$+1.61, hit=22.2% |
| ORD | — | n=12, pnl=$+0.64, hit=8.3% | n=3, pnl=$-0.08, hit=0.0% | n=3, pnl=$-0.11, hit=0.0% |
