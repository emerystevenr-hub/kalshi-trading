# Terminal 1 — Phase 1 Backtest Report

**Run:** bd72ead0  
**Generated:** 2026-04-24T14:28:52.584400+00:00  
**Config:** min_edge=$0.05  stations=['NYC', 'ORD', 'LAX']  models=['gfs', 'hrrr', 'ecmwf_hres', 'aifs']  climo_window=30d

## Aggregate
- Total signals: **39**
- Total P&L: **$+2.56**
- Avg P&L per signal: $+0.0656
- Top station share of P&L: 89.7%
- Concentration: **edge carried by one station**

## By Station (ranked)

| Station | Signals | Hit Rate | Avg Edge | Total P&L |
|---|---:|---:|---:|---:|
| NYC | 22 | 13.6% | $+0.0784 | $+2.30 |
| LAX | 11 | 9.1% | $+0.0649 | $+0.44 |
| ORD | 6 | 0.0% | $+0.0838 | $-0.18 |

## P&L by Edge Threshold Bucket

| Station | 0.02-0.05 | 0.05-0.08 | 0.08-0.12 | 0.12+ |
|---|---:|---:|---:|---:|
| NYC | — | n=12, pnl=$+0.62, hit=8.3% | n=10, pnl=$+1.68, hit=20.0% | — |
| LAX | — | n=9, pnl=$+0.52, hit=11.1% | n=2, pnl=$-0.08, hit=0.0% | — |
| ORD | — | n=2, pnl=$-0.06, hit=0.0% | n=4, pnl=$-0.12, hit=0.0% | — |
