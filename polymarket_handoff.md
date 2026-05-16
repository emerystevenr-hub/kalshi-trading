# Polymarket Arb System — Handoff

**Date:** 2026-04-15
**Status:** Technical system fully operational. Strategy empirically dead.
**Decision required:** Kill / Pivot bid-side / Pivot market-making

---

## Core Finding

Naive N-way ask-side Dutching on Polymarket is structurally unworkable in the current market regime. Live `sum(best_ask)` exceeds 1.0 on every complete market observed — that's a built-in over-round (sportsbook vig). Buying the full YES basket at the offer is a guaranteed pre-fee loss.

Cached "edges" from the screener were Gamma API midpoint illusions. Midpoints don't reflect what a taker actually pays.

## Live Market State Snapshot (15:01:31 PDT)

| Market | Live sum(best_ask) | Legs populated | Verdict |
|---|---|---|---|
| ai_top_model_apr | 1.0140 | 13/13 | +1.4% vig |
| bulgaria_pm | 1.0430 | 10/10 | +4.3% vig |
| ecb_april_2026 | 1.0050 | 4/4 | +0.5% vig |
| epl_top_goalscorer | 1.0000 | 31/31 | break-even (no edge) |
| ipl_champion_2026 | 1.1220 | 10/10 | +12.2% vig (catastrophic) |
| la_liga_winner | 0.9940 | 3/20 | 17 legs missing → unfillable |
| nba_rookie_of_year | 1.0400 | 13/13 | +4.0% vig |
| south_korea_local_2026 | 1.0050 | 5/5 | +0.5% vig |
| uefa_europa_league | 1.0170 | 9/43 | 34 legs missing → unfillable |

**Zero triggers in 14+ hours** with `EXECUTION_THRESHOLD = 0.985`. Last trigger 00:17 PDT (ECB, pre-resolution). 47 total ECB triggers overnight — all aborted at EV cap because book depth evaporated past best.

Large-field sports markets (Europa League, La Liga) have structurally illiquid backmarkers with no asks posted at all. Full-basket Dutching impossible there regardless of vig.

## System Inventory (what's built and works)

Location: `~/Documents/`

- **config.py** — 9 markets, 149 tokens whitelisted. Thresholds, Kelly, cluster caps, per-trade cap.
- **sniper.py** — WebSocket sniper, tracks live order book, fires trigger below threshold. Heartbeat + per-market state snapshot instrumented.
- **execution.py** — 3-layer executor: book-walk sizing, quarter-Kelly, per-trade cap, cluster cap, adverse-selection guard, FOK fills, reversal protocol. Paper-trading mode on.
- **screener.py** — 5-filter Tier 1 screener (MECE / volume / resolution / completeness / legs).
- **hot_list.json** — 20 markets passed all filters on last run.
- **com.steve.polymarket-sniper.plist** — launchd daemon, KeepAlive, RunAtLoad, caffeinate keeping Mac awake.

The infrastructure is sound. The strategy is what's dead.

## Three Strategic Paths

### A. Kill
Save the 14 days. The empirical evidence is conclusive — no ask-side edge exists on Polymarket in this regime. MMs price offers to always sum above 1. Walking away is the honest answer to the data.

### B. Pivot to bid-side "reverse Dutch"
Check if `sum(1 − best_bid) > 1` — i.e., can you *sell* all YES tokens for more than $1. This is the mirror arb and less crowded. Requires either (a) already holding shares or (b) selling NO tokens on bids.

Sniper framework is ~80% reusable. 10-line patch to instrument bid-side snapshot. Fast answer available.

### C. Market-making
Post bids/asks at Dutch-consistent prices. Harvest spread instead of paying it. Completely different execution engine, larger capital commitment, direct competition with professional MMs who have colocation latency advantage.

### My call
**B first.** Ten-minute test to prove/disprove the mirror arb exists. If bid-side is also pinned below 1.0 on all markets → everything is vigged both ways → go to A (kill). If bid-side sums > 1.0 → real strategy, less crowded than ask-side, infrastructure already built.

## First Prompt for New Thread

> Polymarket ask-side Dutching confirmed dead (sum(best_ask) > 1.0 on all 9 markets observed over 14 hrs — see polymarket_handoff.md). Evaluate bid-side reverse Dutch as pivot. I have a working WebSocket sniper + execution engine at ~/Documents/ that can be repointed. First step: patch sniper.py to add sum(1 − best_bid) to the 5-min market snapshot so we get a one-shot readout of whether the mirror arb exists. If bid-side also pinned below 1.0, kill the project.

## Files to Reference

- `~/Documents/polymarket_handoff.md` (this file)
- `~/Documents/config.py`
- `~/Documents/sniper.py`
- `~/Documents/execution.py`
- `~/Documents/sniper.log` (live daemon output — has the market state snapshot)

## Housekeeping

Daemon is still running as of handoff. To stop it cleanly:
```
launchctl bootout gui/$(id -u)/com.steve.polymarket-sniper
```
To keep it collecting data while you evaluate the pivot, leave it alone. Log will keep growing.
