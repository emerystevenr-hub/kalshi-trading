# Terminal 2 (Catalyst) — Position Audit, 2026-04-26

## Executive read

- 8 open positions, **$161.30 cost basis** (= worst-case loss; all bets are "buy NO @ price <1.00")
- 0 lifetime closes in shadow ledger → either Apr 23 memo's "4 positions in runoff, $102 exposure" was a different/un-tracked book, or those positions were closed off-ledger before this ledger started tracking T2
- All 8 opened on **2026-04-24** (calendar_fade batch at 00:09 UTC, tail_probability batch at 14:21 UTC)
- **Two positions appear past their settlement window** and need verification + manual close

## Critical: stale positions (action required)

| Ticker | Settle date in ticker | Open price | Cost | Notes |
|---|---|---|---|---|
| `KXGOVTSHUTLENGTH-26FEB07-G100` | 2026-02-07 | $0.64 NO | $48.00 | Largest single position. Feb 7 is past. Confirm Kalshi resolution and close. |
| `KXHORMUZNORM-26MAR17-B260601` | 2026-03-17 (`-B260601` may extend to Jun 1?) | $0.69 NO | $6.90 | Two dates in ticker — verify whether Mar 17 was the trigger or the close. |

If both already resolved at $0 (NO won, since YES did not occur), realized P&L = **+$30.10** ($30.40 max payout - $161.30 stays the same). Run the manual close in shadow_pnl_core directly. If market voided, P&L = $0 on cost.

## Calendar-fade book (3 positions, $14.80 cost, $15.20 upside)

Thesis: catalysts rarely play out on schedule. Fade YES.

| Ticker | Catalyst | Settle | Side @ Price | Cost | Max upside | Implied/Prior/Edge |
|---|---|---|---|---|---|---|
| `KXDHSFUND-26JUN01` | DHS funding bill passes by Jun 1 | 2026-06-01 | NO @ 0.37 | $3.70 | $6.30 | 67/30/$0.37 |
| `KXTRUMPCHINA-26-MAY15` | US-China trade deal by May 15 | 2026-05-15 | NO @ 0.42 | $4.20 | $5.80 | 62/30/$0.32 |
| `KXHORMUZNORM-26MAR17-B260601` | Hormuz normalization | see above | NO @ 0.69 | $6.90 | $3.10 | partial 62→34 |

## Tail-probability book (5 positions, $146.50 cost, $103.50 upside)

Thesis: extreme-tail outcomes systematically over-priced.

| Ticker | Catalyst | Settle | Side @ Price | Size | Cost | Max upside | Implied/Prior/Edge |
|---|---|---|---|---|---|---|---|
| `KXGOVTSHUTLENGTH-26FEB07-G100` | Govt shutdown ≥100 days in 2026 | see above | NO @ 0.64 | 75 | $48.00 | $27.00 | 41/14/$0.27. Hist max US shutdown=35 days. Highest conviction. |
| `KXMJSCHEDULE-27` | Marijuana rescheduling by 2027 | 2027 | NO @ 0.54 | 50 | $27.00 | $23.00 | 50/17/$0.32 |
| `KXFDAAPPROVALPSYCHEDELIC-27-ANYPSYCH` | FDA approves any psychedelic by 2027 | 2027 | NO @ 0.60 | 50 | $30.00 | $20.00 | 45/16/$0.29 |
| `KXTRUMPADMINLEAVE-26DEC31-KLEA` | Leavitt leaves WH Press Sec by 2027 | 2026-12-31 | NO @ 0.54 | 50 | $27.00 | $23.00 | 50/17/$0.32 |
| `KXTRUMPADMINLEAVE-26DEC31-PHEG` | Hegseth leaves SecDef by 2027 | 2026-12-31 | NO @ 0.58 | 25 | $14.50 | $10.50 | 46/16/$0.30. Half size — correlation hedge w/ Leavitt. |

## Position concentration / risk

- $48 in a single Feb 7 shutdown bet (30% of T2 book) — **largest single concentration**
- $41.50 in Trump-admin-departure cluster (Leavitt + Hegseth) — already size-adjusted for correlation
- $57 in 2027-settlement positions — **8+ months of capital tied up**

## Recommendations

1. **Today**: verify settlement state of the two stale tickers and book the closes manually. Update shadow ledger so settlement_analysis isn't carrying ghost positions.
2. **Define a T2 settlement protocol**: even without a bot, set a recurring weekly check on T2 expiries. Easiest path is a one-page checklist or a 5-line cron-driven script that pings `Kalshi /markets?ticker=...&status=settled` for each open T2 ticker.
3. **Cap T2 book at $200 worst-case** until the thesis_factory has tracked outcomes. Right now we have zero closed T2 positions — we don't know if the calendar_fade or tail_probability theses actually print. $161 is already 80% of that cap.
4. **Don't run T1 calibration math on T2.** Different N, different distributions, heterogeneous one-offs. Score T2 separately when ≥10 closes accumulate.

## Source

- Ledger: `~/Documents/shadow_pnl/ledger.jsonl` (filter `engine=="T2"`)
- Original handoff: `~/Documents/terminal1_handoff_memo.md` (Apr 23 afternoon)
- All open prices and reasons reconstructed from `signal_metadata` + `reason` fields on the open events.
