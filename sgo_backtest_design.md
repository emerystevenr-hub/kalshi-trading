# SGO Pro Historical Backtest — Design

_Created 2026-05-10. Trial expires ~2026-05-17. Decision gate for $299/mo subscription._

## Three questions (operator-defined)

| # | Question | Window | Decision role |
|---|---|---|---|
| Q1 | Median \|Pinnacle − Kalshi\| delta at T-60 min | single snapshot per game | Diagnostic — sets the noise floor |
| Q2 | Fraction of games where \|Pinnacle − Kalshi\| > 3pp at any point | T-120 to T-0, 5-min cadence | Diagnostic — frequency of tradeable mispricings |
| Q3 | When \|Pinnacle − DK/FD/MGM consensus\| ≥ 1pp, how long until retail catches up? | full pre-game, 5-min cadence | **Subscription decision gate** |

## Decision rule (operator-locked)

Per the Q3 result:
- Median retail catchup ≤ 5 min → **cancel SGO trial**, the data upgrade is sub-spread noise
- Median retail catchup ≥ 30 min → **subscribe at $299/mo**, structural value
- Between 5–30 min → judgment call, default to cancel (margin not paying for itself)

## Pull plan

**Universe**: MLB games over the last 30 days (Apr 10 → May 10, 2026).

**Books**: Pinnacle, DraftKings, FanDuel, BetMGM. Pinnacle is the sharp anchor; the three retail books form the consensus comparator (matches T6's existing `consensus_*_p` math).

**Cadence**: 5-minute snapshots over the T-120 → T-0 pre-game window. ~24 timestamps per game per book.

**Storage**: `~/Documents/terminal7_data/sgo_hist_mlb_<YYYY-MM-DD>.jsonl`. One line per (game, timestamp) with all four books' moneyline and lastUpdatedAt.

**API**: `https://api.sportsgameodds.com/v2/events` with `leagueID=MLB`, plus the historical snapshot endpoint (probe to confirm exact path/params before bulk pull). Browser User-Agent header (Cloudflare 1010 mitigation per session-4 lesson 44). Cursor pagination.

## Data availability constraints (surface before pull)

**Q3 is fully answerable** with SGO Pro alone — pure SGO data, both Pinnacle and retail, 30-day window. This is the subscription-decision gate; it has no dependency on our local data.

**Q1 and Q2 are partially answerable.** Both compare Pinnacle to Kalshi, which requires Kalshi history. T6 kalshi snapshots go back to 2026-05-08 — three days. Options:
- **(a)** Run Q1/Q2 on the 3-day overlap window. Sample of ~30-50 games. Statistically thin but indicative.
- **(b)** Backfill Kalshi history via Kalshi's `/markets/{ticker}/candlesticks` REST endpoint. Adds time.
- **(c)** Defer Q1/Q2 entirely; lock the decision on Q3 alone. Q3 is the gate per the operator decision rule.

Recommendation: **(a)**. The 3-day Pinnacle-vs-Kalshi snapshot is enough to spot direction and order-of-magnitude, and Q3 is the gate anyway. Q1/Q2 will hint whether the engine's current reads against retail were systematically off-axis or within noise.

## Analysis plan

**Q1 — Median \|Pinnacle − Kalshi\| at T-60**
- For each game in the 3-day overlap, find the snapshot closest to T-60 (within ±2.5 min).
- Compute Pinnacle home_implied_p (American → probability) and Kalshi home_implied_p (mid of yes_top/no_top complement).
- Take \|pin − kalshi\| per game, median across games.
- Output: scalar (e.g. "0.014" = 1.4pp).

**Q2 — Fraction of games breaching 3pp at any point T-120 to T-0**
- For each game in 3-day overlap, scan all 5-min snapshots in [T-120, T-0].
- Mark game as "breach" if any snapshot has \|pin − kalshi\| ≥ 0.03.
- Output: breach_fraction = breach_count / total_games.

**Q3 — Retail catchup time**
- For each game in 30-day window, scan all 5-min snapshots.
- Identify "divergence events": consecutive snapshots where Pinnacle moves ≥1pp AND retail consensus has not yet moved (\|pin − retail\| transitions from <0.005 to ≥0.01 between t-5 and t).
- For each divergence event, scan forward in 5-min increments until \|pin − retail\| < 0.005. Record the elapsed minutes (clipped at 60 min).
- Output: median catchup time across all events. Also report 25th/75th percentiles and event count.

## Probe gate (before bulk pull)

1. Single-game probe on tonight's PIT @ SF (commence 20:06 UTC, T-91 min).
2. Confirm:
   - SGO returns the event in `/v2/events?leagueID=MLB`
   - `byBookmaker.pinnacle` (or whatever the Pro tier key is — verify exact key) exists with populated odds + recent lastUpdatedAt
   - DK/FD/MGM keys also populated
3. Confirm historical endpoint shape (single past game) before bulk pull.
4. If probe returns sparse Pinnacle (e.g. only on some games), the backtest design needs adjustment — may need to weight by Pinnacle availability or fall back to Circa/Bookmaker.eu.

## Acceptance criteria for this work

- Q3 result computed across at least 30 games with ≥5 divergence events
- Q1, Q2 reported with sample size disclosure
- Decision (subscribe / cancel) recommended with one-line rationale
- All raw data preserved as JSONL for audit

## Out of scope

- Kalshi vs SGO data-swap implementation. That's downstream of the subscription decision.
- Historical Kalshi backfill via candlesticks endpoint. Considered, deferred per option (a) above.
- T7 fire timing. T7 hasn't fired anything yet; backtest is MLB-only by design (T6 is the validator).
