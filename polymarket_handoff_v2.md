# POLYMARKET PROJECT — HANDOFF V2
_Written 2026-04-15 after pivoting from arbitrage hunt to market-making_

## BOTTOM LINE UP FRONT

We killed the arbitrage hypothesis after proving it dead on Polymarket at retail scale (Rule 4 monotonicity: $15 executable at 1%, Rule 2 correlated outcomes: collapses to dead-after-fees House elections + phantom quotes). The real game is **market making with spread capture on slow political markets**. Paper MM is built and ready to run. Next phase: 24-48 hour paper run, measure adverse selection, decide whether to deploy real capital.

**Capital available:** $10k to start, scale to $50k if paper + live confirm edge.
**Deployment target:** VPS (Hetzner/DO, NYC or Ashburn, ~$20/mo) once strategy proves out.
**Next build:** Nothing until we have paper results. Let it run.

---

## WHAT WORKS / WHAT DOESN'T — DON'T REDO THIS

### DEAD (don't revisit without new evidence)

1. **N-way basket Dutching (ask-side).** `sum(best_ask) > 1.0` on every Polymarket event. Symmetric MM vig. Confirmed dead over 14+ hours of live monitoring on 9 soccer markets.
2. **Bid-side reverse Dutching.** Same vig, opposite side. Confirmed dead.
3. **Rule 4 — ladder monotonicity.** Scanned 4,000 events, 157 ladders, 1,184 tokens. Found exactly one two-sided violation: Fed rate 0.25% vs 0.5%, 1.1% edge, $15 executable. Not a business.
4. **Rule 2 — correlated outcomes across `negRisk` events.** After filtering Polymarket's own `negRisk==true` flag, 40 "hits" remain. Dominated by House races at 1-2% edge × $1-3k size. Polymarket taker fee is ~2% → net negative after fees. Rotten Tomatoes "arbs" are phantom MM rebate-farming quotes, not executable.
5. **MLB cross-venue moneyline edge.** Model too weak (pure Elo ignores pitchers/stars) and pattern is gambling at $10k sample size (variance dominates).
6. **MLB World Series futures edge.** Same variance problem, resolves once/year.

### LIVE (the current build)

**Rule 1 + Rule 3 hybrid — maker-only market making on slow political markets.** This is what the $7.4M bot was actually doing. Never cross the spread, never pay taker fees, collect bid-ask spread on every fill plus maker rebates (if we qualify for a tier).

Target universe: political/primary/appointment markets with:
- `negRisk == true` (well-defined resolution)
- Spread ≥ 4c (room to post inside and still earn)
- Min size on both sides ≥ $50 (real books)
- Mid between 5c-95c (not tail)
- 14-365 day resolution horizon

Current universe: ~8-10 markets of that shape. Top candidates from `mm_event_curator.py` run on 2026-04-15:
- FA Cup Winner (4c × $218 × 31d)
- Georgia GOP Senate Primary (5c × $197 × 33d)
- Maine Gov GOP Primary (11c × $64 × 54d)
- Kentucky Dem Senate Primary (4c × $137 × 33d)
- CO-03 GOP Primary (5.5c × $57 × 75d)
- Virginia GOP Senate Primary (4c × $51 × 61d)
- VA-06 House (4c × $52 × 201d)
- Maine Senate (4c × $39 × 201d)

---

## CURRENT FILES IN `/Users/stevenemery/Documents/`

### Active
- `mm_event_curator.py` — curates the MM universe. Run when universe goes stale (weekly).
- `mm_universe.json` — curator output. Top 40 events ranked by spread × size.
- `paper_mm.py` — **paper market maker. Primary tool right now.** Reads universe, posts virtual quotes at bid+1c / ask-1c, simulates fills, logs realized P&L + adverse-selection drift.
- `paper_mm_trades.csv` — one row per simulated fill.
- `paper_mm_positions.csv` — hourly inventory + P&L snapshot.

### Reference
- `inequality_scanner.py` — Rule 4 ladder scanner. **Dead strategy, keep for forensics only.**
- `correlated_scanner.py` — Rule 2 correlated-outcome scanner. **Dead strategy, keep for forensics only.**
- `inequality_scanner_log.csv`, `correlated_scanner_log.csv` — historical scan logs.
- `sniper.py` / `execution.py` / `config.py` — original Dutching infrastructure from the initial project. **Not used by current MM strategy.**

### Deprecated / Do Not Use
- `mlb_scanner.py` — has `sys.exit` at top. Deprecated.
- `mlb_edge_tracker.py`, `mlb_futures_edge.py`, `mlb_futures_crossvenue.py` — abandoned MLB experiments.
- `mlb_debug_dump.py`, `mlb_model.py` — MLB Elo infrastructure. Keep, may be reused if we pivot to the data-product path (see Option 3 below).

---

## IMMEDIATE NEXT STEP

### Run paper MM for 24-48 hours

```bash
cd ~/Documents && python3 paper_mm.py
```

Let it run overnight + at least one US trading day. Ctrl+C gives final summary.

### Go/no-go thresholds from paper results

| Metric | KILL | Deploy $10k | Deploy $50k |
|---|---|---|---|
| Fills / hour across universe | <0.5 | 1-3 | >3 |
| Realized P&L per round trip | ≤$0 | +$1-$2 | >$2 |
| Adverse-selection rate | >65% | 50-65% | <50% |
| Avg adverse drift per fill | worse than -1.5c | -0.5 to -1.5c | better than -0.5c |
| Max consecutive losing day | >-5% capital | -2 to -5% | <-2% |

If paper lands in the "Deploy $50k" column, move directly to VPS provisioning + live trading.
If paper lands in "Deploy $10k", prove it live with $10k for 2 weeks before scaling.
If paper hits "KILL", see "Pivot options" at bottom.

---

## WHAT TO BUILD NEXT (ONLY AFTER PAPER PROVES POSITIVE)

Priority order:

### 1. Fair-value anchor (`fair_value.py`)
Pulls consensus mid from Kalshi (primary competitor to Polymarket on political markets) and The Odds API (for the few sports markets in our universe). Writes `fair_value.json` with per-market FV + confidence. Paper MM v2 will cross-reference: if FV diverges >3% from our quote center, auto-cancel that side. This is the adverse-selection guard.

### 2. Live order execution (`live_mm.py`)
Polymarket CLOB Python client. Requires funded Polygon wallet with USDC + API credentials. Replaces the virtual-quote loop with real limit-order placement + cancellation. Hard-coded risk controls:
- Per-market inventory cap: $5k at $50k total capital
- Daily loss circuit breaker: auto-cancel all + cooldown at -3% capital
- Per-fill order size cap: $500 regardless of universe quote size
- FV-divergence auto-pause (depends on #1)

### 3. VPS deployment
- Hetzner CX22 or DigitalOcean $12 droplet in NYC/Ashburn
- Ubuntu 24.04, Python 3.12 venv
- systemd unit with auto-restart on crash
- Secrets in `/etc/mm/secrets` (400 perms)
- Telegram bot for P&L alerts + crash notifications
- CSV archives to Backblaze B2 nightly

### 4. Weekly universe refresh (`refresh_universe.sh`)
Cron job: run `mm_event_curator.py` weekly, diff against current universe, alert on events added/removed, require manual approval before new events go live.

---

## KEY DECISIONS STEVE HAS MADE

- **Project is live.** We are not killing Polymarket work despite proving multiple dead ends.
- **Capital ramp: $10k → $50k** contingent on proving edge + low risk.
- **VPS deployment** is the target once strategy proves out. Not touching VPS until paper is positive.
- **Risk tolerance:** Not explicitly set. Default assumption until he says otherwise: 10% max drawdown = full stop-and-review trigger.

## OPEN QUESTIONS FOR STEVE

1. **Polymarket account status** — does he have a funded Polygon wallet with USDC? If not, that's a 1-day setup that should happen in parallel while paper runs.
2. **Telegram/Pushover channel for alerts** — should be set up before live trading but not strictly needed for paper.
3. **News-driven auto-kill** — does he want a news-feed kill switch (FOMC, breaking political news) or will he manage that manually?

---

## COMMUNICATION STYLE — IMPORTANT

Steve operates with:
- First-principles reasoning
- Outcomes > activity
- Direct, high-signal communication
- No fluff, no hedging, no soft language
- Strong positions backed by clear reasoning

Respect these. Do not:
- Oversell dead strategies
- Use corporate tone or generic summaries
- Recommend "killing the project" as the first option — pivot and build. He said explicitly "I want to make it work." If a strategy dies, propose the next real path, don't suggest quitting.
- Over-explain obvious concepts

When strategies fail (three did before we landed on MM), be blunt about why, then propose the next test.

---

## PIVOT OPTIONS IF MM ALSO DIES

Only relevant if paper MM hits KILL thresholds. In priority order:

1. **Ramp into being a dedicated Polymarket MM.** Quote tighter than incumbents across 50+ markets, accept that this is running a liquidity desk, not doing arbitrage. Months to break-even. Real business if pursued seriously.

2. **Data product spinoff.** The scanner + Elo + event-universe code is actually decent sports/political modeling infra. Retool as a subscription research product: scan cross-venue futures (Pinnacle + DraftKings + Polymarket), surface sharp-consensus vs. Polymarket gaps, sell to bettors who HAVE the capital + infra to execute. Zero capital at risk, SimpleBooks-adjacent recurring revenue.

3. **Real kill.** Only if both MM AND data-product economics fail. Unlikely — at minimum option 2 is viable.

---

## HOW TO RESUME THIS PROJECT IN A NEW THREAD

1. Read this doc.
2. Read `~/Documents/paper_mm_trades.csv` (if run has happened) + `paper_mm_positions.csv`.
3. Check current universe: `cat ~/Documents/mm_universe.json | jq '.events[0:5]'`
4. Ask Steve where paper MM stands — how many hours has it run, what's the final realized P&L + adverse rate.
5. Reference the go/no-go table above. Make the deployment call.
6. Build next step from the "WHAT TO BUILD NEXT" section above.

Do not re-scan for arbitrage. Do not re-test Rule 4 or Rule 2 without fundamentally new data. Those are dead.
