# Terminal Trading Portfolio — Handoff

_Generated 2026-05-10 (session 5, late UTC). Hand to a fresh session to resume in context. Supersedes HANDOFF_2026-05-10_session4.md._

_Next session is the **Friday agent**. The prior agent goes dark after the morning T6 log confirmation on 2026-05-11 and resumes Friday night PT. While dark, three settlement events fire (T3b CPI May 13, T3c ICSA May 14, T7 conf finals G1/G2 begin May 13-15). The Friday agent's primary work is verification of each, plus T6 patch confirmation on real data, plus the May 17 SGO cleanup. Direction in §11._

---

## TL;DR — what changed in session 5

Session 5 caught and fixed a **CRITICAL** contamination bug in T6's vegas-matching logic that had silently fabricated 100% of T6's lifetime closes (25 of 25). Same bug class patched in T7 before T7 ever fired. SGO Pro $299/mo trial decisively rejected via 403-game open/close analysis. Two May 17 cleanup actions queued.

**T6 vegas-match bug — caught + patched.** The session-3 fix for the multi-game-series matching issue (line 264-307 in `terminal6_mlb_paper_trader.py`, "prefer soonest upcoming") introduced a worse bug: `min(future, key=_commence_dt)` selected the soonest *upcoming* game when matching by team-pair, but didn't check that the matched Vegas game corresponded to the Kalshi ticker's encoded date. Result: a settled-game ticker (e.g. `KXMLBGAME-26MAY091610WSHMIA`, May 9 16:10 ET game) bound to the *next day's* Vegas line (May 10 16:16 UTC), Kalshi was at settled price (0.20 NO / 0.80 YES because WSH won), engine fired NO at $0.20, reconciler closed at $0.80 the next cycle, repeat. Over ~12 hours the engine fired 25 closes for $+3,676.69 — all 25 contaminated by the same mechanism. Median ticker-vs-commence mismatch was ~20-23 hours.

**Patch shipped both engines.** New helper `parse_ticker_game_dt_utc()` parses the Kalshi ticker's encoded date as ET → UTC datetime. `evaluate_market` narrows Vegas candidates to within ±12h (T6 MLB tickers carry full datetime) or ±18h (T7 NBA/NHL tickers carry date only, anchored 19:30 ET) of the ticker's encoded datetime BEFORE running future/past selection. Helper unit-tested across 8 cases per engine including malformed tickers, post-DST dates, missing prefixes. End-to-end tested against the actual May-9 ticker scenario — correctly rejects with new bucket "no vegas match (no game within 12h of ticker date)".

**Milestone gate filtering.** `terminal6_milestone_check.py` now defaults to filtering contaminated closes via heuristic: signal_metadata `commence_time_utc` more than 12h from ticker's encoded date. `--include-contaminated` flag for audit view. Current state: clean view shows ACCUMULATING n=0 (engine starts fresh on the validation clock); audit view preserves the $+3,676.69 fabricated P&L for traceability.

**Both daemons restarted live with patch on 2026-05-10 18:31 UTC.** T6 PIDs ws=60083, trader=60087, reconciler=60091; T7 PIDs ws=60139, trader=60143, reconciler=60147, lines_puller=60151. All TTY=??. T6 first post-restart cycle hit `[cap] daily opens 15 ≥ 15, skipping` because the contaminated opens from earlier in the day still counted toward today's UTC-day cap; first clean fires expected after 2026-05-11 00:00 UTC. T7 first post-restart cycle: 24 markets evaluated, all 24 rejected at Filter 14 (game N>2) — correct pre-conf-finals behavior; T7 awaits conf finals G1 markets to appear.

**SGO Pro $299/mo — DECIDED CANCEL.** Single-game probe on PIT@SF tonight confirmed Pinnacle present + fresh in `byBookmaker.pinnacle`. Endpoint discovery confirmed SGO Pro REST exposes only `openBookOdds` + `closeBookOdds` market-level aggregates and per-book single-snapshot odds — NO 5-min cadence time-series. The §11 backtest design (5-min Pinnacle-vs-Kalshi delta over T-120 to T-0) cannot be answered with this tier. Pivoted to D analysis (open/close magnitude + lastUpdatedAt timing): chunked-pulled 403 finalized MLB games over 30 days. Result:

- Pinnacle moves *less* than retail in 81.1% of games (327 of 403)
- Median Δ_pinn − Δ_retail = **−3.09pp** (Pinnacle moves 3pp closer to consensus open than retail does)
- Median |Δ_pinn| / |Δ_retail| ratio = 0.928
- 97.0% directional alignment (Pinnacle and retail agree on direction in 391 of 403 games)
- Pinnacle's lastUpdatedAt leads retail-median by **3.9 min median** (sub-spread per the 5-min cancel rule)

Both Steve's locked decision rules agree → CANCEL. B (forward poll) skipped per pre-locked rule "if D shows convergence, B may already tell you the answer is no." See `~/Documents/sgo_d_decision_report.md` for full report.

**Two May 17 cleanup actions queued, both operator-side.** (1) Cancel SGO Pro trial in their billing dashboard before auto-bill. (2) `rm ~/Documents/.sgo_api_key` to remove the credential reference.

**Macro cap status unchanged: 50.0% AT CAP, exit code 1.** No new macro deployment permitted.

**KL kill-switch decision unchanged.** Manual diagnostic only through n=300. Carried from session 4. Future agents should not propose adding KL automation before n=300 (deliberate, not oversight).

---

## Decision register (locked this session)

### Vegas-match data integrity — CRITICAL (NEW, locked 2026-05-10)

The Kalshi ticker carries an unambiguous encoded game datetime. Any Vegas/sharp-line match from a team-pair lookup MUST also constrain to within ±12h (MLB time-encoded tickers) or ±18h (NBA/NHL date-only tickers) of the ticker's encoded datetime. Without this constraint, a multi-day series silently mis-binds Game-N's Kalshi ticker to Game-(N+1)'s Vegas line. Apply this rule in any future engine that uses team-pair-only matching against external sharp data.

### SGO Pro $299/mo — CANCEL (NEW, locked 2026-05-10)

The decision rule was: subscribe IF Pinnacle leads retail by ≥3pp on >20% of games for ≥30 min. Achieved: Pinnacle moves smaller than retail by median 3pp; 4-min timing lead. Both magnitude and timing fail the rule decisively. SGO Pro REST tier is open/close + per-book snapshot only, not time-series; AllStar WebSocket may answer the timing question structurally but is a separate untested decision. **Don't reopen this without new evidence** (e.g. an SGO AllStar tier trial).

### KL automated kill-switch — manual through n=300 (carried, still binding)

Decision from session 4: manual diagnostic only through n=300. Rolling 30-day median KL diagnostic to be added to Monday milestone output (open task). Future agents should not propose KL automation before n=300; deliberated, not overlooked.

### Build queue (updated)

| # | Item | Type | Status | Earliest start |
|---|---|---|---|---|
| 0a | T1 sub-bucket gate + capital recycle | prerequisite | ✅ shipped session 2 | done |
| 0b | Macro concentration dashboard + cap | prerequisite | ✅ shipped session 2 | done |
| 1 | T7 (NBA/NHL playoffs, Game 1-2 only) | engine, $2K shadow | ✅ DEPLOYED 2026-05-10; vegas-match fix shipped before any fires | done; pending first cycle |
| 2 | ~~T8 (NFP nowcast)~~ | engine | ARCHIVED 2026-05-09 | n/a |
| 2-replacement | TBD: JOLTS+Indeed / PMI / HFW / Truflation | engine | gated on residual study | post-T7 first cycle |
| 3 | T9 (crypto, funding-rate indicator) | engine | gated on correlation matrix | after T6 hits clean n≥100 |
| 4 | T10 (earnings) | engine | likely permanent defer | n/a |
| 5 | T11 (NFL game-winner) | engine, conditional | gated on T6 validation (clean n≥300, lower-95 ≥ 0) | September 2026 |

### T6 early-kill criteria (unchanged in code)

`terminal6_milestone_check.py`, runs Mondays 9:09 AM PDT:
- `n ≥ 200 and lower_95 < -$0.50` → EARLY KILL
- `n ≥ 300 and lower_95 ≥ 0` → VALIDATED
- `n ≥ 300 and mean > 0 and lower_95 < 0` → INCONCLUSIVE (extend to n=500)
- `n ≥ 300 and mean ≤ 0` → DEAD
- else → ACCUMULATING

**Note (session 5):** the contamination filter excludes pre-2026-05-10 18:31 UTC fires from the gate. Effective n is 0 as of session-5 close. n≥200 / n≥300 thresholds are unchanged; validation clock now runs from clean fires only.

### T7 milestone gates (unchanged in code)

Per spec §6:
- `n ≥ 5 and architectural_failures == 0` → ARCHITECTURE_CONFIRMED
- Any architectural failure → ARCHITECTURE_FAIL (operator sets `~/Documents/terminal7_data/architecture_fail.flag`)
- `n < 3 by 2026-05-25` → INSUFFICIENT_LIQUIDITY
- date ≥ 2026-06-15 → END_OF_RUNWAY
- else → ACCUMULATING

### Macro concentration cap

50.0% AT CAP. exit code 1. Block any new macro deployment.

### Data source for T6 + T7 — LOCKED

**The Odds API free tier.** SGO Pro decisively rejected via 403-game backtest. T6 + T7 lines pullers stay on Odds API. Documented thesis is now "retail-aggregator-consensus vs Kalshi" arbitrage, functionally within ~1pp of sharp-vs-Kalshi at any snapshot, retail catches up to Pinnacle within ~4 min. T6's clean-n=300 milestone is a valid measurement of that arbitrage.

### Adversarial review process

Caught 6 issues at session-4 deployment of T7 — but **missed the vegas-match bug** across both T6 and T7 because the spec didn't describe the data join at high enough resolution. New checklist addition documented as session-5 lesson 50: *for every join key, what could match the wrong record? Show me the disambiguator.*

---

## Strategic context

- **Operator**: Steve Emery, Bend OR (Pacific Time). Direct, no fluff, one strong direction, structural advantage > tactical optimization.
- **Operating pattern this session**: explicit sign-off gates on CRITICAL/HIGH fixes; "show me the diff before commit" on critical-path changes; "one terminal prompt at a time" on operator-side commands; bulk-approve M/L; defer items that touch live execution paths between now and May 14.
- **Goal**: $500k profit, multi-year, multi-engine.
- **Validation discipline**: No engine goes to real money until clean n ≥ 300 with positive lower-95 CI on after-fee mean P&L. T6 has explicit early-kill at n=200.
- **Cadence-bound thesis**: macro engines (T3a/T3b/T3c) max out at ~24 events/year combined. Sports (T6/T7/T11) is the daily-cadence vertical that scales.
- **Filter for new engines**: independent leading indicator outside Kalshi AND costly enough that retail can't easily access it.

---

## Portfolio P&L — Lifetime (post-session-5)

| Engine | Open | Closed (clean) | Realized (clean) | Open CB | Mode | Notes |
|---|---|---|---|---|---|---|
| T1 (weather) | varies | 311+ | -$121 after fees | varies | shadow_subset | restricted to 40-50¢ entry bucket; $1K bankroll |
| T2 (catalyst) | 4 | 8 | -$161.48 | $155 | archived | thesis kill 2026-05-09 |
| T3a (Fed arb) | 0 | 0 | $0 | $0 | shadow | scanner running, no opens; June 17 FOMC |
| T3b (CPI) | 2 | 0 | $0 | $11.28 | shadow | first event May 13 — auto-settles |
| T3c (Claims) | 1 | 5 | +$4.25 | $15.25 | shadow | second print May 14 — Cowork-scheduled |
| T6 (MLB) | 12 | 0 (audit: 25) | $0 (audit: $+3,676.69) | varies | shadow | post-fix as of 2026-05-10 18:31 UTC; cap-locked through May 11 00:00 UTC |
| T7 (NBA/NHL) | 0 | 0 | $0 | $0 | shadow | $2K bankroll; awaiting conf finals G1 (~May 13-15) |
| T5 (catalyst finder) | — | — | — | — | infrastructure | bankroll $0; T2 downstream archived |
| T4 (Polymarket arb) | — | — | — | — | archived | US-restricted |

**T6 historical "lifetime" P&L of $+3,676.69 across 25 closes is preserved in the ledger** but quarantined from the milestone gate. All 25 closes have ticker-vs-commence mismatch ≥19h, confirming all were contaminated. Clean validation clock starts post-2026-05-10 18:31 UTC.

T6 currently has 12 open positions at exposure $568.64, opened pre-fix from contaminated-vegas matches. They will close at next reconciler cycle; their P&L will also be flagged contaminated by the milestone filter on close.

---

## Engine States

### T1 — Kalshi Weather Ensemble (RESTRICTED to 40-50¢ subset)

**Status:** running, gated to validated subset. Bankroll $1K. **Changes session 5:** none. **Kill criterion:** if 40-50¢ bucket lower-95 turns negative on next n=50 closes, T1 archives.

### T2 — Kalshi Catalyst Book (ARCHIVED)

**Status:** archived 2026-05-09 (session 2). Bankroll $0. **Changes session 5:** none.

### T3a — Kalshi Fed Decisions

**Status:** scanner running, no opens. Next FOMC June 17-18. **Changes session 5:** none. **Deferred HIGHs (pre-FOMC pass):** H5-H9 from session 3 audit.

### T3b — Kalshi CPI Nowcast (FIRST DECISION-GATE EVENT MAY 13)

**Status:** running, 2 NO positions open on KXCPIYOY-26APR (T3.6, T3.7), $11.28 CB, max profit +$12.72. Reconciler under launchd KeepAlive (PID per session-3 was 40443; respawned across session boundaries).

**Changes session 5:** none.

**No manual action May 13.** First hourly tick after Kalshi finalizes ~14:00 UTC closes both positions automatically. Cowork task `t3b-cpi-settlement-verify-may13` fires 09:00 PT to confirm.

**Friday agent: verify** May 13 settlement happened cleanly, Cowork task ran, both T3.6 and T3.7 closed, realized P&L matches expectation (~+$12.72 if NO wins; ~-$11.28 if YES wins — outcome direction is data, not validation).

**Deferred HIGH (post-May 13):** H11 (annul-close ordering bug, inert today).

### T3c — Kalshi Initial Jobless Claims (SECOND EVENT MAY 14)

**Status:** running, 1 NO position open on KXJOBLESSCLAIMS-26MAY14 ($15.25 CB). Mode=shadow. H13 idempotency landed session 3.

**Changes session 5:** none.

**Cowork tasks armed for May 14:**
- 07:15 PT — `t3c-icsa-freshness-precheck-may14` — GREEN/YELLOW/RED on whether ICSA print landed
- 07:30 PT — `t3c-icsa-reconcile-may14` — runs reconciler `--once`

If pre-check returns YELLOW/RED, hold the reconciler.

**Friday agent: verify** May 14 settlement happened cleanly, both Cowork tasks ran, position closed, P&L visible in ledger.

**Deferred HIGH (post-May 14):** H15 (annul-close ordering, inert today).

### T6 — Kalshi MLB Game Markets (THESIS VALIDATOR — RESET POST-FIX)

**Status:** running, $12K bankroll. PIDs (post-session-5 restart): ws=60083, trader=60087, reconciler=60091. All TTY=??.

**Critical session-5 work:**
- Vegas-match bug fixed via ticker-date narrowing (±12h)
- 25 contaminated closes flagged via milestone filter
- Daemon restarted live 2026-05-10 18:31 UTC

**As of session-5 close:** post-restart cycle hit `[cap] daily opens 15 ≥ 15, skipping`. The cap counts contaminated opens from earlier today; reset at 2026-05-11 00:00 UTC. **First clean fires expected after midnight UTC May 11.**

**Friday agent must verify:**
1. T6 paper_trader.log shows `[FIRE]` lines from 2026-05-11 onward — these should be against actually-upcoming games (lead_min positive) with deltas in the 3-5pp range
2. No `[FIRE]` line binds a settled-day ticker to a future-day Vegas — cross-check by parsing each open's `signal_metadata.event_ticker` date and confirming `signal_metadata.commence_time_utc` is within ±12h
3. New rejection bucket "no vegas match (no game within 12h of ticker date)" appearing — that's the patch ACTIVELY firing on stale tickers
4. milestone_check.py shows clean n > 0 with reasonable mean / lower_95 (no longer all-zero)

**Open: rolling 30-day median KL diagnostic** to be added to Monday milestone output. See bug-fix queue.

### T7 — Kalshi NBA/NHL Playoffs (PATCHED + DEPLOYED, AWAITING CONF FINALS)

**Status:** ✅ live shadow, $2K bankroll. Mode=shadow. Active=true. PIDs (post-session-5 restart): ws=60139, trader=60143, reconciler=60147, lines_puller=60151. All TTY=??.

**Spec at:** `~/Documents/terminal7_nba_nhl_spec.md`

**Filters in code (in priority order):**
1-13. Inherited from T6 (freshness, parse, vegas match — NOW with ticker-date narrowing, lead time, freshness, spread, kalshi p band, delta threshold, cluster cap, daily/total opens, entropy collapse, Kelly).
14. Game-number gate via `GAME_NUM_RE` on event title; reject N>2.
15. Per-series exposure cap via synthetic `series_id`.
16. Liquidity floor: skip if `OI < 100 AND vol_24h < 50`.

**Sentinel:** `[ALERT] title-parse-failed` fires only on titles where "game" appears but regex fails. Non-game contracts hard-skip silently.

**Session-5 patch:** added `parse_ticker_game_dt_utc()` for NBA/NHL ticker format (yymmmdd, anchored 19:30 ET); `evaluate_market` narrows by ±18h before future/past selection. Same fix class as T6.

**Current behavior (post-restart 2026-05-10 18:31 UTC):** 24 markets evaluated, 24 rejected at Filter 14 (game N>2). All current playoff markets are 2nd-round Game 3+ or later. Engine waits for conf finals G1/G2 markets to appear.

**Runway dates:**
- Conf Finals G1-G2: ~May 13-25 (~8 NBA + ~8 NHL games)
- Finals G1-G2: ~June 2-10
- Total G1/G2 universe: ~12-16 games combined NBA+NHL

**Friday agent must verify:**
1. T7 paper_trader.log shows `[FIRE]` lines from conf finals games starting ~May 13-15
2. Each fire's `signal_metadata.commence_time_utc` is within ±18h of the ticker's encoded date
3. Filter 14 still firing on G3+ markets
4. Filter 15 series-cap firing on second open within a series (G1+G2 = max 2)
5. milestone_check.py — n moves toward 5 (ARCHITECTURE_CONFIRMED gate)
6. No `[ALERT] title-parse-failed` — if it fires, Kalshi changed event-title format

**Open MEDIUMs (deferred, all opportunistic):** M1 counter staleness 1-cycle window, M2 already_open O(N×M), M3 parse_event_ticker defensive coding, M4 no-event_title bucket, M5 vegas sport-tag fallback. None are deployment blockers.

### T8 — Kalshi NFP nowcast (ARCHIVED)

**Status:** archived 2026-05-09 pre-deployment. **Changes session 5:** none.

### T5 — Catalyst finder (DORMANT)

**Status:** module still runs but downstream is archived. Bankroll $0.

---

## Audit findings — full disposition (post-session-5)

### CRITICAL (2, both resolved)

- ✅ C1 (session 4): redeploy_t7_all.sh `set -e` aborts before EC capture — fixed via set +e/set -e wrapper.
- ✅ C2 (session 5): T6+T7 vegas-match wrong-day binding — fixed via ticker-date narrowing in `evaluate_market`. Helper `parse_ticker_game_dt_utc()` added to both engines.

### HIGH (5 + 0, all resolved)

- ✅ H1-H6 from session-4 T7 review.

### MEDIUM (5 deferred + 1 NEW)

- M1-M5 T7 — opportunistic cleanup, deferred.
- **M6 (NEW session 5):** SGO trial credential file `~/Documents/.sgo_api_key` will become a stale reference after May 17 trial expiry. Cleanup queued operator-side (May 17).

### LOW (4, opportunistic)

- L1-L4 unchanged from session 4.

### NEGATIVE TEST FINDINGS

- N1, N2 unchanged from session 4.

---

## Bug-fix queue (post-session-5)

1. **Rolling 30-day median KL in Monday milestone output.** ENGINEERING TASK. Carried from session 4. Modify `terminal6_milestone_check.py` to compute and display median KL over last 30 days from `near_miss_kl.jsonl`.

2. **`AlreadyClosedError` class in `shadow_pnl_core.py`.** Carried. Replaces string-match fragility across all reconcilers. ~10 min refactor.

3. **T3b annul-close ordering** (H11): mirror canonical `compute_state` from `shadow_pnl_core`. Post-May 13.

4. **T3c annul-close ordering** (H15): same fix. Post-May 14.

5. **T3a hardening pass** (H5-H9 from session 3 audit): pre-June 17 FOMC.

6. **T7 MEDIUMs M1-M5** (session-4 review): opportunistic cleanup.

7. **Defensive isinstance guards in `detect_sport`/`parse_kalshi_event_teams`** (N2 from session-4 negative test): one-line fix.

8. **NEW (session 5): post-May 17 SGO cleanup.** (a) Cancel SGO Pro trial in dashboard. (b) `rm ~/Documents/.sgo_api_key`. (c) Optionally `rm ~/Documents/terminal7_data/sgo_d_pull.log` (empty placeholder). All operator-side, ~1 min total.

9. **NEW (session 5): adversarial review checklist update.** Add "for every join key, what could match the wrong record?" check. Documented as session-5 lesson 50.

---

## Active Daemons & Schedules — verified post-session-5 redeploy

### Scheduler (`portfolio_scheduler.py`)

5 jobs configured. All exit=0 at session-5 close.

| Job | Cadence | Status |
|---|---|---|
| t1_nws_actuals | 6h | enabled |
| t3c_claims_data | 12h | enabled |
| t2_daily_picks | 24h | DISABLED |
| t6_mlb_lines_puller | 90min | enabled |
| entropy_collapse_detector | 5min | enabled |

### Long-running daemons (verified TTY=?? this session)

| Engine | PID (session 5) | Daemon | Notes |
|---|---|---|---|
| T6 ws_logger | 60083 | `terminal6_mlb_kalshi_ws.py` | RESTARTED session 5 |
| T6 paper trader | 60087 | `terminal6_mlb_paper_trader.py --live --interval-sec 1800` | RESTARTED session 5; **vegas-match fix LIVE** |
| T6 reconciler | 60091 | `terminal6_mlb_settlement_reconciler.py --interval-sec 3600` | RESTARTED session 5 |
| T3b reconciler | (launchd) | `com.t3b.settlement-reconciler` | KeepAlive respawn |
| T7 ws_logger | 60139 | `terminal7_kalshi_ws.py` | RESTARTED session 5 |
| T7 paper trader | 60143 | `terminal7_paper_trader.py --live --interval-sec 1800` | RESTARTED session 5; **vegas-match fix LIVE** |
| T7 reconciler | 60147 | `terminal7_settlement_reconciler.py --interval-sec 3600` | RESTARTED session 5 |
| T7 lines puller | 60151 | `terminal7_lines_puller.py --interval-sec 900` | RESTARTED session 5 |

PIDs change per session boundary. TTY=?? confirmed at restart.

### Scheduled Cowork tasks (active)

- `t3b-cpi-settlement-verify-may13` — fires 2026-05-13 09:00 PT
- `t3c-icsa-reconcile-may14` — fires 2026-05-14 07:30 PT
- `t3c-icsa-freshness-precheck-may14` — fires 2026-05-14 07:15 PT
- `t1-edge-rerun-weekly` — Mondays 08:03 PT
- `t6-shadow-trading-milestone-checks` — Mondays 09:09 PT
- `entropy-detector-calibration` — daily 08:09 PT

### Status command

```
bash ~/Documents/portfolio_status.sh
```

(Note: portfolio_status.sh uses `ps -ax` against host PIDs and `/opt/homebrew/bin/python3` — runs on operator's host, NOT in sandbox. Sandbox-side milestone checks via `python3 ~/Documents/terminal6_milestone_check.py` work standalone.)

---

## Critical Operational Lessons

### Net new from session 5

**49.** `min(future, key=_commence_dt)` for vegas-match by team-pair-only is unsafe when a ticker carries an unambiguous date. Constrain candidates to within ±12h (MLB time-encoded) or ±18h (NBA-NHL date-only) of the ticker's encoded datetime BEFORE the future/past selection. Without this, a multi-game series mis-binds Game-N's Kalshi ticker to Game-(N+1)'s Vegas line, fabricating "winning" trades on already-settled markets. T6 burned 25/25 closes on this in 12 hours, fabricating $3,676.69.

**50.** Adversarial review against the spec doesn't catch vegas-matching bugs because the spec doesn't describe the data join at low enough resolution. The session-3 fix that introduced lesson-49's bug was reviewed at the time. **Standing addition to review checklist:** for every join key, what could match the wrong record? Show me the disambiguator.

**51.** SGO Pro REST API exposes only opening + closing aggregate odds (`openBookOdds`, `closeBookOdds`) at the market level, plus per-book single-snapshot odds. NO time-series, NO 5-min cadence, NO movement history. AllStar plan WebSocket streaming is the time-series tier and was not in scope of this trial. If a future task needs lead-time analysis at finer than per-game granularity, evaluate AllStar separately at a different price point.

**52.** The "sharp leads retail by 30+ min" thesis is dead at REST granularity. 403-game backtest result: Pinnacle moves SMALLER than DK/FD/MGM consensus by median 3.09pp; Pinnacle's lastUpdatedAt leads retail by ~4 min median (sub-spread). Retail captures Pinnacle's signal AND overshoots within minutes. T6/T7 staying on Odds API is correct. AllStar upgrade is the only remaining path that could re-open the thesis.

**53.** When zsh barfs `zsh: no matches found:` on a glob, the ENTIRE command-line aborts — subsequent commands in a chain don't run. Bash silently passes unmatched globs through. If you give an operator a multi-command zsh paste with globs, all paths must be guaranteed to match OR use bash subshell OR `setopt NULL_GLOB`. Cost us one round-trip on the SGO cleanup verification.

**54.** Sandbox bash invocations are isolated — no cwd / env / process carryover. Background processes via nohup/`&` do not survive across bash calls because each call is its own container. For pulls that exceed the per-call timeout (~45s), chunk the work into smaller windows that each complete inside one call. The 30-day SGO pull was completed via 16 chunked calls.

**55.** SGO REST `byBookmaker` per-book entry has only three fields: `odds`, `lastUpdatedAt`, `available`. There is no per-book opening odds field. Market-level `openBookOdds` is the consensus open across all books, which approximates Pinnacle's open (since Pinnacle is one of 59 books) but isn't strictly Pinnacle-specific. Any "Pinnacle move" computation against `openBookOdds` is therefore confounded — but the confound is bounded and the directional result (Pinnacle moves less, leads by 4 min) was consistent across signals.

### Carried forward (still binding — see HANDOFF_2026-05-10_session4.md and earlier for full text)

Lessons 1-48 still apply. Of particular relevance to the Friday agent:

- **40** (`set -e` aborts before EC capture)
- **42** (hardcoded UTC offsets break DST)
- **43** (Unicode accents silently fail equality)
- **44** (Cloudflare 1010 blocks default urllib UA — use Mozilla/Safari UA spoof for SGO)
- **45** (adversarial review is high-leverage but not exhaustive — see lesson 50)
- **47** (dry-run validation gates must mirror live behavior)
- **48** (sandbox vs host filesystem path mismatch — `Path.home()` returns sandbox user home, NOT operator's host home)

---

## File Map (additions/changes session 5)

```
~/Documents/
  HANDOFF_2026-05-10_session5.md       (NEW — this file, supersedes session4)
  HANDOFF_2026-05-10_session4.md       (prior; keep for audit)

  # SESSION-5 CRITICAL-PATH PATCHES
  terminal6_mlb_paper_trader.py        (MODIFIED — added zoneinfo import,
                                        parse_ticker_game_dt_utc helper,
                                        ticker-date narrowing in evaluate_market,
                                        new rejection bucket "no vegas match
                                        (no game within 12h of ticker date)")
  terminal7_paper_trader.py            (MODIFIED — same fix, NBA/NHL date-only
                                        format, ±18h window)
  terminal6_milestone_check.py         (MODIFIED — added zoneinfo import,
                                        contamination filter (default-on),
                                        --include-contaminated audit flag,
                                        contamination summary in stdout)
  terminal6_dashboard.py               (MODIFIED — sum_t6_realized() applies
                                        same contamination filter as milestone
                                        check; T6 P&L row shows clean numbers,
                                        excluded count + P&L on dim audit line.
                                        Verified consistent w/ milestone_check)
  shadow_dashboard.py                  (MODIFIED — portfolio-wide dashboard
                                        also applies T6 contamination filter
                                        when pairing closes with opens. T6 row
                                        + portfolio totals + audit secondary
                                        line all consistent w/ milestone_check
                                        and terminal6_dashboard. T6-scoped
                                        filter; other engines unaffected.)

  # SGO trial deliverables (session 5, all kept for audit)
  sgo_backtest_design.md               (NEW — original Phase-1-thru-4 design
                                        with constraints surfaced)
  sgo_d_pull.py                        (NEW — chunkable 30-day open/close pull,
                                        --days-ago-start/--days-ago-end/--append)
  sgo_d_analyze.py                     (NEW — three-signal analysis,
                                        prints JSON_SUMMARY at end)
  sgo_d_decision_report.md             (NEW — CANCEL verdict, methodology, caveats)
  terminal7_data/sgo_open_close_30d.jsonl  (NEW — 403 rows, audit data)
  terminal7_data/sgo_d_pull.log        (empty placeholder; safe to remove)

  # Trial credential — schedule for May 17 removal
  .sgo_api_key                         (operator deletes May 17 after trial cancel)
```

---

## Pending Actions (ordered)

### Tonight (May 10, post-handoff)

- **Operator: confirm T6 fires clean closes after midnight UTC cap reset.** Single log check around 2026-05-11 00:30 UTC: `tail -50 ~/Documents/terminal6_data/paper_trader.log`. Look for `[FIRE]` lines whose `signal_metadata.event_ticker` encoded date is within ±12h of `signal_metadata.commence_time_utc`. The presence of new rejection bucket entries `"no vegas match (no game within 12h of ticker date)"` ACTIVELY firing on stale May-9 tickers CONFIRMS the patch is working.

### Tomorrow morning (May 11)

- **Operator: paste T6 cycle log to next session.** First clean cycle should fire after 00:00 UTC. Confirms patch works on real data before the agent is dark until Friday.

### May 13 — April CPI release (T3b auto-settle)

- ~12:30 UTC: BLS releases.
- ~14:00 UTC: Kalshi finalizes. T3b reconciler (launchd KeepAlive) auto-closes both T3.6 and T3.7.
- 09:00 PT: Cowork task `t3b-cpi-settlement-verify-may13` fires; verifies closure.
- **Friday agent: verify settlement happened cleanly. Report realized P&L for both positions.**

### May 14 — ICSA release (T3c reconcile)

- 05:30 PT: DOL releases ICSA, FRED mirrors within ~1 hour.
- 07:15 PT: Cowork freshness pre-check.
- 07:30 PT: Cowork reconciler `--once`. H13 idempotency landed session 3.
- **Friday agent: verify settlement happened cleanly. Report realized P&L.**

### May 13-15 — T7 conf finals G1/G2 begin

- NBA conf finals G1 ~May 13-14; NHL conf finals G1 ~May 13-15. Markets list as `KXNBAGAME-26MAY13...` / `KXNHLGAME-26MAY14...`.
- T7 lines puller pulls fresh; paper trader cycles every 30 min; first FIRE expected on first G1 with delta ≥ 3pp.
- **Friday agent: audit T7 paper_trader.log for first FIRE lines. Confirm ticker-date narrowing held (no settled-game stale fires). Report fire count, distribution by sport (NBA vs NHL), and any concentration patterns.**

### Weekly milestones (already scheduled)

- Mondays 08:03 PT: `t1-edge-rerun-weekly`
- Mondays 09:09 PT: `t6-shadow-trading-milestone-checks` — most-watched metric in portfolio
- Daily 08:09 PT: `entropy-detector-calibration`

### May 17 — SGO trial cleanup (operator-side, scheduled)

1. **Cancel SGO Pro trial in their billing dashboard before auto-renew.** This is the only action that prevents auto-bill.
2. **`rm ~/Documents/.sgo_api_key`** — removes credential reference.
3. (Optional) `rm ~/Documents/terminal7_data/sgo_d_pull.log` — empty placeholder.

The pull script + analysis script + decision report + raw data file (`sgo_open_close_30d.jsonl`) **STAY** for audit. Only the credential file goes.

### Engine slot #2 candidate selection (no rush)

Replacement for archived T8. Candidates: JOLTS+Indeed Hiring Lab, PMI Employment Index, high-frequency wage data (Homebase/Paychex), Truflation. Filter: independent leading indicator outside Kalshi AND costly enough that retail can't easily access it.

---

## §11 — DIRECTION FOR NEXT SESSION (Friday agent)

**You're picking up Friday night, Pacific time. The prior agent has been dark since Sunday/Monday morning. Multiple settlement events fired while dark; T6 vegas-match patch ran for ~4 days unattended. Your work is to verify each event, confirm patch behavior on real data, and surface any anomalies.**

### Resume sequence (priority order)

**1. Daemon health snapshot.**

Operator: `bash ~/Documents/portfolio_status.sh`

Confirm: scheduler clean (all exit=0), no freshness alarm, macro cap exit=1 (AT CAP, not BREACHED), all daemons TTY=??.

If any T6 or T7 daemon is missing or has TTY=ttys*, redeploy: `bash ~/Documents/redeploy_t6_all.sh` and/or `bash ~/Documents/redeploy_t7_all.sh`.

**2. T6 vegas-match patch confirmation (the most important item).**

`python3 ~/Documents/terminal6_milestone_check.py`

Expected: clean view shows `n > 0`, reasonable mean / lower_95 (probably noisy due to small n), gate=ACCUMULATING.

`python3 ~/Documents/terminal6_milestone_check.py --include-contaminated`

Expected: contaminated count is roughly 25 + N where N is the count of pre-fix open positions that closed during the dark window. New fires (post-2026-05-11 00:00 UTC) should NOT be flagged contaminated. **If new post-fix fires are being flagged contaminated, the patch is not working — investigate immediately.**

Cross-check: `tail -500 ~/Documents/terminal6_data/paper_trader.log | grep -E "FIRE|no vegas match.*ticker date"`. The "no vegas match (no game within 12h of ticker date)" rejection bucket should be present in the log — it's the patch ACTIVELY firing.

**3. T3b CPI settlement verification (May 13).**

Check ledger: `grep -E '"engine":"T3b"' ~/Documents/shadow_pnl/ledger.jsonl | grep '"type":"close"'`

Expected: two close events on 2026-05-13, on tickers KXCPIYOY-26APR-T3.6 and -T3.7. Sum of realized P&L. If positions closed at $1.00 NO, P&L ≈ +$12.72; if YES wins, P&L ≈ -$11.28.

Cross-check Cowork task `t3b-cpi-settlement-verify-may13` log/output if it captured anything.

**4. T3c ICSA settlement verification (May 14).**

Check ledger: `grep -E '"engine":"T3c"' ~/Documents/shadow_pnl/ledger.jsonl | grep '"type":"close"' | grep '2026-05-14'`

Expected: one close event on 2026-05-14, ticker KXJOBLESSCLAIMS-26MAY14. Realized P&L visible.

Cross-check Cowork tasks `t3c-icsa-freshness-precheck-may14` (07:15 PT) and `t3c-icsa-reconcile-may14` (07:30 PT).

**5. T7 first fires audit (May 13-15 onward).**

`tail -300 ~/Documents/terminal7_data/paper_trader.log | grep -E "FIRE|ALERT|size-cap"`

For each `[FIRE]` line:
- Verify ticker is from a Game-1 or Game-2 series (FILTER 14 enforced)
- Verify event_title contains "Game 1" or "Game 2"
- Cross-check against ledger: open's `signal_metadata.commence_time_utc` should be within ±18h of the ticker's encoded date. Use `parse_ticker_game_dt_utc` from `terminal7_paper_trader.py` to verify retroactively.

`python3 ~/Documents/terminal7_milestone_check.py`

Expected by Friday night: n=0-3, gate=ACCUMULATING. ARCHITECTURE_CONFIRMED needs n≥5; may not arrive by Friday but should within the next ~7-10 days. Report distribution by sport (NBA vs NHL) and by series.

**6. SGO May 17 cleanup status.**

Confirm operator did:
1. Cancelled SGO Pro trial in their billing dashboard
2. `rm ~/Documents/.sgo_api_key`

If not done by Friday: remind. The SGO Pro trial expires May 17 ~midnight; the key in `.sgo_api_key` no longer auths. Don't try to use it.

**7. Macro cap and any new alarms.**

`/opt/homebrew/bin/python3 ~/Documents/portfolio_macro_concentration.py` — should still be 50.0% AT CAP, exit=1.

Check `~/Documents/freshness_alarm.flag` and `~/Documents/macro_cap_alarm.flag` — both should be absent.

### Items the prior agent verified before going dark

- T6 + T7 daemons restarted with vegas-match fix on 2026-05-10 18:31 UTC. All TTY=??.
- Operator confirmed T6 first clean cycle in tomorrow-morning (May 11) check-in. **If that confirmation landed, copy the confirmed log snippet into the next handoff under "session 6 TL;DR".**
- SGO trial cleanup verified: no daemon, no LaunchAgent, no cron, no scheduled job references SGO. Only cleanup remaining is the May 17 dashboard cancel + key file removal.

### Watch-fors (likely traps for the Friday agent)

1. **T6 daily-cap leakage from contaminated count.** The May-10 contaminated fires counted toward the daily cap until midnight UTC. First clean cycle was post-2026-05-11 00:00 UTC. If somehow the cap counter persisted across day boundaries, T6 may not have fired Tuesday at all. Check ledger date distribution.

2. **T7 sentinel `[ALERT] title-parse-failed`.** If the sentinel fired, Kalshi changed event-title format on G1/G2 markets and Filter 14 regex needs updating. Investigate before any code change — the regex may need a one-line update to handle a new format.

3. **T3b and T3c reconcilers may have logged AlreadyClosedError** if the launchd respawn raced with the auto-close hourly tick. H13 idempotency should handle this gracefully but verify no real errors in the reconciler logs.

4. **VGK@ANA from session-4 probe** — that game (May 9) is long settled. Don't confuse with the current VGK/ANA series tickers in T7. The ticker date disambiguates.

5. **NHL Mammoth / Utah Hockey Club** — H3+H6 normalization handles both names but isn't live-tested until Utah re-appears in the feed (eliminated this round). Don't be alarmed if Utah-related tickers are absent.

6. **Macro cap is at 50.0%** — block any T3a/T3b/T3c bankroll increases; the cap only releases when one of the macro engines is retired or moved out of "macro" classification.

7. **Don't trust the milestone gate's RAW (audit) view.** Default view is the clean one; audit view (with `--include-contaminated`) preserves history but is not the validation gate. Decision-making uses default.

8. **SGO trial expiry creates `401 Unauthorized` if anything is still polling.** Confirmed nothing on the system polls SGO (no daemon, no LaunchAgent, no cron). If you see 401s in logs from SGO endpoints, find the rogue caller — that's a session-5 follow-up regression.

### Files to read before any new bet or engine deploy

```
~/Documents/HANDOFF_2026-05-10_session5.md       (this file)
~/Documents/sgo_d_decision_report.md             (SGO cancel rationale + caveats)
~/Documents/terminal6_mlb_paper_trader.py        (vegas-match fix in evaluate_market)
~/Documents/terminal7_paper_trader.py            (same fix, NBA/NHL)
~/Documents/terminal6_milestone_check.py         (contamination filter)
```

### Verification gates before any new engine deploy (extended from session 4)

1. Edge analysis lower-95 CI > 0 after fees on clean closes only
2. Bucket concentration check (no single bucket >40% of profit)
3. Entropy collapse calibration hit rate
4. Macro concentration cap (50% hard limit) — currently AT CAP, blocks new macro
5. T6 milestone gate not in EARLY_KILL or DEAD state on clean closes
6. T7 milestone gate not in ARCHITECTURE_FAIL or INSUFFICIENT_LIQUIDITY state
7. Adversarial review checkpoints cleared
8. **NEW (session 5):** vegas-match (or equivalent join) verification — for any engine joining Kalshi tickers to external sharp data, the join key MUST be disambiguated by the ticker's encoded date. Document the disambiguator in the spec.
9. **NEW (session 5):** review-checklist question "for every join key, what could match the wrong record? Show me the disambiguator." answered explicitly before sign-off.

Real-money path does not exist in any engine yet. Clean n=300 is the gate.

---

## How to resume in a new thread

Paste this prompt to the Friday agent:

> Read `~/Documents/HANDOFF_2026-05-10_session5.md` for full context. Resume per §11.
>
> Run `bash ~/Documents/portfolio_status.sh` and `python3 ~/Documents/terminal6_milestone_check.py` and `python3 ~/Documents/terminal7_milestone_check.py` to confirm portfolio state.
>
> Report (a) T6 vegas-match patch verification — clean closes accumulating, no contaminated stale-ticker fires (cross-check via `--include-contaminated`); (b) T3b CPI settlement May 13 outcome and realized P&L; (c) T3c ICSA settlement May 14 outcome and realized P&L; (d) T7 conf finals G1/G2 first fire audit — fire count, ticker-date narrowing held, distribution by sport/series; (e) SGO May 17 cleanup status (cancel + key file removal); (f) macro cap and any new alarms; (g) any new issues since session 5 closed.
>
> CRITICAL/HIGH go to Steve first; MEDIUM/LOW autonomous. Direct, no fluff.
