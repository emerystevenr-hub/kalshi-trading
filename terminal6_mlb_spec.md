# Terminal 6 — Kalshi MLB Game Markets

**Spec version:** 0.1, drafted 2026-05-08
**Status:** spec for review — no code shipped yet
**Decision needed before build:** confirm the data-source choice for sharp consensus lines (see §3) and the trade-trigger threshold (see §5)

---

## 0. Why this engine exists

After T1 (negative edge) and the T7 macro-pivot finding (no retail flow on Kalshi macro markets at daily cadence), MLB game markets are the only Kalshi market class that combines high cadence + real retail flow + clean binary outcomes + a quant edge thesis we can model.

Validation throughput: ~15 games/day × 2 markets × 6-month season = ~5,400 settled markets/yr. Versus T3b CPI's 12 events/yr. **450x more data per calendar week.**

---

## 1. Edge thesis

**Hypothesis:** Sharp US sportsbook moneylines (Pinnacle, Circa Sports, Bookmaker.eu) update on lineup news, weather, and steam moves 30-90 minutes before retail-aimed books (DraftKings, FanDuel) and Kalshi. Kalshi prices anchor to retail-book consensus, not sharp books. Trade the gap.

**Why this works on MLB specifically:**
- MLB has the deepest public modeling stack of any US sport (Pythagorean expectancy, FIP, wOBA, Statcast, ZiPS, Steamer)
- Lineups and starting pitchers are confirmed 90-180 min before first pitch — discrete information events that move sharp lines fast
- Retail Kalshi flow lags lineup announcements
- Settlement is mechanical (final score posted within 4h of first pitch)
- 162 regular-season games per team plus playoffs = enormous sample size in one season

**Why this might NOT work (kill criteria):**
- If Kalshi MMs are already arbing against sharp books, the gap closes before retail can hit it
- If sharp-book API access is rate-limited or expensive enough to eat the edge
- If Kalshi MLB markets have hidden bid-ask spread costs (e.g., 1-2¢ MM rebate spam) that exceed the sharp-vs-Kalshi gap

---

## 2. Market structure (verified 2026-05-08 via probe)

- **Series:** `KXMLBGAME` (1,897 sports series total in Sports category; this is the per-game one)
- **Event ticker format:** `KXMLBGAME-{YYMMMDD}{HHMM}{AWAYTM}{HOMETM}` — e.g. `KXMLBGAME-26MAY101920DETKC` = Detroit @ Kansas City, May 10 at 19:20 ET
- **Markets per event:** 2 — one per team, binary "Team X wins"
- **Sample depth (May 8, 2026, 18:25 UTC):**
  - ATL/LAD: ATL = YES 51¢ × 650, NO 45¢ × 201, spread 4¢
  - STL/SD: SD = YES 53¢ × 1, NO 45¢ × 275, spread 2¢
  - NYM/AZ: NYM = YES 51¢ × 158, NO 47¢ × 40, spread 2¢
- **Recent trade flow:** 277 trades across 5 games sampled in /trades endpoint (last 100 each)
- **Settlement:** mechanical, final score determines winner

---

## 3. Data sources

| Need | Source | Cost | Latency | Decision |
|---|---|---|---|---|
| Sharp consensus moneyline | **The Odds API** (free tier 500 req/mo, $59/mo unlimited) | $0 → $59/mo | <1s | **Build for free tier first; upgrade if engine validates** |
| Sharp consensus moneyline (alt) | Pinnacle.com /odds-feed (no public API; web scrape) | $0 | brittle | fallback only |
| Sharp consensus moneyline (alt) | OddsJam ($299/mo) | $299/mo | <1s | reject — too costly until real-money decision |
| Game state, lineups | MLB Stats API (statsapi.mlb.com) | $0 | <1s | use |
| Probable starters, weather | Rotowire RSS / FanGraphs | $0 | a few hr | use |
| Historical lines for backfill | OddsHistory (the-odds-api archive) | one-time | n/a | one-time backfill purchase if needed |

**Open question for Steve:** OK to use The Odds API free tier (~16 req/day budget, enough to poll 4× per game-day on game days)? Or buy the $59/mo plan now to allow full pre-game polling cadence? Free tier is sufficient for spec and shadow-trade phase.

---

## 4. Architecture (mirrors T3 patterns)

```
~/Documents/
  terminal6_mlb_spec.md                    (this file)
  terminal6_mlb_kalshi_logger.py           (5-min poll on KXMLBGAME events, /markets, /orderbook)
  terminal6_mlb_lines_puller.py            (Vegas sharp lines from Odds API, every 30 min on game days)
  terminal6_mlb_game_state.py              (MLB Stats API: schedule, lineups, weather, starters)
  terminal6_mlb_paper_trader.py            (signal generator + ShadowLedger.open() — DRY-RUN-FIRST)
  terminal6_mlb_settlement_reconciler.py   (settles on final score from MLB Stats API)
  terminal6_sigma_fit.py                   (empirical sigma on residuals, mirrors T3b pattern)
  terminal6_data/
    kalshi_KXMLBGAME-*.jsonl               (per-event snapshots)
    vegas_lines_*.jsonl                    (per-game sharp lines, joined by game_id)
    game_state_*.jsonl                     (MLB Stats API joined data)
    settlements.jsonl                      (final scores, settlement records)
  empirical_sigma_t6.json                  (sigma table, fit on closed games)
```

**Engine ID for ledger:** `T6` — same starting bankroll allocation as other engines ($5,000 simulated).

---

## 5. Signal generation

**Variables:**
- `kalshi_implied_p`: yes_ask price on the team's win market (e.g., ATL @ 51¢ means market thinks 51% ATL wins)
- `sharp_implied_p`: derived from sharp consensus moneyline, de-vigged to fair-value probability
- `delta = sharp_implied_p − kalshi_implied_p`

**Trade triggers (initial — to be tuned post-shadow):**
- `delta ≥ 0.03` (3 percentage points) → BUY YES on the Kalshi side (we think Kalshi is underpricing)
- `delta ≤ −0.03` → BUY NO (overpricing)
- Minimum `kalshi_implied_p ∈ [0.20, 0.80]` (skip extreme heavy favorites/dogs where delta is tiny in absolute terms)
- Minimum spread ≤ 5¢ (no fills inside wide books)
- Skip if game starts in <15 min (Kalshi markets close minutes before first pitch and we want margin)

**Decision needed:** are these initial thresholds reasonable, or should we be tighter (5pp delta) initially to avoid noise?

**Sizing:**
- Initial: 5 contracts per signal, $0.40-$0.60 entry typical = $2-3 cost per position
- Per-game cap: max 1 position (don't both-side a game by accident)
- Daily cap: 10 positions
- Total open cap: 30 positions

---

## 6. Sigma calibration

Mirrors T3b. After every settled batch (~daily), refit `empirical_sigma_t6.json` on the residual distribution (kalshi_implied_p − settled_outcome). Bucket by `kalshi_implied_p` decile and `delta` decile. n≥10 per bucket required for use; fallback to global mean otherwise. Same JSON shape as T3b's sigma table.

---

## 7. Validation milestones

- **End of week 1 (May 15):** logger + lines puller + paper trader live in dry-run. n=10-20 settled.
- **End of week 4 (June 5):** n≈100 settled. First sigma refit. Initial edge-bucket analysis (mirrors T1 analysis we just ran).
- **End of week 6 (June 19):** n≈200 settled. Go/no-go decision on real-money sizing.
- **Real-money go-live criteria:** n ≥ 200, lower-95 CI on after-fee mean P&L > 0, no single bucket driving >40% of profit, σ-table residuals fit modeled distribution.

---

## 8. Trader-side freshness gate

Same as T3 traders. At the top of the open() codepath:
```python
if (Path.home() / "Documents" / "freshness_alarm.flag").exists():
    return  # refuse to open while data is stale
```
Also — engine-specific staleness check: refuse to open if `vegas_lines` for this game is older than 60 min (sharp consensus stale).

---

## 9. Watchdog integration

Add four rows to `portfolio_freshness_watchdog.py`:
- `T6 kalshi logger` — `kalshi_logger.log` — max 0.5h
- `T6 lines puller` — `lines_puller.log` — max 1.0h on game days
- `T6 game state` — `game_state.log` — max 4h (slower-moving)
- `T6 paper trader` — `paper_trader.log` — max 1h

---

## 10. Scheduler integration

Add three jobs to `scheduler_jobs.json`:
- `t6_mlb_lines` — every 30 min, 24h on game days, sharp Vegas line pull
- `t6_mlb_game_state` — every 6h, MLB Stats schedule + lineup pull
- `t6_mlb_settlement` — every 4h, attempt to settle any games where final score is posted

The kalshi logger and paper trader are continuous-loop nohup daemons (5-min and 30-min cycles), not scheduler jobs.

---

## 11. Kill criteria

T6 is killed if:
- After n=200, lower-95 CI on after-fee P&L is negative (no detectable edge)
- After n=100, sharp-vs-Kalshi delta closes to <1pp average (means Kalshi MMs caught up to sharp books)
- The Odds API rate limit makes the engine impossible to run at sufficient cadence and the $59/mo upgrade doesn't return positive expected value at current sizing

Any of these triggers a single-page postmortem and engine archival, mirroring T4 archived state.

---

## 12. What this spec does NOT include (deferred to v0.2 if v0.1 validates)

- In-game live trading (each pitch resolves new info)
- Player props (over/under on individual stat lines) — different framework, separate engine
- NBA / NHL / NFL extensions of the same pattern (same architecture, different data sources)
- Tennis / golf — different signal structure (single-elimination vs head-to-head per match)

---

## 13. Decisions confirmed by operator 2026-05-08

1. **Data source:** The Odds API free tier ✓
2. **Trade threshold:** delta ≥ 3pp ✓
3. **Sizing (Phase 1, n=0 to 100):** 10 contracts per signal
4. **Daily cap:** 15 positions, 30 total open
5. **Phase 2 ramp (n=100 to 300):** 25 contracts per signal, 25 daily cap
6. **Real-money decision criteria:** n ≥ 300, lower-95 CI on after-fee mean P&L > 0, no single bucket >40% of profit

**Status: code ship in flight as of 2026-05-08 18:50 UTC.**
