# Terminal Trading Portfolio — Handoff

_Generated 2026-05-09 (session 3). Hand to a fresh session to resume in context. Supersedes HANDOFF_2026-05-09_session2.md._

_The next session's primary build is **T7 (NBA/NHL playoffs, Game 1-2 only)**. T7 is a structural clone of T6 with three deltas. Spec already locked at `~/Documents/terminal7_nba_nhl_spec.md`. Direction at §11 of this doc._

---

## TL;DR — what changed in session 3

Operator-led session focused on (a) full code audit of all open engines, (b) ADP-NFP residual study (T8 gate), (c) T7 spec lock, and (d) execution of approved CRITICAL/HIGH fixes with sign-off gates.

**Audit dispatched 7 parallel reviewers** across T1, T3a, T3b, T3c, T6, shared infra, and T2 seal verification. Steve flagged three "fresh-agent-might-miss" risks before action — Kelly formula even-money assumption, KL telemetry being parallel not replacement, architectural decisions vs flagged bugs. All three checked directly against code, all three cleared. Three highest-risk items also re-verified directly: T3b May 13 path, T2/T5 lineage, T6 gate ordering fix. Full audit at `~/Documents/AUDIT_2026-05-09.md`.

**Fixes shipped this session (with rationale):**

1. **T3b settlement reconciler now under launchd.** Was the only CRITICAL — reconciler was launched manually via nohup, vulnerable to Mac sleep/reboot before May 13. New plist `com.t3b.settlement-reconciler.plist` has `KeepAlive` (auto-respawn), `RunAtLoad` (startup gap closer), `ThrottleInterval=30` (no hot-loop on persistent failure). PID 40443 verified live under `launchctl list`.

2. **T3c reconciler idempotent on double-fire (H13).** `ShadowLedger.close()` raises `ValueError` on already-closed positions; outer `except Exception` was catching but aborting the rest of `reconcile_once`. Wrapped specifically in `try/except ValueError` that logs `[skip-already-closed]` and `continue`s. Critical for May 14 — Cowork scheduled task running `--once` could fire twice (manual run + scheduled run, daemon overlap, retry). Now safe. `raise` re-raises non-"already closed" `ValueError`s — fail-loud on unknown errors, fail-quiet only on the expected idempotency case.

3. **T6 WS subscription refresh ACTUALLY reconnects now (H18).** Audit caught that `subs_refresh_loop` was defined-but-unused; the real main-loop refresh detected ticker changes but only logged "next disconnect/reconnect cycle" — could be hours. Implementation: `_ACTIVE_WS` global populated by `ws_run_forever`, `_FORCE_RECONNECT` flag resets backoff to 1s on operator-triggered reconnect, main loop calls `_ACTIVE_WS.close()` on subs change. Validated via dedicated `--refresh-test` flag (subscribe → wait 60s → drop one ticker → force reconnect → wait 60s → check delta in subs_acks). Phase 2 delta=+2 (orderbook_delta + trade re-acked). VERDICT: PASS. Then live-redeployed (PIDs 40696/40700/40704). **This was a silent edge-killer**: new MLB games were invisible to the trader for hours after creation, slowing every n=300 close-rate ETA. Fixed.

4. **T2 reactivation surface killed (H19).** Three paths could re-enable T2 picks beyond the disabled scheduler job:
   - `~/Library/LaunchAgents/com.t2.daily-picks.plist` (deleted)
   - `~/Documents/com.t2.daily-picks.plist` (deleted)
   - `~/Documents/install_t2_daily_picks.sh` (deleted, would launchctl-load the plist)
   - `~/Documents/deploy_t2_tail_batch.py` (deleted, would open 5 tail_probability shadow positions)
   Verified `launchctl list | grep t2` returns empty before deletion. Operator-side action because Cowork sandbox cannot delete user files (Operation not permitted). Only `t2_archetype_calibration.py` remains — benign calibration helper, not a trading driver, not a re-enable route.

5. **T1 bucket gate boundary fixed (H1).** Off-by-one: gate was `entry > T1_ENTRY_MAX` (allowed 0.50). Validated bucket from `t1_edge_analysis.py` is half-open `[0.40, 0.50)` — entry==0.50 was classified into the "50-60c" decile during the +$0.18 lower-95 study. Trader was leaking out-of-sample trades at exactly $0.50. Fix: `>` → `>=`. One char + comment block explaining the half-open semantic.

6. **T1 `already_open_on_ticker` honors annul_close (H3).** Prior version popped on close without checking later annul_close events; duplicate opens were possible after any reconciler annul. Rewrote to mirror `shadow_pnl_core.compute_state` canonical pattern: track open records, close events, and annulled_close_ts; resolve at end with the half-open semantic.

7. **T1 reconciler T-strike fail-closed (H4).** Ambiguous T-suffix tickers (e.g., `T42` could be `>42°` or `<42°`) were silently defaulting to threshold_above. A wrong guess flips win→loss on settle. Now returns `None` (manual reconciliation required for legacy entries that pre-date strike_display in signal_metadata).

8. **T1 DEFAULT_CONTRACTS comment block (H2).** No code change. Documented that `--contracts` argv is the bankroll lever for T1; raising `DEFAULT_CONTRACTS` requires also adding a real exposure check. Notes the ~$25-50/fire and ~$300 peak open exposure analysis at $0.40-0.50 entry × 5 fires/day at the locked sizing. Inside $1K bankroll comfortably.

9. **T6 dashboard Athletics drift fixed (H17).** `terminal6_dashboard.py:66` still mapped OAK/ATH → "Oakland Athletics". Trader had been fixed earlier in session 2 to map both to bare "Athletics" (matches Odds API post-2024 rebrand). Dashboard kept showing phantom "no vegas/kalshi join" on Athletics games. Trader fired correctly throughout — operator-visible inconsistency only.

10. **T3b reconciler docstring states paper-only design intent (H10).** Audit flagged that reconciler settles on BLS row presence alone — never queries Kalshi `expiration_value`/`result`/`status`. This is INTENTIONAL for paper Phase 1 (calibration on BLS truth). Added explicit docstring header: paper-only convention, go-live gate is `kalshi_market.status == 'settled' AND result IN {'yes','no'}`. Now the next audit won't re-flag it.

11. **Scheduler per-job exception fence (H22).** `portfolio_scheduler.py` had `run_job()` internal try/except (good) but the per-job state mutation, `save_state()`, and logging were unprotected. A single ENOSPC or corrupt state write could kill the supervisor, taking down all pullers. Wrapped the entire per-job body in try/except; logs `[fatal-protect] job=X raised Y` and continues.

12. **`portfolio_status.sh` captures macro cap exit code (H21).** Was piping cap output through `tail` — `$?` returned tail's exit, never the cap script's. Now uses `${PIPESTATUS[0]}`, prints "MACRO CAP BREACHED — exit=N" when ≥2, writes/clears `macro_cap_alarm.flag` (analogous to `freshness_alarm.flag`).

13. **Redeploy scripts pre-flight macro cap + freshness flag (H20). [HAS A BUG — see §6]** Intent was to refuse redeploy when cap breached or freshness alarm active. The freshness check works correctly. The macro cap check has a `EC=$?` inside an `if ! cmd` body which captures the inverted value (always 0). Functionally a no-op right now. Steve approved running the live T6 redeploy anyway because (a) T6 is non-macro and shouldn't be blocked by a macro cap breach to begin with, and (b) the H-T6-2 fix needed to land. Bug + design rework queued post-May-14.

14. **May 14 07:15 PT freshness pre-check scheduled (H14).** Cowork scheduled task `t3c-icsa-freshness-precheck-may14` fires 15 min before the 07:30 PT T3c reconciler. Verifies ICSA print is in `~/Documents/terminal3c_data/icsa_history.jsonl` and cross-checks against FRED. Reports GREEN/YELLOW/RED to the session. If YELLOW or RED, holds reconciler — operator decides next step. This is the operator-side answer to H14 (no retry logic in `--once` reconciler).

**ADP-NFP residual study — T8 ARCHIVED pre-deployment.**

Decision rule (per session 2 handoff): if σ on (NFP − ADP) exceeds Kalshi NFP bin spacing, archive. Stronger result obtained:
- σ stanford era (2023-now, n=40): **136K**
- σ legacy clean window (2014-2019, n=72): **120K**
- Kalshi bin spacing 25K-50K → ratio 2.7–5.4×, FAIL the σ gate
- **Bigger finding**: ρ(NFP, ADP) = 0.20 legacy, 0.34 Stanford. Variance explained by ADP is *negative* — subtracting ADP from NFP makes residual MORE volatile than NFP alone. ADP+NFP behave largely as independent random variables; ADP's noise dominates whatever signal it carries.
- Even with optimal regression conditioning, ADP shaves only 4-6% of NFP variance.
- 3 of last 12 months had OPPOSITE signs between NFP and ADP.

ADP isn't a leading indicator. T8 architecture is fine; the data is the problem. Failed both audit filters: ADP is publicly free (no access cost moat) AND not a meaningful leading indicator. Same archetype-failure as Deribit IV from prior lessons.

Build queue position #2 is now open. Replacement candidates queued in `T8_VERDICT_2026-05-09.md`:
1. JOLTS + Indeed Hiring Lab — paid Indeed data has access-cost moat
2. PMI Employment Index (S&P Global, ISM) — free, weak edge
3. High-frequency wage data (Homebase, Paychex) — leads private payrolls 2-4 weeks
4. Truflation/state-level inflation panels — T3b extension

Don't promote T9 — its correlation matrix prereq still hasn't shipped.

**T7 spec locked at `~/Documents/terminal7_nba_nhl_spec.md`.**

**Material discovery during spec drafting:** Kalshi event titles for KXNBAGAME / KXNHLGAME are formatted `"Game N: Away at Home"`. Game number is right there. The session 2 handoff's assumption that "Kalshi tickers don't expose series_state directly; need NBA/NHL official or ESPN API" was wrong (or has changed). One regex on event title replaces an entire external API integration — ~5 LOC for the G1-G2 gate. This simplifies T7 substantially.

**Runway reality check (probed Kalshi 2026-05-09):**
- Round 1 G1-G2: past
- Round 2 G1-G2: past  
- Conf Finals G1-G2: ~May 15-25, ~4 series × 2 games = 8 games
- Finals G1-G2: ~June 2-10, 2 series × 2 games = 4 games
- **Max addressable G1/G2 universe ≈ 12 games combined NBA+NHL through finals**
- After delta filter, spread/liquidity gates, entropy gate: expect 6-10 fires
- Frame T7 as architecture-confirmation only, not edge validation

**Liquidity warning:** at probe time, all 14 open KXNBAGAME markets had `volume_24h=0` and `open_interest=0`. INSUFFICIENT_LIQUIDITY is a foreseeable end-state. Spec acknowledges this with explicit verdict.

---

## Decision register (locked this session)

### Build queue (updated)

| # | Item | Type | Status | Earliest start |
|---|---|---|---|---|
| 0a | T1 sub-bucket gate + capital recycle | prerequisite | ✅ shipped session 2 | done |
| 0b | Macro concentration dashboard + cap | prerequisite | ✅ shipped session 2 (gate has bug — §6) | done |
| 1 | **T7 (NBA/NHL playoffs, Game 1-2 only)** | engine, $2K reserved | **NEXT — spec locked, build now** | 2026-05-10 |
| 2 | ~~T8 (NFP nowcast)~~ | engine | **ARCHIVED 2026-05-09** — see T8_VERDICT_2026-05-09.md | n/a |
| 2-replacement | TBD: JOLTS+Indeed / PMI / HFW data / Truflation | engine | gated on residual study for chosen indicator | post-T7 |
| 3 | T9 (crypto, funding-rate indicator) | engine | gated on correlation matrix | after T6 hits n≥100 |
| 4 | T10 (earnings) | engine | gated on Kalshi earnings volume_24h probe | likely permanent defer |
| 5 | **T11 (NFL game-winner)** | engine, conditional | **gated on T6 validation (n≥300, lower-95 ≥ 0)** | September 2026 |

### T6 early-kill criteria (unchanged, locked in code)

`terminal6_milestone_check.py`, runs Mondays 9:09 AM PDT via `t6-shadow-trading-milestone-checks`:
- `n ≥ 200 and lower_95 < -$0.50` → EARLY KILL
- `n ≥ 300 and lower_95 ≥ 0` → VALIDATED
- `n ≥ 300 and mean > 0 and lower_95 < 0` → INCONCLUSIVE (extend to n=500)
- `n ≥ 300 and mean ≤ 0` → DEAD
- else → ACCUMULATING

### T7 build constraints (locked, see spec for full)

- Game 1-2 only — independence assumption defensible at small n
- Series-state lookup via ticker title regex (NEW finding — replaces ESPN API)
- Per-series exposure cap: 2 max (G1 + G2)
- Bankroll $2K, Kelly 5% cap = $100/position, total exposure 50% = $1K
- Runway-constrained: ~12 G1/G2 games this cycle, expect 6-10 fires, NOT enough for n=300
- **Architecture-confirmation only** — T6 still validates the sharp-sports thesis
- Adversarial review checkpoint mandatory before shadow capital deploys
- **Open decision: Odds API tier upgrade ($30/mo Plus, 100K calls/mo).** T6 alone is over the 500/mo free tier (M-T6-4 from audit). Adding T7 makes this acute. Steve has not signed off on this yet — it's an explicit open question in the spec at §9.

### Macro concentration cap

Still **53.6% — OVER CAP.** Two paths to compliance: T7 deploys $2K active sports → 50.0% exact, or T3a resize. Steve nixed T3a resize as "meaningless" (engine has no open positions; moving inactive bankroll between engines is bookkeeping theater). T7 deployment is the resolution path.

**Macro cap preflight gate in redeploy scripts has a bash bug (see §6).** Functionally a no-op. Doesn't block anything right now. Steve approved running T6 redeploy anyway because T6 is non-macro and shouldn't be blocked by a macro breach to begin with. Bug + design rework queued post-May-14.

### Adversarial review process (norm continues)

Backtests on new engines must go through second-model adversarial review before deploying capital, even shadow capital. T2 post-mortem is the template. T7 spec specifies three checkpoints: after spec lock (BEFORE code), after backtest (BEFORE shadow capital), after first 5 closed positions.

---

## Strategic context

- **Operator**: Steve Emery, Bend OR (Pacific Time). Direct, no fluff, one strong direction, structural advantage > tactical optimization.
- **Operating pattern this session**: explicit sign-off gates on CRITICAL/HIGH fixes; "show me the diff before commit" on May 13/14 critical-path changes; "one terminal prompt at a time" on operator-side commands; bulk-approve M/L; defer items that touch live execution paths between now and May 14.
- **Goal**: $500k profit, multi-year, multi-engine.
- **Validation discipline**: No engine goes to real money until n ≥ 300 closed with positive lower-95 CI on after-fee mean P&L. T6 has explicit early-kill at n=200.
- **Cadence-bound thesis**: macro engines (T3a/T3b/T3c) max out at ~24 events/year combined; not a $500k path. Sports (T6/T7/T11) is the daily-cadence vertical that scales.
- **Filter for new engines**: independent leading indicator outside Kalshi AND costly enough that retail can't easily access it. T8 failed both filters. Use this filter to evaluate the queue replacement.

---

## Portfolio P&L — Lifetime

| Engine | Open | Closed | Realized | Open CB | Mode | Notes |
|---|---|---|---|---|---|---|
| T1 (weather) | varies | 311+ | -$121 after fees | varies | shadow_subset | restricted to 40-50¢ entry bucket only; $1K bankroll; gate boundary fixed this session |
| T2 (catalyst) | 4 | 8 | -$161.48 | $155 | **archived** | thesis kill 2026-05-09 (session 2); positions left to expire; reactivation surface killed this session |
| T3a (Fed arb) | 0 | 0 | $0 | $0 | shadow | scanner running, no opens; June 17 FOMC; 5 HIGHs deferred to pre-FOMC pass |
| T3b (CPI) | 2 | 0 | $0 | $11.28 | shadow | first event May 13 — auto-settles; reconciler now under launchd |
| T3c (Claims) | 1 | 5 | +$4.25 | $15.25 | shadow | second print May 14 — auto-settles via scheduled task; idempotent on double-fire as of this session |
| T6 (MLB) | 17 | 0 | $0 | $363+ | shadow | bankroll $12K; H-T6-2 fix landed live; new-game discovery now within 30-min refresh tick |
| T7 (NBA/NHL) | 0 | 0 | $0 | $0 | reserved | $2K reserve; **build next**; spec at terminal7_nba_nhl_spec.md |
| T5 (catalyst finder) | — | — | — | — | infrastructure | bankroll $0; T2 downstream archived |
| T4 (Polymarket arb) | — | — | — | — | archived | US-restricted |

T6 trades fired session 2 (4 trades on 2026-05-10 slate) are still pre-settlement. Validation clock ticks.

---

## Engine States

### T1 — Kalshi Weather Ensemble (RESTRICTED to 40-50¢ subset)

**Status:** running, gated to validated subset. Bankroll $1K.

**Changes this session:**
- Bucket gate boundary `>` → `>=` at line 468 of `terminal1_phase2_paper_trader.py`. Now correctly enforces half-open `[0.40, 0.50)` to match `t1_edge_analysis.py` decile classification. Trades at exactly $0.50 now reject with `[skip-bucket-gate]`.
- `already_open_on_ticker` rewritten to mirror `shadow_pnl_core.compute_state` canonical annul_close handling. Tracks open records, close events, annulled_close_ts; resolves at end. Prevents duplicate opens after any reconciler annul.
- T-strike fail-closed in `terminal1_settlement_reconciler.py`. Ambiguous T-suffix returns `None` instead of guessing threshold_above.
- DEFAULT_CONTRACTS comment block added — documents `--contracts` argv as the bankroll lever; raising DEFAULT_CONTRACTS requires real exposure check.

**Remaining HIGHs (deferred):**
- None. All T1 HIGHs from audit shipped this session.

**Kill criterion:** if 40-50¢ bucket lower-95 turns negative on next n=50 closes, T1 archives entirely. Monday weekly task `t1-edge-rerun-weekly` is the trigger.

### T2 — Kalshi Catalyst Book (ARCHIVED)

**Status:** archived 2026-05-09 (session 2). Picks pipeline disabled. Bankroll $0.

**Changes this session:**
- LaunchAgent plist removed from `~/Library/LaunchAgents/`
- `~/Documents/com.t2.daily-picks.plist` removed
- `~/Documents/install_t2_daily_picks.sh` removed
- `~/Documents/deploy_t2_tail_batch.py` removed
- `launchctl list | grep t2` returns empty — verified

4 open positions remain to expire over 60-90 days as deadlines pass. Settlement reconciler still runs for them; correctly passive (won't auto-close early or re-open).

`t2_archetype_calibration.py` remains on disk — benign calibration helper imported only by the disabled `terminal2_daily_t5_picks.py`. Not a re-enable route. Cal file is stale (last write 2026-05-05, calendar_fade still shows n=2 below blocking threshold) but pipeline is gated by scheduler so this is cosmetic.

`kalshi_thesis_factory.py:591` still has the broken `our_prob = implied_yes × 0.5` formula. No active engine imports the module. T5 is bankroll=0 mode=infrastructure. No DEPRECATED marker added (Steve did not approve H23 in bulk approve list — explicit subset). Module is silent dead code.

### T3a — Kalshi Fed Decisions

**Status:** scanner running, no opens, no validation runway. Next FOMC June 17-18.

**Changes this session:** none.

**Deferred HIGHs (pre-FOMC pass):**
- H5: `kalshi_fee` rounding wrong (rounds half-up vs Kalshi's ceil) — causes EV-after-fee to clear `min-profit-usd` when reality wouldn't
- H6: `snapshot_event_legs` silently drops legs with no orderbook → `evaluate_arb` proceeds on partial event
- H7: FOMC observer aggregates `sum_ask` substituting 100¢ for missing legs → fake dislocations
- H8: `state["processed"]` permanently sticky → flapping arb only evaluates first crossing
- H9: Refuses arb when leg size < `contracts_per_leg` instead of resizing

T3a doesn't trade. None of these are bleeding capital. Dedicated hardening pass scheduled pre-June 17 FOMC.

### T3b — Kalshi CPI Nowcast (FIRST DECISION-GATE EVENT MAY 13)

**Status:** running, 2 NO positions open on KXCPIYOY-26APR (T3.6, T3.7), $11.28 CB, max profit +$12.72.

**Changes this session:**
- Settlement reconciler now under launchd (`com.t3b.settlement-reconciler.plist`). PID 40443 verified. KeepAlive respawn on crash, RunAtLoad startup gap closer, ThrottleInterval=30 prevents hot-loop on persistent failure.
- Docstring header documents PHASE 1 PAPER design intent — settlement triggers on BLS row presence not Kalshi `expiration_value`. Go-live gate explicitly stated.

**No manual action May 13.** First hourly tick after Kalshi finalizes ~14:00 UTC closes both positions automatically.

**Settlement math (verified):** `determine_yes_outcome` returns `actual > strike` (strict). T3.6 wins NO if April CPI rounds ≤ 3.6. T3.7 wins NO if rounds ≤ 3.7. If 3.7 exactly, T3.6 loses and T3.7 wins.

**Deferred HIGH (post-May 13):**
- H11: `find_open_t3b_positions` annul-close ordering bug. Inert today (no annulments on the 2 open positions). Fix after May 13 settles.

### T3c — Kalshi Initial Jobless Claims

**Status:** running, 1 NO position open on KXJOBLESSCLAIMS-26MAY14 ($15.25 CB). Mode=shadow.

**Changes this session:**
- Reconciler `sl.close()` wrapped in `try/except ValueError` for "already closed" case. Idempotent on double-fire. Other positions in the pass continue if one is already-closed; per-event rollup writes correctly. `[skip-already-closed]` log line on hit.
- Cowork scheduled task `t3c-icsa-freshness-precheck-may14` armed for 2026-05-14 07:15 PT (15 min before reconciler runs at 07:30 PT). Verifies ICSA print is in `icsa_history.jsonl` AND cross-checks against FRED. Returns GREEN/YELLOW/RED. If YELLOW or RED, surfaces verdict but does not run reconciler — operator decides.

**Deferred HIGH (post-May 14):**
- H15: same annul-close ordering bug as H11. Inert today. Fix after May 14 settles.

### T6 — Kalshi MLB Game Markets (THESIS VALIDATOR)

**Status:** running, $12K bankroll. Daemons redeployed this session with H-T6-2 fix. PIDs 40696 (ws), 40700 (paper trader), 40704 (reconciler), all TTY=??.

**Changes this session:**
- **WS subscription refresh now actually reconnects** (H-T6-2). `_ACTIVE_WS` global populated by `ws_run_forever`, `_FORCE_RECONNECT` flag resets backoff to 1s, main loop calls `_ACTIVE_WS.close()` on subs change. Outer ws_run_forever loop reconnects within ~2s with new SUBSCRIBED_TICKERS via `on_open`.
- New `--refresh-test` flag for dry-run validation. Subscribes, waits 60s, drops one ticker, forces reconnect, waits 60s, exits with PASS/FAIL based on subs_acks delta. Validated PASS this session before live redeploy.
- Dashboard team-name normalization: OAK and ATH map to bare "Athletics" (mirrors trader's `_normalize()` after the 2024 rebrand).

**Verification post-redeploy:** ws_logger writing 42 books, ~50 deltas/sec, writes_total +42 every 5s. Live data flowing.

**Why this fix mattered:** the prior code commented "we accept that new markets show up after the next disconnect/reconnect cycle, which happens at most every ~30-60 min during normal operation." In practice, stable connections last hours. New MLB games created during the day were invisible to the trader until a natural disconnect. With ~45 MLB games/day and trigger gates filtering to 2-5 fires/day, this was the binding constraint on close-rate ETA. n=300 should now arrive faster than session 2's mid-July to end-August projection.

**Audit findings — what's clean:**
- Three Vegas matching fixes from session 2 verified in code at lines 311-321 (lead-time before freshness), 272-275 (Athletics normalize), 282-307 (MATCH-SOONEST window collection)
- Kelly formula at lines 427-452 IS binary-Kalshi-aware (effective_stake = p+fee, net_payout = 1-p-fee, b = net_payout/effective_stake), NOT even-money
- KL telemetry is parallel observation, NOT replacing the linear delta gate (line 371 still gates at 0.03)
- 8 trigger gates verified active, no phantom gates
- Milestone gates exactly match handoff
- No T2-style tautological priors

**Deferred MEDIUMs (post-May-14 cleanup):**
- M-T6-1: MAX_CONTRACTS=500 cap silent override — add `[size-cap]` log line
- M-T6-2: O(N) ledger re-read per signal in `already_open_on_event` — cache once per cycle
- M-T6-4: Free-tier Odds API budget overrun — paid tier decision
- M-T6-5: `near_miss_kl.jsonl` no rotation — daily rotation
- M-T6-6: `parse_kalshi_event_teams` brittle on 5+ letter prefixes — currently safe

### T7 — Kalshi NBA/NHL Playoffs (BUILD NEXT, SPEC LOCKED)

**Status:** $2K bankroll reserved. Mode=reserved. Active=false.

**Spec locked at:** `~/Documents/terminal7_nba_nhl_spec.md`

**See §11 of this handoff for build directions to the next session.**

### T8 — Kalshi NFP nowcast (ARCHIVED)

**Status:** archived 2026-05-09 pre-deployment. ADP-NFP residual study verdict: ρ < 0.35, variance explained < 6% even with optimal regression conditioning. ADP not a leading indicator for NFP. Architecture is fine; data is the problem.

**Files:** `~/Documents/T8_VERDICT_2026-05-09.md`, `~/Documents/adp_nfp_residual_study.py`, `~/Documents/adp_nfp_extended_analysis.py`

**Engine slot freed.** Replacement candidates listed in T8_VERDICT and Build Queue Position 2.

### T5 — Catalyst finder (DORMANT)

**Status:** module still runs but downstream is archived. Bankroll $0. No active consumer of `kalshi_thesis_factory.py` output.

---

## Audit findings — full disposition

CRITICAL (1):
- ✅ C1: T3b reconciler under launchd

HIGH — fixed this session (10):
- ✅ H1: T1 boundary fix
- ✅ H2: T1 contracts comment
- ✅ H3: T1 annul handling
- ✅ H4: T1 T-strike fail-closed
- ✅ H10: T3b paper-only docstring
- ✅ H13: T3c idempotency
- ✅ H17: T6 dashboard Athletics
- ✅ H18: T6 WS refresh + dry-run validation + live redeploy
- ✅ H19: T2 reactivation surface killed (operator-side)
- ✅ H21: status.sh exit code capture
- ✅ H22: scheduler per-job try/except

HIGH — partially fixed (1):
- ⚠️ H20: redeploy preflight macro cap gate. Code shipped but has bash bug (EC=$? captures inverted value inside `if !` body — always 0). Functionally a no-op. Plus design issue: blocking ALL redeploys on macro cap breach is wrong — should only block category=macro deployments. Bug + redesign queued post-May-14.

HIGH — deferred (per Steve's direction):
- H5-H9: 5 T3a HIGHs — pre-June 17 FOMC dedicated hardening pass
- H11: T3b annul-close ordering — fix after May 13 settles
- H14: T3c reconciler retry logic — operator-side answer (scheduled freshness pre-check)
- H15: T3c annul-close ordering — fix after May 14 settles

HIGH — not approved (1):
- H23: DEPRECATED markers on broken modules (`terminal2_thesis_backtest.py`, `kalshi_thesis_factory.py:591`). Steve did not approve in bulk approve list. Modules silently dead-code; no consumer. Cosmetic.

MEDIUM and LOW: tracked in `AUDIT_2026-05-09.md` for opportunistic cleanup pass.

---

## Bug-fix queue (post-May-14)

1. **`AlreadyClosedError` class in `shadow_pnl_core.py`.** Replaces string-match fragility (`"already closed" in str(e).lower()`) across all reconcilers (T3c done this session, T3b deferred, future engines). 10-minute refactor. Steve flagged this as low-priority hardening.

2. **Macro cap preflight gate redesign:**
   - Fix bash bug: capture exit code OUTSIDE `if !` body
   - Fix design: only block deployments where category=macro, not all redeploys
   - Add per-engine flag in redeploy scripts: `IS_MACRO=true` → check cap; otherwise skip

3. **T3b annul-close ordering** (H11): mirror canonical `compute_state` from `shadow_pnl_core`.

4. **T3c annul-close ordering** (H15): same fix.

5. **T3a hardening pass** (H5-H9 from audit): fee rounding, partial-leg arb, sticky processed state, auto-resize, FOMC observer dislocation gate.

---

## Active Daemons & Schedules — verified post-redeploy

### Scheduler (`portfolio_scheduler.py`)

PID changes per session. TTY=??. 5 jobs configured; t2_daily_picks disabled.

| Job | Cadence | Status |
|---|---|---|
| t1_nws_actuals | 6h | enabled |
| t3c_claims_data | 12h | enabled |
| t2_daily_picks | 24h | DISABLED |
| t6_mlb_lines_puller | 90min | enabled |
| entropy_collapse_detector | 5min | enabled |

Scheduler now has per-job try/except fence — single bad job no longer kills supervisor.

### Long-running daemons (verified TTY=?? this session)

| Engine | PID (this session) | Daemon | Notes |
|---|---|---|---|
| T6 ws_logger | 40696 | `terminal6_mlb_kalshi_ws.py` | H-T6-2 fix live |
| T6 paper trader | 40700 | `terminal6_mlb_paper_trader.py --live --interval-sec 1800` | |
| T6 reconciler | 40704 | `terminal6_mlb_settlement_reconciler.py --interval-sec 3600` | |
| T3b reconciler | 40443 (launchd) | `com.t3b.settlement-reconciler` | KeepAlive respawn |

T3b/T3c paper traders, T1 daemons, T3a fed_scanner — older PIDs from session 2, surviving on inertia. All TTY=?? after session 2's setsid fix.

### Scheduled Cowork tasks (active)

- `t3b-cpi-settlement-verify-may13` — fires 2026-05-13 09:00 PT. Verifies T3b auto-settle worked.
- `t3c-icsa-reconcile-may14` — fires 2026-05-14 07:30 PT. Runs T3c reconciler `--once`.
- `t3c-icsa-freshness-precheck-may14` — fires 2026-05-14 07:15 PT (NEW this session). 15 min before reconciler. GREEN/YELLOW/RED on whether ICSA print landed.
- `t1-edge-rerun-weekly` — Mondays 08:03 PT.
- `t6-shadow-trading-milestone-checks` — Mondays 09:09 PT.
- `entropy-detector-calibration` — daily 08:09 PT.

### Status command

```
bash ~/Documents/portfolio_status.sh
```

Now: scheduler health, scheduled jobs, freshness watchdog, macro concentration with `${PIPESTATUS[0]}` exit code capture + `macro_cap_alarm.flag` write/clear, live daemons, LaunchAgents.

---

## Critical Operational Lessons

### Net new from session 3

**33. Bash `if ! cmd; then EC=$?; fi` captures the INVERTED exit code, not the original.** The H20 macro cap preflight has this exact bug — `EC` is always 0 inside the body, gate is a no-op. To capture the true exit code:
```bash
cmd
EC=$?
if [ "$EC" -ge 2 ]; then ...; fi
```
Don't combine the test and the capture.

**34. ADP-NFP correlation is < 0.35. ADP doesn't predict NFP at all.** Variance explained is NEGATIVE (subtracting ADP makes the residual MORE volatile). Filter for new engines must include "correlation gate" alongside "σ vs bin spacing gate" — the σ rule didn't catch ADP's deeper failure mode (no signal at all, just noise).

**35. Kalshi event titles already encode information you might think requires an external API.** T7 spec assumed ESPN integration for series_state. The Kalshi event title contains "Game N: Away at Home" — one regex eliminates an entire external dependency. Always probe the actual data first; don't trust spec assumptions about what's available.

**36. Python `global` declarations: at most one per name per function, must come BEFORE the first assignment in the function (not before the first conditional branch that uses it).** Moving `global X` declarations into individual `if` branches works in some cases but fails when multiple branches of the same function each declare it. Consolidate globals at the top of the function.

**37. Daemon detacher (session 2 lesson #23) survives terminal close but the dry-run/redeploy pattern is the operator's gate against a code regression killing live processes.** When changing code paths in a live daemon (e.g., the H-T6-2 fix), build a `--refresh-test` or `--once` flag that exercises the same code without touching the live ledger. Run it before live redeploy. The cost of a dedicated dry-run flag is ~30 LOC and one operator command — it caught the Python `global` bug in this session before it touched the live daemon.

**38. Multiple `_FORCE_RECONNECT`-type globals must consolidate at function top. Multiple `global X` declarations within the same function scope after assignments produce `SyntaxError: name 'X' is assigned to before global declaration`.** Plan globals when designing functions, not when refactoring branches.

**39. Cowork sandbox cannot delete user files via shell `rm` (Operation not permitted) AND cannot resolve mount paths in `mcp__cowork__allow_cowork_file_delete`.** File removal is operator-side. Always include this as an explicit operator action when sealing engines. Trying to invoke the cowork delete tool wastes round-trips.

### Carried forward (still binding)

(See HANDOFF_2026-05-09_session2.md for lessons 1-32. All still apply.)

---

## File Map (additions/changes this session)

```
~/Documents/
  AUDIT_2026-05-09.md                 (NEW — consolidated 7-engine audit, severity-ranked)
  T8_VERDICT_2026-05-09.md            (NEW — T8 archive verdict, replacement candidates)
  terminal7_nba_nhl_spec.md           (NEW — T7 spec v0.1, locked, see §11 for build direction)
  HANDOFF_2026-05-09_session3.md      (THIS FILE)
  HANDOFF_2026-05-09_session2.md      (prior; keep for audit)
  HANDOFF_2026-05-09.md               (original; keep for audit)

  com.t3b.settlement-reconciler.plist (NEW — launchd plist; loaded as PID 40443)
  adp_nfp_residual_study.py           (NEW — T8 gate study, primary)
  adp_nfp_extended_analysis.py        (NEW — T8 gate study, deeper analysis with correlation/regression)

  # Modified this session:
  terminal1_phase2_paper_trader.py    (H1 boundary, H2 contracts comment, H3 annul handling)
  terminal1_settlement_reconciler.py  (H4 T-strike fail-closed)
  terminal3b_settlement_reconciler.py (H10 paper-only docstring)
  terminal3c_settlement_reconciler.py (H13 idempotency try/except)
  terminal6_mlb_kalshi_ws.py          (H18 _ACTIVE_WS + _FORCE_RECONNECT + --refresh-test + force reconnect on subs change)
  terminal6_dashboard.py              (H17 Athletics map normalize)
  portfolio_scheduler.py              (H22 per-job try/except fence)
  portfolio_status.sh                 (H21 ${PIPESTATUS[0]} capture + macro_cap_alarm.flag)
  redeploy_t6_all.sh                  (H20 preflight gates — note bash bug)
  redeploy_entropy_phase1.sh          (H20 preflight gates — same bug)

  # Removed this session (operator-side):
  com.t2.daily-picks.plist            (gone — was in Documents AND Library/LaunchAgents)
  install_t2_daily_picks.sh           (gone)
  deploy_t2_tail_batch.py             (gone)

  # Live data files (auto-generated):
  macro_cap_alarm.flag                (NEW — written by status.sh when cap ≥ 50%)
  terminal3b_data/settlement_reconciler.{out,err}  (launchd output redirects)
```

---

## Pending Actions (ordered)

### Immediate

- **None operator-side.** All approved CRITICAL/HIGH for this session shipped. Next session takes T7 build per §11.

### May 13 — April CPI release (T3b auto-settle)

- ~12:30 UTC: BLS releases.
- ~14:00 UTC: Kalshi finalizes. T3b reconciler (now under launchd, PID 40443) auto-closes 2 NO positions on next hourly tick.
- 09:00 PT: Cowork task `t3b-cpi-settlement-verify-may13` fires to verify closure.
- **Operator check before May 13**: `launchctl list | grep t3b` should still show `com.t3b.settlement-reconciler` with a live PID. KeepAlive will respawn it on crash.

### May 14 — ICSA release (T3c reconcile)

- 05:30 PT: DOL releases ICSA, FRED mirrors within ~1 hour.
- 07:15 PT: Cowork task `t3c-icsa-freshness-precheck-may14` fires (NEW). Returns GREEN/YELLOW/RED.
- 07:30 PT: Cowork task `t3c-icsa-reconcile-may14` fires, runs reconciler `--once`. Now idempotent on double-fire.
- **If pre-check returns YELLOW/RED**: hold the reconciler, ping next session, await ICSA freshness before manual run.

### Weekly milestones (already scheduled)

- Mondays 08:03 PT: `t1-edge-rerun-weekly`
- Mondays 09:09 PT: `t6-shadow-trading-milestone-checks` — most-watched metric in portfolio
- Daily 08:09 PT: `entropy-detector-calibration`

### Bug-fix queue (post-May-14)

See §6.

---

## §11 — T7 BUILD DIRECTION FOR NEXT SESSION

**This is the primary work for the next session.** Steve's instruction: "write t7 its a t6 clone you have the spec."

### What you have

- **Spec**: `~/Documents/terminal7_nba_nhl_spec.md` — full architecture, deltas vs T6, gates, runway, decisions.
- **T6 reference**: every T6 file is your template. They work. Don't reinvent — clone.
- **Reserved bankroll**: T7 entry already in `~/Documents/shadow_pnl/engines.json` with `bankroll_usd: 2000, mode: "reserved", active: false`. Update mode→"shadow" and active→true when ready to deploy.

### Critical context the spec doesn't fully say

1. **The "ESPN API for series_state" assumption is wrong.** Kalshi event titles already contain `"Game N: Away at Home"`. One regex on title eliminates an external dependency. Spec §2.1 has the exact regex. Don't build ESPN integration — you don't need it.

2. **Liquidity warning is real.** All 14 open KXNBAGAME markets at probe time had vol_24h=0 and OI=0. INSUFFICIENT_LIQUIDITY is a foreseeable verdict. Don't be surprised if T7 fires zero trades during conf finals — that IS a valid outcome and the spec acknowledges it.

3. **The macro cap math.** If T7 deploys at $2K active sports, total deployed becomes $30K and macro share drops to 50.0% exact. That brings the portfolio FROM "over cap" TO "at cap exactly." This is the resolution path Steve identified. T7 deployment is what fixes the cap breach.

4. **Adversarial review is a hard gate, not a suggestion.** T2 post-mortem is the template. Three checkpoints in the spec at §8. Don't skip them. Build the spec, send to second model. Build the backtest, send to second model. Capital deploys only after the second checkpoint clears.

5. **Open questions for Steve in spec §9 are NOT optional.** Six open questions. The Odds API tier upgrade ($30/mo) is the most important — T6 alone is over the free tier. Adding T7 makes this acute. Get explicit sign-off before Day 1 of build.

### Build sequence (per spec §10)

**Day 1 — scaffolding + data feeds**
1. Clone T6 files: `terminal6_*` → `terminal7_*` (rename inside files too — module imports, log paths, ENGINE constant, data dir).
2. Strip MLB-specific bits: KXMLBGAME → KXNBAGAME + KXNHLGAME, MLB team map → NBA + NHL maps.
3. Wire Odds API endpoints: `basketball_nba` + `icehockey_nhl` (NOT `baseball_mlb`).
4. Wire Kalshi WS subscription to KXNBAGAME + KXNHLGAME.
5. NBA team map: 30 teams. Watch for Brooklyn Nets / Charlotte Hornets edge cases.
6. NHL team map: 32 teams. **Critical edge cases**: Utah Hockey Club (2024 relocation from Arizona Coyotes — verify Vegas API returns "Utah" or "Utah Hockey Club"; verify Kalshi ticker uses UTA or UHC), Vegas Golden Knights (disambiguate from "Vegas" the city in Vegas API responses), Seattle Kraken.
7. Game-number regex: `re.compile(r'^Game\s+(\d+)\s*:', re.IGNORECASE)`. Reject if N > 2.
8. Unit test the regex against the 7 currently-open NBA event titles probed in this session — they're all G3-G5 currently so should ALL reject (G1-G2 of round 2 has already happened).

**Day 2 — gates + reconciler**
1. Game 1-2 gate as Filter 14 in trader (per spec §3).
2. Per-series exposure cap: parse `R\d` from series_ticker, cap at 2 positions per series (G1+G2).
3. Sport-specific `_normalize()` per spec §2.3. **Maintain ONE per sport, not a shared function** — sport-specific edge cases will leak into each other otherwise.
4. Settlement reconciler: clone T6's, no semantic changes (G3+ won't fire anyway). Verify reconciler handles a synthetic G3 settlement gracefully.
5. Liquidity floor (per spec §3 Filter 16): skip markets with `open_interest < 100` AND `volume_24h < 50`. **Tune at first 3 fires.**
6. **Telemetry MUST mirror T6**: KL near-miss persistence, full rejection breakdown with no value-embedding-in-categories, per-cycle log line. Lesson #31 from session 2 applies here.

**Day 3 — dry runs + adversarial review**
1. Dry-run trader against current open Kalshi NBA/NHL markets. NO positions opened.
2. Confirm Vegas matching works on a sample game. Specifically test the Athletics-style edge cases (Utah Hockey Club, Vegas Golden Knights).
3. **Adversarial review checkpoint #2**: send code + sample dry-run output to a second model BEFORE deploying capital. Look for:
   - Tautological logic (any prior reducing to price × constant)
   - Validation that's post-hoc on settled data
   - Hidden NBA/NHL series structure assumptions
   - Hidden Vegas data quirks (e.g., NHL home-team disambiguation)

**Day 4 — deploy shadow**
1. Update `engines.json` T7 entry: mode→"shadow", active→true.
2. Build `redeploy_t7_all.sh` (clone of `redeploy_t6_all.sh`).
3. Run the same dry-run sequence as session 3's H18 validation: `--once` then `--refresh-test` then live redeploy. Don't skip.
4. Verify TTY=?? for all daemons.
5. Watch first few cycles for "title-parse-failed" rejection counts (per spec §11) — if > 0 on any cycle, alert. Title format change would silently kill the engine.

### Operator workflow patterns to mirror

Steve's operating preferences this session:

- **Show diff before commit on critical-path changes.** When you write a non-trivial change to a file that touches active execution (T6 paper trader, T6 WS, T3b reconciler, T3c reconciler, settlement code), present the diff first. He eyeballs it. He signs off. Then commit.
- **One terminal prompt at a time** when running operator-side commands. He runs, you wait for output, you give next prompt. Don't dump 6 commands at once.
- **CRITICAL/HIGH go to him first; MEDIUM/LOW autonomous.** Same pattern as this session. Don't unilaterally execute high-stakes fixes.
- **Direct, no fluff, no soft language.** "If you want," "perhaps," etc. — don't.
- **Defer work that touches live execution paths between now and any settlement event.** May 13 + May 14 are in the immediate window. Don't touch T3b or T3c reconcilers code-wise until both have settled. T6 fixes are fine — T6 closes are days away.
- **Adversarial review is mandatory before T7 capital.** Not optional. Not "if time permits."
- **Steve will challenge bad reasoning.** When he says something like "your resize suggestion is meaningless," he's right and you re-evaluate. Don't double down on weak arguments.

### Watch-fors (likely T7 traps)

1. **Ticker pattern parsing.** KXMLBGAME prefix is 10 chars. KXNBAGAME is 10 chars. KXNHLGAME is 10 chars. The team-pair parsing in T6's `parse_event_teams` strips `KXMLBGAME-` and looks at characters 11+ (date+teams). Same pattern works for NBA/NHL but verify — date format may differ.
2. **Event title format.** "Game N: Away at Home" was the format observed at probe time. Make sure the regex tolerates whitespace/case variants. Add a fallback: if title doesn't parse, log a `[title-parse-failed]` reject and surface the count in the per-cycle telemetry (per Lesson #35 — sentinel count for silent failures).
3. **Series ticker `R\d` parsing.** Currently round 2 (R2). Watch for "Play-In" round which NBA has — the series ticker format may handle this differently. If you see `R0` or `RPlayIn`, the parser must handle it gracefully.
4. **NHL puck-drop times vary**. MLB games are mostly evening starts at deterministic windows. NHL games can be 1pm matinee, 7pm primetime, or back-to-back doubleheaders. The MIN_LEAD_MINUTES and MAX_LEAD_HOURS gates from T6 may need different values.
5. **Vegas books for NHL playoffs** are sometimes shy of MLB coverage. Pinnacle is solid. DK/FD/MGM may not list every NHL playoff game until close to puck-drop. The "≥3 books for valid sharp" gate from T6 should be kept.
6. **MAX_CONTRACTS at $2K bankroll**. Spec says 200 (vs T6's 500). Check that T6's silent-cap behavior (M-T6-1) doesn't cause issues here — small bankroll means Kelly will be binding more often than the cap. Add the `[size-cap]` log line FROM THE START in T7. Don't inherit T6's silent-override hole.
7. **Adversarial review process** is a hard gate. Steve will ask if you ran it. The T2 post-mortem is the template — second model challenges before capital. Don't skip.

### Files to create (cloned from T6 with sport-specific deltas)

```
~/Documents/
  terminal7_paper_trader.py             (clone terminal6_mlb_paper_trader.py; add Filter 14, Filter 15, sport detection)
  terminal7_kalshi_ws.py                (clone terminal6_mlb_kalshi_ws.py; subscribe KXNBAGAME + KXNHLGAME; KEEP the H-T6-2 fix patterns: _ACTIVE_WS, _FORCE_RECONNECT, --refresh-test flag)
  terminal7_lines_puller.py             (clone terminal6_mlb_lines_puller.py; basketball_nba + icehockey_nhl endpoints; cadence per spec §4.4)
  terminal7_settlement_reconciler.py    (clone terminal6_mlb_settlement_reconciler.py; KEEP the idempotency try/except pattern from H13)
  terminal7_kalshi_logger.py            (clone terminal6_mlb_kalshi_logger.py; series ticker filter)
  terminal7_milestone_check.py          (clone terminal6_milestone_check.py; adjust gates per spec §6 — no early-kill, ARCHITECTURE_CONFIRMED at n≥5)
  terminal7_data/                       (mkdir; mirror terminal6_data/ structure)
  redeploy_t7_all.sh                    (clone redeploy_t6_all.sh; replace terminal6 → terminal7; KEEP --refresh-test gate before live deploy; FIX the H20 macro cap bug while you're in there — capture EC outside `if !`)
```

### Engines.json T7 entry update (when ready to deploy)

```json
"T7": {
  "name": "Kalshi NBA/NHL Playoffs (Game 1-2 only)",
  "bankroll_usd": 2000,
  "active": true,
  "mode": "shadow",
  "category": "sports",
  "notes": "Active 2026-05-XX. Architecture clone of T6 with Game 1-2 only filter via event title regex. Series-state via ticker R\\d parse. NBA + NHL via Odds API basketball_nba + icehockey_nhl. Bankroll $2K from T1 sub-bucket recycle. Kelly cap 5% = $100/position. Total exposure cap 50% = $1K. Architecture-confirmation only this cycle — runway ~12 G1/G2 games combined. Spec at ~/Documents/terminal7_nba_nhl_spec.md. Adversarial review checkpoints completed YYYY-MM-DD."
}
```

### Verification before any T7 bet

1. Adversarial review checkpoint #2 cleared (post-backtest, pre-capital).
2. Edge analysis lower-95 CI > 0 after fees on backtest sample (acknowledging small n).
3. Bucket concentration check (no single bucket > 40% of profit).
4. Entropy collapse calibration hit rate.
5. Macro concentration cap compliant (T7 deployment brings 53.6% → 50.0% exact).
6. Liquidity floor verified at deployment time (some KXNBAGAME open_interest > 100).

---

## How to resume in a new thread

Paste this prompt to the next session:

> Read `~/Documents/HANDOFF_2026-05-09_session3.md` for full context. Run `bash ~/Documents/portfolio_status.sh` and `python3 ~/Documents/terminal6_milestone_check.py` to confirm portfolio state. Report (a) any daemon health issues, (b) T6 close progress toward n=300 with current gate verdict, (c) whether T3b May 13 settlement and T3c May 14 settlement completed cleanly, (d) macro concentration cap status, (e) any new issues since session 3 closed.
>
> Then: read `~/Documents/terminal7_nba_nhl_spec.md` and §11 of the handoff. Build T7 per the sequence in §11. T7 is a structural clone of T6 with three deltas (game number gate via title regex, series state via ticker R\d parse, sport-specific normalization). Adversarial review checkpoints are mandatory before any shadow capital deploys. Open questions in spec §9 require Steve sign-off — surface them at the start of build, not at the end. Direct, no fluff.

### Verification before any new bet (general)

1. Edge analysis lower-95 CI > 0 after fees
2. Bucket concentration check (no single bucket >40% of profit)
3. Entropy collapse calibration hit rate (Phase 2 enabled = full directional logic)
4. Macro concentration cap (50% hard limit) compliant
5. T6 milestone gate not in EARLY_KILL or DEAD state
6. **NEW**: T7 adversarial review checkpoints cleared (if T7 active)

Real-money path does not exist in any engine yet.
