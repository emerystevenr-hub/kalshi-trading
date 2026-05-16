# Terminal 1 — Phase 1 Backtest Report

**Run:** f89e1672  
**Generated:** 2026-04-24T14:28:52.632929+00:00  
**Config:** min_edge=$0.05  stations=['NYC', 'ORD', 'LAX']  models=['gfs', 'hrrr', 'ecmwf_hres', 'aifs']  climo_window=30d

## Aggregate
- Total signals: **79**
- Total P&L: **$+6.05**
- Avg P&L per signal: $+0.0766
- Top station share of P&L: 64.4%
- Concentration: **edge carried by one station**

## By Station (ranked)

| Station | Signals | Hit Rate | Avg Edge | Total P&L |
|---|---:|---:|---:|---:|
| NYC | 36 | 13.9% | $+0.0825 | $+3.90 |
| LAX | 21 | 14.3% | $+0.0626 | $+1.84 |
| ORD | 22 | 4.5% | $+0.0676 | $+0.32 |

## P&L by Edge Threshold Bucket

| Station | 0.02-0.05 | 0.05-0.08 | 0.08-0.12 | 0.12+ |
|---|---:|---:|---:|---:|
| NYC | — | n=15, pnl=$+1.54, hit=13.3% | n=21, pnl=$+2.36, hit=14.3% | — |
| LAX | — | n=19, pnl=$+1.92, hit=15.8% | n=2, pnl=$-0.08, hit=0.0% | — |
| ORD | — | n=19, pnl=$+0.41, hit=5.3% | n=3, pnl=$-0.09, hit=0.0% | — |
