# Terminal Trading Portfolio — Handoff

_Generated 2026-05-09 (session 2). Hand to a fresh session to resume in context. Supersedes HANDOFF_2026-05-09.md._

---

## TL;DR — what changed today (session 2)

Long session. Six structural changes shipped, locked, and tested:

1. **Daemon FD inheritance bug fixed permanently.** `portfolio_scheduler.py` was zombified by terminal-close FD inheritance — every Python child died on `init_sys_streams: bad file descriptor`. Replaced the brittle `nohup + disown` pattern in `redeploy_t6_all.sh` and `redeploy_entropy_phase1.sh` with a macOS-native Python detacher (`os.fork → os.setsid → os.fork → fd redirect → os.execvp`). Every restarted daemon now runs with TTY=??, fully session-detached. **Closing the controlling terminal no longer kills any daemon.** Operational lesson #21 updated.

2. **T2 archived after CoWork diagnosis.** Root cause: `kalshi_thesis_factory.py` line 591 — `our_prob = implied_yes × CALFADE_PRIOR_MULT` is a price multiplier, not a prior. Edge formula reduces to `implied_yes × 0.5`, which produces fake "edges" linear in market confidence. The 94% backtest validation was tautological — it post-classified settled-NO markets as calendar_fade then computed NO rate. Scheduler job `t2_daily_picks` disabled, deploy artifacts wiped, `t2_archetype_calibration.blocked_archetypes()` confirmed blocking. 4 open T2 positions left to expire (do not close early). Bankroll → $0. Mode → archived.

3. **T6 bankroll doubled then re-incremented to $12K.** Absorbed T2's $5K, plus $2K from T1 sub-bucket capital recycle. Kelly cap 5% unchanged; max single position $600 ($600 = 5% × $12K). Total exposure cap $6K (50% × $12K).

4. **T1 restricted to 40-50¢ entry-bucket only.** Whole-engine T1 lifetime is statistically negative after fees (mean -$0.40, lower-95 -$0.84, n=311). Only the 40-50¢ entry bucket has positive lower-95 (+$0.18, n=24). Gate added at `terminal1_phase2_paper_trader.py` Filter 3. Bankroll dropped $5K → $1K. Freed $4K: $2K to T6 (above), $2K reserved in T7 slot.

5. **T6 Vegas matching — three bugs found and fixed (this was the big one).**
   - **Gate ordering:** Vegas freshness checked before game-start lead time, masking in-progress games as "stale." Swapped order. Truthful diagnostics restored.
   - **Athletics rebrand:** Odds API now returns "Athletics" (no city) post-2024 Oakland departure; team-name match was failing on every A's game. Added `_normalize()` in trader to handle both forms.
   - **MATCH-SOONEST bug (root cause of "0 trades fired" mystery):** team-name match loop returned the FIRST Vegas game with the same team-pair. MLB schedules same-team-pair series on consecutive days; the loop matched yesterday's game, lead_min went hugely negative (~-1100 min), every market for tonight was rejected as "in progress" — silently. Fix: collect all candidates within ±36h, prefer soonest upcoming. **This is what unblocked the engine — first 4 trades fired immediately after.**
   - Plus: rejection breakdown bucketed for sub-threshold delta, spread, and kalshi-p (each was splintering into 1-count categories hidden by top-10 truncation). Truncation cap removed entirely.

6. **Macro concentration cap shipped (50% hard limit).** `portfolio_macro_concentration.py` reads engines.json, computes deployed-capital macro share, returns exit-code-based gating for redeploy scripts. Wired into `portfolio_status.sh`. **Current state: 53.6% — over cap.** Resolves automatically when T7 deploys with $2K reserve, or via T3a resize. Steve's call.

7. **KL divergence telemetry implemented + persisted.** Per-cycle log line shows median/max |delta|, median/max KL, and would-trigger counts at KL≥0.001 / KL≥0.005. Near-miss observations (sub-threshold delta rejections) persist to `~/Documents/terminal6_data/near_miss_kl.jsonl` with full signal metadata. **First read (n=43): median KL 0.00005, max 0.00126, only 3 of 43 exceed KL=0.001, zero exceed 0.005 — the linear delta threshold is correctly placed; KL doesn't unlock meaningful additional signal at current near-miss distribution.** Re-evaluate at n=300 closes when distribution is richer. The Monday milestone task reads this file for empirical KL fitting.

8. **First T6 trades fired this session.** Cycle at 2026-05-09 17:35:42 UTC opened 4 new positions (16 accepted signals; 12 were duplicates of the existing 13 open). Validation clock now ticking. Trades:

   | Ticker | Side | Size | Entry | Delta | KL | f_full |
   |---|---|---|---|---|---|---|
   | KXMLBGAME-26MAY101335COLPHI-COL | YES | 500 | $0.340 | +0.051 | 0.0057 | 0.039 |
   | KXMLBGAME-26MAY101340HOUCIN-HOU | NO | 500 | $0.550 | -0.048 | 0.0047 | 0.060 |
   | KXMLBGAME-26MAY101605PITSF-PIT | YES | 60 | $0.490 | +0.040 | 0.0032 | 0.005 |
   | KXMLBGAME-26MAY101215WSHMIA-WSH | NO | 92 | $0.570 | -0.031 | 0.0019 | 0.009 |

Also: T6 weekly milestone check (`terminal6_milestone_check.py`) created with explicit early-kill criteria locked in code so the T2 "no-kill-criterion-meant-losses-compounded" failure mode cannot recur.

**One deferred decision:** `MAX_CONTRACTS = 500` is binding before the Kelly + 5% bankroll cap on high-edge trades (COL bet wanted ~688 contracts, capped at 500; HOU wanted ~654, capped at 500). At $12K bankroll the contract variance cap leaves Kelly-implied size on the table. Decide deliberately whether to raise to 1000 so the 5% bankroll cap becomes binding instead. Defer until n≥20 closes provide variance evidence.

---

## Decision register (locked this session)

### Build queue (locked) — FUTURE BUILDS SCHEDULED

In priority order. Prerequisites must ship before any new engine.

| # | Item | Type | Status | Earliest start |
|---|---|---|---|---|
| 0a | T1 sub-bucket gate + capital recycle | prerequisite | ✅ shipped 2026-05-09 | done |
| 0b | Macro concentration dashboard + cap | prerequisite | ✅ shipped 2026-05-09 | done |
| 1 | **T7 (NBA/NHL playoffs, Game 1-2 only)** | engine, $2K reserved | **NEXT — build now** | 2026-05-10 |
| 2 | T8 (NFP nowcast) | engine | gated on ADP-NFP residual validation | after T7 ships |
| 3 | T9 (crypto, funding-rate indicator) | engine | gated on correlation matrix | after T6 hits n≥100 |
| 4 | T10 (earnings) | engine | gated on Kalshi earnings volume_24h probe | likely permanent defer |
| 5 | **T11 (NFL game-winner)** | engine, conditional | **gated on T6 validation (n≥300, lower-95 ≥ 0)** | September 2026 |

**T7 build constraints (locked):**
- Game 1-2 only — independence assumption defensible at n=150 max this cycle.
- Series-state lookup required (Kalshi tickers don't expose series_state directly; need NBA/NHL official or ESPN API).
- Runway-constrained: NBA/NHL playoffs end mid-June. T7 confirms architecture transfer; T6 is the validator.
- $2K bankroll reserved from T1 sub-bucket recycle (engines.json T7 entry, mode=reserved).
- Repurpose slot in July if T6 validates and NFL prep is ready.

**T8 build constraints (locked):**
- Validate ADP-NFP historical residual distribution over 10-year window before deploying capital. Post-2020 ADP methodology change widened residuals materially. If σ on (NFP − ADP) exceeds Kalshi contract bin spacing, ADP is just another noisy nowcast and the engine collapses to T3b-with-extra-steps. Decision rule: if validation passes, T8 builds; if not, T8 archived before deployment.
- Counts toward macro cap.

**T9 build constraints (locked):**
- Indicator: perpetual swap funding rates from Binance/Bybit/OKX (NOT Deribit IV — that's volatility, not direction). Spot-perp basis as secondary signal.
- Correlation matrix from shadow-ledger residuals must ship first.
- Counts toward macro cap (inherits Fed-regime beta).

**T11 build constraints (locked):**
- Conditional on T6 validation. If T6 validates by All-Star break (mid-July), T11 build starts immediately for September camp opening. If T6 doesn't validate, T11 doesn't build and the entire sharp-sports vertical is buried.
- Architecture clone of T6 with NFL ticker filter + NFL Vegas feed.

**Removed from queue:** Kalshi-Polymarket sports arb (Polymarket is US-restricted; same wall as T4).

**Adversarial review process (new norm):** Backtests on new engines must go through second-model adversarial review before deploying capital, even shadow capital. CoWork builds, second model challenges, then capital. T2 post-mortem is the template — the 94% number looked rigorous, was tautological, no second pair of eyes caught it.

**Removed from queue:** Kalshi-Polymarket MLB arb. Polymarket is US-restricted (same wall as T4). Should not have been suggested.

**Conditional addition:** T11 (NFL) when T6 hits n≥300 with positive lower-95. T6 is the validator for the entire sharp-sports vertical — T7, T11 build only if T6 validates.

### T6 early-kill criteria (locked, encoded in code)

`terminal6_milestone_check.py` runs every Monday 9:09 AM PDT via the existing scheduled task `t6-shadow-trading-milestone-checks`. Gates:

- `n ≥ 200 and lower_95 < -$0.50` → **EARLY KILL** — archive T6, bury sports vertical, reallocate $12K. Don't wait for n=300.
- `n ≥ 300 and lower_95 ≥ 0` → **VALIDATED** — ship NFL (T11) build, queue T7 regular season for October, real-money decision unlocked.
- `n ≥ 300 and mean > 0 and lower_95 < 0` → **INCONCLUSIVE** — extend to n=500 for tighter CI.
- `n ≥ 300 and mean ≤ 0` → **DEAD** — archive T6.
- else → **ACCUMULATING** — no action.

Plus concentration flags: any delta-bucket >40% of P&L OR any single team >30% triggers a flag for review.

### T7 constraints (locked, will go in spec when built)

- Game 1-2 only (independence assumption defensible at n=150 max this cycle; series_state would need data we don't have).
- Series-state lookup required (Kalshi tickers don't expose series_state directly — need NBA/NHL official or ESPN API mapping).
- Runway-constrained: NBA/NHL playoffs end mid-June. T7 is architecture-confirmation, not validator. T6 validates.
- Repurpose slot in July if T6 validates and NFL prep is ready.

### T8 constraints (locked)

- Validate ADP-NFP historical residual distribution over 10-year window before deploying capital. Post-2020 ADP methodology change widened residuals. If σ on (NFP − ADP) exceeds Kalshi contract bin spacing, ADP is just another noisy nowcast and the engine collapses to T3b-with-extra-steps.

### T9 constraints (locked)

- **Indicator changed:** Deribit IV is the wrong signal (volatility, not direction). Use perpetual swap funding rates from Binance/Bybit/OKX as directional indicator. Spot-perp basis as secondary.
- Build correlation matrix from shadow-ledger residuals before deploying alongside the macro book.
- Counts as macro for concentration cap (inherits Fed-regime beta).

### Macro concentration cap

50% hard limit. Categories:
- **macro:** T3a, T3b, T3c, T8 (Fed/inflation/labor)
- **crypto:** T9 (inherits macro beta)
- **non-macro:** T1 (weather), T6/T7/T11 (sports), T10 (earnings, partial)

Current portfolio: **53.6% macro — OVER CAP.** Two paths to compliance: (a) wait for T7 deployment ($2K active sports → 50.0% exact), or (b) reduce T3a from $5K to $2.5K → 49%. Steve's call.

### Sports vertical thesis

T6 is the validator. T7 confirms architecture. T11 (NFL) builds on success. Single point of failure is T6 — if it doesn't validate, the entire sports book collapses.

T6 close-rate is now the most-watched metric in the portfolio. Currently 0 closed (13 open from initial deployment). At ~45 MLB games/day with trigger gates filtering to maybe 2-5 fires/day, n=300 ETA is mid-July to end of August. Get the trader firing cleanly is the gating activity.

### Adversarial review process (new norm)

Backtests on new engines must go through second-model adversarial review before deploying capital, even shadow capital. CoWork builds, second model challenges, then capital. T2 post-mortem is the template — the 94% number looked rigorous, was tautological, no second pair of eyes caught it.

---

## Strategic Context

- **Operator:** Steve Emery, Bend OR (Pacific Time). Direct, no fluff, one strong direction, structural advantage > tactical optimization.
- **Goal:** $500k profit, multi-year, multi-engine.
- **Validation discipline:** No engine goes to real money until n ≥ 300 closed with positive lower-95 CI on after-fee mean P&L. T6 has explicit early-kill at n=200 to prevent T2-style compounding.
- **Cadence-bound thesis:** macro engines (T3a/T3b/T3c/T8) max out at ~24 events/year combined; not a $500k path. Sports (T6/T7/T11) is the daily-cadence vertical that scales.
- **Filter for new engines:** independent leading indicator outside Kalshi AND costly enough that retail can't easily access it (T6's paid Vegas data is the model — moat = leading indicator + access cost).

---

## Portfolio P&L — Lifetime

| Engine | Open | Closed | Realized | Open CB | Mode | Notes |
|---|---|---|---|---|---|---|
| T1 (weather) | ~25-40 | 311 | -$121 after fees | varies | shadow_subset | restricted to 40-50¢ entry bucket only; $1K bankroll |
| T2 (catalyst) | 4 | 8 | -$161.48 | $155 | **archived** | thesis kill 2026-05-09; positions left to expire |
| T3a (Fed arb) | 0 | 0 | $0 | $0 | shadow | reconciler running, no opens; June 17 FOMC |
| T3b (CPI) | 2 | 0 | $0 | $11.28 | shadow | first event May 13 — auto-settles |
| T3c (Claims) | 1-2 | 5 | +$4.25 | varies | shadow | second print May 14 — auto-settles |
| T6 (MLB) | 13 | 0 | $0 | $67.20 | shadow | bankroll $12K; Kelly+entropy+fees active; thesis validator |
| T7 (NBA/NHL) | 0 | 0 | $0 | $0 | reserved | $2K reserve from T1 recycle; build pending |
| T5 (catalyst finder) | — | — | — | — | infrastructure | bankroll $0; T2 downstream archived |
| T4 (Polymarket arb) | — | — | — | — | archived | US-restricted |
| **TOTAL deployed** | | | **-$278.23** | **~$233** | | |

---

## Engine States

### T1 — Kalshi Weather Ensemble (RESTRICTED to 40-50¢ subset)

**Status:** running, gated to one validated subset. Bankroll $1K.

Gate: `terminal1_phase2_paper_trader.py` Filter 3. `T1_ENTRY_MIN = 0.40, T1_ENTRY_MAX = 0.50`. All other entries skipped with `[skip-bucket-gate]` log line.

Kill criterion: if 40-50¢ bucket lower-95 turns negative on next n=50 closes, T1 archives entirely. Monday weekly task `t1-edge-rerun-weekly` is the trigger.

Files unchanged: `terminal1_phase2_paper_trader.py` (gate added), `t1_edge_analysis.py` (the bucket analyzer).

### T2 — Kalshi Catalyst Book (ARCHIVED)

**Status:** archived 2026-05-09. Picks pipeline disabled in scheduler. Deploy artifacts deleted. Mode = archived. Bankroll = $0.

4 open positions left to expire over 60-90 days as deadlines pass:
- KXKASHOUT-26APR-JUN01 (Kash Patel out by Jun 1) — DTR 35d at open Apr 26
- KXLEAVEPOWELLGOV-26AUG01-JUN (Powell leaves Fed by Aug 1) — DTR 100d at open
- (2 others — verify against ledger)

Do not close early. Do not average down. Calendar_fade thesis is broken; the markets are right; expected outcome is full write-down to $0 over 60-90 days.

Deploy gate confirmed closed:
- `scheduler_jobs.json`: `t2_daily_picks` `enabled: false`
- LaunchAgent `com.t2.daily-picks.plist`: not loaded
- `terminal2_data/t2_deploy_*.py`: all deleted
- `t2_archetype_calibration.blocked_archetypes()`: returns `{calendar_fade, tail_probability}`

### T3a — Kalshi Fed Decisions

**Status:** scanner running, no opens, no validation runway. Next FOMC June 17-18.

Macro concentration candidate for resize: if you want to bring portfolio under 50% cap without waiting for T7, drop T3a from $5K to $2.5K. No operational impact (no positions).

### T3b — Kalshi CPI Nowcast (FIRST DECISION-GATE EVENT MAY 13)

**Status:** 2 NO positions open on KXCPIYOY-26APR ($11.28 CB, max profit +$12.72). Auto-settles via daemonized hourly reconciler (PID 73161, FDs verified clean during this session). Entropy gate active.

**No manual action required May 13.** First hourly tick after Kalshi finalizes ~14:00 UTC closes both positions automatically.

### T3c — Kalshi Initial Jobless Claims

**Status:** mode upgraded from `planning` → `shadow` (was already trading). 1 NO position open on KXJOBLESSCLAIMS-26MAY14 ($15.25 CB). First print +$4.25 (3W/2L, n=5). Entropy gate active.

Cowork-side scheduled task `t3c-icsa-reconcile-may14` fires May 14 07:30 PT to run reconciler `--once` (T3c reconciler is not yet daemonized; that's a future build).

### T6 — Kalshi MLB Game Markets (THESIS VALIDATOR)

**Status:** running, $12K bankroll, all daemons session-detached on TTY=??. **First trades fired this session.**

Today's structural changes:
1. **Bankroll $5K → $10K → $12K.** Absorbed T2 archive ($5K) + T1 sub-bucket recycle ($2K). Max single position cap by Kelly = $600 (5% × $12K); per-position contract cap MAX_CONTRACTS=500 currently binding before Kelly cap on high-edge trades — deferred decision whether to raise to 1000. Total exposure cap $6K (50%).
2. **WS feed live.** Latest PIDs (verify with `ps aux | grep terminal6`).
3. **Three Vegas matching bugs fixed** — gate ordering, Athletics rebrand, MATCH-SOONEST (this last one was the root cause of "0 trades fired" — same-team-pair series caused yesterday's game to be matched, lead_min went hugely negative, all upcoming games silently rejected).
4. **KL telemetry implemented** — per-cycle log + persistence to `terminal6_data/near_miss_kl.jsonl` for Monday milestone empirical fitting. First read confirms linear delta threshold is correctly placed.
5. **Early-kill criteria locked.** See `terminal6_milestone_check.py`. Run anytime: `python3 ~/Documents/terminal6_milestone_check.py`. Run weekly Mondays via `t6-shadow-trading-milestone-checks` Cowork task.
6. **First 4 trades fired** at 2026-05-09 17:35:42 UTC (after match-soonest fix landed). All on May 10 slate (May 9 evening tickers were already in the open book). See TL;DR table for entries.

Validation criterion: n=300 with positive lower-95 CI on after-fee P&L. Currently at n=0 closed (17 open as of trade fires). ETA mid-July to end of August at typical close rate. Watch close-rate weekly via milestone task.

**T6 is the linchpin.** T7, T11 build only if T6 validates. Single point of failure for the entire sharp-sports vertical.

Files of note:
- `terminal6_mlb_paper_trader.py` — Kelly + fees + entropy gate + 8 trigger gates + T1-style sub-threshold delta bucketing
- `terminal6_milestone_check.py` — weekly Monday milestone with explicit early-kill gates
- `terminal6_mlb_kalshi_ws.py` — WS logger
- `terminal6_mlb_lines_puller.py` — Vegas sharp consensus
- `terminal6_mlb_settlement_reconciler.py` — daemon

### T7 — Kalshi NBA/NHL Playoffs (RESERVED, build next)

**Status:** $2K bankroll reserved from T1 recycle. Mode = reserved. Active = false.

Build constraints locked (see Decision register above).

### T8 / T9 / T10 — queued

See build queue section.

### T5 — Catalyst finder (DORMANT)

**Status:** module still runs but downstream is archived. Bankroll $0. Decision pending whether to fully archive.

---

## Active Daemons & Schedules — verified 2026-05-09

### Scheduler (`portfolio_scheduler.py`)

PID currently 35781 (this session — restart with `setsid` Python detacher). TTY=??. Health verified. 5 jobs configured; t2_daily_picks now disabled.

| Job | Cadence | Status |
|---|---|---|
| t1_nws_actuals | 6h | enabled |
| t3c_claims_data | 12h | enabled |
| t2_daily_picks | 24h | **DISABLED 2026-05-09** |
| t6_mlb_lines_puller | 90min | enabled |
| entropy_collapse_detector | 5min | enabled |

### Long-running daemons (all TTY=?? after this session)

| Engine | PID (latest) | Daemon |
|---|---|---|
| T6 | 36344/36348/36352 | WS logger / paper trader / reconciler |
| T3b | 35587 | paper trader (entropy gate) |
| T3c | 35593 | paper trader (entropy gate) |
| T3b | 73161 | settlement reconciler (May 13 critical — FDs verified clean) |
| Scheduler | 35781 | portfolio_scheduler.py |

Older daemons (T1 logger/metar puller, T3a fed_scanner, T1 settlement reconciler etc.) — most on TTY=?? from previous sessions, surviving on inertia. T1 kalshi_logger (PID 30057) and metar_taf_puller (PID 29640) still on s004 — vulnerable to terminal close but T1 edge is near-zero so non-blocking. Future tidy-up.

### Status command

```
bash ~/Documents/portfolio_status.sh
```

Now includes: scheduler health, scheduled jobs, freshness watchdog, **macro concentration**, live daemons, LaunchAgents.

---

## Pending Actions (ordered)

### Immediate (operator-side)

- **Decide macro cap remediation:** T3a resize to $2.5K, OR wait for T7 build to take total to $30K and 50% exactly. No urgency since cap blocks new deployments, doesn't unwind existing.

### May 13 — April CPI release (T3b auto-settle)

- ~12:30 UTC: BLS releases.
- ~14:00 UTC: Kalshi finalizes. T3b reconciler auto-closes 2 NO positions.
- Cowork task `t3b-cpi-settlement-verify-may13` fires 09:00 PT to verify closure.

### May 14 — ICSA release (T3c reconcile)

- Cowork task `t3c-icsa-reconcile-may14` fires 07:30 PT, runs reconciler `--once`.

### Weekly milestones (already scheduled)

- Mondays 8:03 AM PDT: `t1-edge-rerun-weekly` — runs t1_edge_analysis.py against the 40-50¢ subset.
- Mondays 9:09 AM PDT: `t6-shadow-trading-milestone-checks` — runs `terminal6_milestone_check.py`. Reports gate verdict + concentration flags.
- Daily 8:09 AM PDT: `entropy-detector-calibration` — score watch alerts vs settlements; recommend Phase 2 enable at n≥15 with >65% hit rate.

### Build queue (when prerequisites compliant)

1. T7 (NBA/NHL Game 1-2 only) — first new engine. ~2-3 weeks build.
2. T8 (NFP) — gate on ADP-NFP residual validation.
3. T9 (crypto, funding-rate) — gate on correlation matrix.
4. T10 — verify volume first; likely defer.

---

## Critical Operational Lessons

### Net new from 2026-05-09 session 2

**23. Daemon detachment requires `setsid`, not just `nohup` + `disown`.** macOS Terminal close, when the user clicks "Terminate," sends signals (or kills the process group) that bypass `nohup`'s SIGHUP-ignore. `disown` removes the process from the shell's job table but doesn't change process group. Only `setsid` puts the process in its own session, where the controlling terminal cannot reach it. macOS doesn't ship `setsid`; the portable equivalent is a Python wrapper that does `os.fork → os.setsid → os.fork → fd redirect → os.execvp`. All redeploy scripts now use this pattern.

**24. Backtest validation MUST be walk-forward and out-of-sample.** T2's calendar_fade was "validated" at 94% NO rate by `terminal2_thesis_backtest.py`. The validation method was: classify already-settled markets into archetype buckets by pattern, then compute NO-settle rate within each bucket. That's selecting markets that ended NO and labeling them, then computing the NO rate. Tautological. Real validation requires: (a) hold out a test window, (b) apply the entry-criterion-at-time-of-evaluation, (c) measure outcomes only on simulated entries that the criterion would have triggered. Not "look at all settled markets and bucket them post-hoc."

**25. "Edge" formulas that reduce to `price × constant` are not models.** `kalshi_thesis_factory.py` line 591 set `our_prob = implied_yes × 0.5`, then computed `edge = implied_yes − our_prob = implied_yes × 0.5`. Edge is just half the market price. There is zero event-specific information. Higher market confidence produces bigger fake edges, which is exactly backwards. Any prior that doesn't incorporate event-specific data is not a prior; it's a relabeling.

**26. The T6 sharp-sports thesis is a single point of failure for the entire sports vertical.** T7 (NBA/NHL) and T11 (NFL) gate on T6 outcome. If T6 doesn't validate, the architecture is dead and downstream engines don't build. T6's close rate is therefore the most-watched metric in the portfolio.

**27. Filter for new engines: independent leading indicator + costly access.** Necessary AND sufficient. Free indicators (Deribit IV, public ADP) erode fast. T6 works because Vegas data is paid + 30-90 min ahead of Kalshi retail flow. Both moats. Apply both before queuing any engine.

**28. Hard concentration caps must enforce mechanically, not by judgment.** The 50% macro cap is a structural rule; manual judgment at allocation time is exactly the failure mode that lets engines drift. `portfolio_macro_concentration.py` returns exit codes; redeploy scripts must check and refuse breach.

**29. Every engine needs an explicit early-kill criterion in code BEFORE it deploys.** T2 went 8/8 because no early-kill criterion existed. T6 now has gates encoded in `terminal6_milestone_check.py`. Future engines must include analogous criteria as part of the build spec, not as an afterthought.

**30. When matching by team-pair across data sources, MLB schedules are series-based. Same teams play 3 consecutive days.** A naive "first match" loop returns the wrong game on series day 2/3. Always: (a) collect ALL candidates, (b) filter to a time window around now (e.g., ±36h), (c) prefer the soonest upcoming match. This bug masqueraded as "0 trades fired" for an entire session — every upcoming game was being silently mis-matched to yesterday's same-team-pair game and rejected as in-progress. The fix unblocked the trader immediately.

**31. Reject-reason strings that include numeric values splinter into single-count categories that get hidden by top-N truncation.** `f"spread {spread}c > 5c"` produces `spread 6c > 5c`, `spread 7c > 5c`, etc. — each its own bucket. With 20 markets each at unique values, all 20 hide below the top-10 cutoff. Rules: (a) reject reasons are CATEGORIES, not data points — never embed values, (b) put values in a separate aggregated telemetry line, (c) show all categories in the breakdown, no truncation. The 20-market silent-rejection mystery on 2026-05-09 took half a session to find because of this.

**32. Calibrate signal thresholds against the near-miss population, not just closed positions.** Near-miss markets (sub-threshold rejections that DID compute the signal) are a much larger sample than closed positions (the only feedback path most engines use). Persist near-miss telemetry per cycle. At n=43 near-misses, T6's KL distribution (max 0.00126) confirmed the linear 3pp delta threshold is correctly placed — empirical evidence available immediately rather than waiting for n=50 closes.

### Carried forward (still binding)

(See HANDOFF_2026-05-09.md for the full original lesson list 1-22. All still apply.)

---

## File Map (additions this session)

```
~/Documents/
  portfolio_macro_concentration.py    (NEW — concentration cap dashboard + enforcement)
  terminal6_milestone_check.py        (NEW — explicit early-kill criteria)
  HANDOFF_2026-05-09_session2.md      (this file)
  HANDOFF_2026-05-09.md               (prior; keep for audit)

  # Modified this session:
  terminal1_phase2_paper_trader.py    (T1_ENTRY_MIN/MAX gate added)
  terminal6_mlb_paper_trader.py       (gate ordering, Athletics norm, MATCH-SOONEST fix,
                                        delta/spread/kalshi-p bucket collapse, breakdown
                                        truncation removed, KL telemetry + near-miss
                                        persistence, $12K bankroll)
  redeploy_t6_all.sh                  (Python detach helper)
  redeploy_entropy_phase1.sh          (Python detach helper)
  shadow_pnl/engines.json             (T1 reduced, T2 archived, T6 $12K, T7 reserved,
                                        categories tagged, T3c upgraded planning→shadow)
  scheduler_jobs.json                 (t2_daily_picks disabled)
  portfolio_status.sh                 (concentration section added)

  # Live data files (auto-generated, just log them for awareness):
  terminal6_data/near_miss_kl.jsonl   (NEW — per-cycle near-miss telemetry, accumulating;
                                        Monday milestone reads for empirical KL fitting)
  terminal6_data/milestone_latest.txt (NEW — one-line headline from milestone_check.py)
```

---

## How to resume in a new thread

Paste this prompt:

> Read `~/Documents/HANDOFF_2026-05-09_session2.md` for full context. Run `bash ~/Documents/portfolio_status.sh` and `python3 ~/Documents/terminal6_milestone_check.py`. Report (a) any daemon health issues, (b) T6 close progress toward n=300 with current gate verdict, (c) macro concentration cap status (still over 50%? did T7 deploy or T3a get resized?), (d) any pending settlement actions for May 13 (T3b CPI) or May 14 (T3c Claims), (e) status of build queue items 1-5 — has T7 build started? has ADP-NFP residual validation been done for T8? (f) what you recommend next. Direct, no fluff.

### Verification before any new bet

1. Edge analysis lower-95 CI > 0 after fees
2. Bucket concentration check (no single bucket >40% of profit)
3. Entropy collapse calibration hit rate (Phase 2 enabled = full directional logic available)
4. **NEW:** Macro concentration cap (50% hard limit) compliant — `python3 portfolio_macro_concentration.py`
5. **NEW:** T6 milestone gate not in EARLY_KILL or DEAD state — `python3 terminal6_milestone_check.py`

Real-money path does not exist in any engine yet.
