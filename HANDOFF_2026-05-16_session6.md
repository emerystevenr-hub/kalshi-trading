# Terminal Trading Portfolio — Handoff

_Generated 2026-05-16 (session 6, evening UTC). Hand to a fresh session to resume in context. Supersedes HANDOFF_2026-05-10_session5.md._

_Session 6 was the Friday agent. Picked up 6 days after session 5's dark window. Verified the T6 vegas-match patch on real fires, confirmed T3b/T3c settlements, caught a 40-hour Odds API outage that starved T7's conf finals G1 window, rotated the key, archived T1 entirely, bumped T6 to absorb T1's macro-cap denominator slot, and killed 20-day orphan T3b duplicates. Three loose ends carried forward: T3b kalshi-logger needs proper launchd plist before mid-June CPI, SGO trial dashboard cancel due tomorrow May 17, monitoring gap in portfolio_status.sh._

---

## TL;DR — what changed in session 6

**Odds API key rotated.** Old key `64b0...e50f` was returning HTTP 401 since 2026-05-15 01:28 UTC (~40h before session 6 opened) — quota trail showed monthly-tier exhaustion (last good headers: `used=401 remaining=99`, then 99 calls drained May 14, then 401 forever). T6 lines puller and T7 lines puller both starved of vegas data for the entire window. T6 hadn't fired since 2026-05-14 17:56 UTC; **T7 had ZERO fires during the conf finals G1 window** — the audit the Friday session was designed around. Operator rotated to a paid 20K/month plan, new key `ff12...a796`. Smoke-tested against `/sports` endpoint (zero quota) before deploying. Both pullers confirmed pulling games with sharp consensus within minutes. Baseline logged to `~/Documents/odds_api_burn.log`. Projected burn ~1,440 calls/month (T6 ~480/month + T7 ~960/month) → ~14x headroom on the 20K cap. **Day-14 wall risk: none at projected burn rate.** Recheck planned daily.

**T1 archived entirely.** Operator decision mid-session: T1 thesis broken — 40% win rate on public weather data with no independent signal. The 40-50¢ sub-bucket that justified the 2026-05-09 reduction did not hold. Killed 5 T1 daemons (`terminal1_phase2_paper_trader`, `terminal1_kalshi_logger`, `terminal1_metar_taf_puller`, `terminal1_nws_actuals`, `terminal1_settlement_reconciler`) plus `terminal1_model_pullers` (sixth daemon not in initial kill list — caught and killed). engines.json flipped: T1 `bankroll_usd=0`, `active=false`, `mode=archived`, `category=archived`. Pattern matches T2/T4 archived entries.

**T6 bankroll bumped $12K → $13K.** T1 archive shrank the macro-cap denominator from $30K to $29K, pushing macro from 50.0% AT CAP to 51.7% OVER CAP. Bumped T6 by $1K to restore $30K total and 50.0% exact. Kelly per-bet cap moves $600 → $650 (5% of $13K). Total exposure cap moves $6K → $6.5K. Notes appended documenting the recycle.

**Orphan T3b processes killed.** `portfolio_status.sh` revealed two 20-day-old nohup'd duplicates running parallel to the launchd-managed reconciler: pid 73159 (`terminal3b_kalshi_logger.py`) and pid 73161 (`terminal3b_settlement_reconciler.py`) plus their caffeinate wrappers (73164, 73165). The duplicate reconciler created a race condition risk on next CPI settlement (T3b lacks H11 annul-close idempotency, deferred post-May 13 per session 5). Killed cleanly; launchd-managed pid 40443 reconciler still alive and healthy.

**T3b kalshi-logger LaunchAgent attempt FAILED — fell back to nohup.** Killing the orphan removed the only running T3b kalshi-logger. Built `com.t3b.kalshi-logger.plist` mirroring `com.t3b.settlement-reconciler.plist` (5-field swap via heredoc; then via verbatim sed-clone after first attempt failed). Both attempts: launchctl `load -w` and `bootstrap` succeeded, but actual spawn returned exit 78 (EX_CONFIG, "configuration error") in a 30-second throttle loop. Hand-running the exact command launchd uses produced exit 0 with clean output. Stderr file never created — launchd was rejecting before the script ran. Most likely cause: macOS Background Items approval required (Ventura+). Burned token-time pivoting through sudo bootout, modern bootstrap syntax, and verbatim-clone diff — none cleared the 78. **Pivoted to nohup+caffeinate detached pattern** (same as the 20-day orphan that just got killed). Wrote `~/Documents/relaunch_t3b_logger.sh` mirroring the `detach()` function in `redeploy_t7_all.sh` — Python double-fork + setsid + FD redirect. T3b kalshi logger relaunched: pid 6325 (Python) + 6327 (caffeinate), both TTY=?? confirmed, log shows fresh "T3b Kalshi Logger starting" line. **Will not survive a system reboot.** Proper launchd plist must be debugged before mid-June CPI release. Plist file removed; one stale launchd registration noise-looping every 30s with exit 78 until reboot.

**T6 vegas-match patch CONFIRMED on real fires.** 8 clean post-patch closes through session 6, every one with Δ=0.0h between ticker-encoded datetime and `signal_metadata.commence_time_utc`. Clean realized: **-$609.32** across 8 closes (2W/6L, mean -$76.17, 95% CI [-$162.51, +$10.18]). Gate: ACCUMULATING (n=8 of 300). 37 pre-patch contaminated closes (original 25 from session 5 + 12 stale opens that closed post-restart) all correctly flagged contaminated by milestone filter. Rejection bucket "no vegas match (no game within 12h of ticker date)" actively firing 74-88 hits per cycle on stale tickers — patch doing the work. Bankroll readout in paper_trader.log aligned exactly to clean realized at the log's mtime — formula is correct. False alarm on the initial discrepancy I caught (log was stale by 2 closes that settled overnight).

**Settlements: T3b CPI May 12, T3c ICSA May 14 — both LOST.** Settled cleanly via Cowork tasks and launchd-managed reconcilers, no errors, no AlreadyClosedError, no race-induced double-close. Direction was wrong on both prints — YES side won both. T3b: 3 closes (T3.5, T3.6, T3.7 — handoff inventory missed T3.5, engine auto-opened it post-session-5), all NO positions, total -$14.01. T3c: 3 NO positions (-210000, -200000, -195000), total -$25.00. Plumbing sound, macro forecasts wrong this cycle. Total macro engine loss this session: -$39.01.

**macro_cap_alarm.flag cleared.** Flag from 2026-05-14 14:33 UTC (content: `macro cap exit=1`) was operator-decided stale. Single write, never updated — likely a one-time alarm fire from a transient cap touch. Removed at session close.

**SGO Pro $299/mo trial — operator action pending.** `.sgo_api_key` already removed (confirmed sandbox-side at session open). Dashboard cancel in SGO billing UI must happen before May 17 auto-bill — calendar item for operator, can't be verified from sandbox.

---

## Decision register (locked this session)

### T1 archive — FULL ARCHIVE (NEW, locked 2026-05-16)

T1 weather ensemble archived after the 40-50¢ sub-bucket gating (session 4) did not produce defensible edge. Win rate 40%, no independent signal — all data is public NWS/MET, no proprietary forecast advantage. Bankroll $1K → $0, 5+1 daemons killed, mode/active/category flipped to archived pattern matching T2/T4. Reactivation requires a structural change to the data source (e.g., paid proprietary forecast API or ensemble methodology not available via free NWS).

### T6 bankroll = $13K (NEW, locked 2026-05-16)

After T1 archive shrank the cap denominator, bumped T6 from $12K to $13K to restore the macro concentration cap to exactly 50.0%. Routine reallocation that preserves the structural discipline of the 50% hard limit. Kelly cap moves to $650 per bet; total exposure cap to $6.5K. No change to T6's validation criteria (n=300 with positive lower-95).

### Odds API plan upgrade — 20K/month tier (NEW, decided 2026-05-16)

Free tier (~500/month) exhausted on day ~12 of the May cycle. Burn rate on T6 + T7 combined is ~48 calls/day → 1,440/month. Free tier was structurally insufficient once T7 deployed; this should have been caught when T7 went live in session 5 but the cap math wasn't computed. Paid 20K/month tier has ~14x headroom, won't exhaust at current cadence. Day-14 quota wall risk: none unless cadence changes.

### Vegas-match data integrity — CRITICAL (carried, still binding, session 5)

Unchanged. Ticker-date narrowing required on every Kalshi-to-Vegas join. Patch confirmed live on T6 and T7 in real fires this session.

### SGO Pro $299/mo — CANCEL (carried, still binding, session 5)

Dashboard cancel pending operator action before May 17 auto-bill. No daemon, LaunchAgent, or cron polls SGO. `.sgo_api_key` already removed.

### KL automated kill-switch — manual through n=300 (carried, still binding)

Unchanged. Carried from session 4.

### Build queue (updated)

| # | Item | Type | Status | Earliest start |
|---|---|---|---|---|
| 0a | T1 sub-bucket gate + capital recycle | prerequisite | ✅ shipped session 2 | done |
| 0b | Macro concentration dashboard + cap | prerequisite | ✅ shipped session 2 | done |
| 1 | T7 (NBA/NHL playoffs, Game 1-2 only) | engine, $2K shadow | ✅ DEPLOYED 2026-05-10; missed G1 window due to Odds API outage | live, awaiting G2+ fires |
| 2 | ~~T8 (NFP nowcast)~~ | engine | ARCHIVED 2026-05-09 | n/a |
| 2-replacement | TBD: JOLTS+Indeed / PMI / HFW / Truflation | engine | gated on residual study | post-T7 first cycle |
| 3 | T9 (crypto, funding-rate indicator) | engine | gated on correlation matrix | after T6 hits clean n≥100 |
| 4 | T10 (earnings) | engine | likely permanent defer | n/a |
| 5 | T11 (NFL game-winner) | engine, conditional | gated on T6 validation (clean n≥300, lower-95 ≥ 0) | September 2026 |

### T6 early-kill criteria (unchanged)

`terminal6_milestone_check.py`, Mondays 9:09 AM PDT:
- `n ≥ 200 and lower_95 < -$0.50` → EARLY KILL
- `n ≥ 300 and lower_95 ≥ 0` → VALIDATED
- `n ≥ 300 and mean > 0 and lower_95 < 0` → INCONCLUSIVE (extend to n=500)
- `n ≥ 300 and mean ≤ 0` → DEAD
- else → ACCUMULATING

Current: n=8 (clean), gate=ACCUMULATING. Audit n=45 quarantined.

### T7 milestone gates (unchanged)

Per spec §6. Current: n=0, gate=ACCUMULATING. **G1 conf finals window MISSED due to Odds API outage** — runway compressed by ~5-7 games. ARCHITECTURE_CONFIRMED gate (n≥5) still reachable through G2+ and Finals (~12 games remaining in playoff universe).

### Macro concentration cap

50.0% AT CAP exact (restored). exit code 1. Block any new macro deployment. `macro_cap_alarm.flag` cleared as stale.

### Data source for T6 + T7 — LOCKED

Odds API paid 20K/month tier (upgraded from free tier this session). T6 + T7 lines pullers stay on Odds API; AllStar WebSocket evaluation deferred to a future session if/when sharp lead-time thesis is reopened.

### Adversarial review process

Session-5 lesson 50 (every join key needs a disambiguator) held — vegas-match patch verified on real fires. New session-6 addition: lesson 57 below.

---

## Strategic context

- **Operator**: Steve Emery, Bend OR (Pacific Time). Direct, no fluff, one strong direction, structural advantage > tactical optimization.
- **Operating pattern this session**: bias toward one terminal prompt at a time on host-side actions; sandbox edits in parallel without prompts; show diff before commit on critical-path changes; CRITICAL/HIGH to Steve first; MEDIUM/LOW autonomous. Same as session 5.
- **Goal**: $500K profit, multi-year, multi-engine.
- **Validation discipline**: No engine goes to real money until clean n ≥ 300 with positive lower-95 CI on after-fee mean P&L. T6 has explicit early-kill at n=200.
- **Cadence-bound thesis**: macro engines (T3a/T3b/T3c) max out at ~24 events/year combined. Sports (T6/T7/T11) is the daily-cadence vertical that scales.
- **Filter for new engines**: independent leading indicator outside Kalshi AND costly enough that retail can't easily access it.

---

## Portfolio P&L — Lifetime (post-session-6)

| Engine | Open | Closed (clean) | Realized (clean) | Open CB | Mode | Notes |
|---|---|---|---|---|---|---|
| T1 (weather) | 0 | n/a | n/a | $0 | **archived** | ARCHIVED 2026-05-16 session 6; thesis broken; all daemons killed |
| T2 (catalyst) | 4 | 8 | -$161.48 | $155 | archived | unchanged |
| T3a (Fed arb) | 0 | 0 | $0 | $0 | shadow | unchanged; June 17 FOMC |
| T3b (CPI) | 0 | 3 | -$14.01 | $0 | shadow | May 12 CPI lost; engine auto-opened T3.5 between session 5 and settlement |
| T3c (Claims) | 1 | 8 | -$20.75 | varies | shadow | May 14 ICSA lost (3 NO close, -$25); session 6 net -$25 |
| T6 (MLB) | 0 | 8 (audit: 45) | -$609.32 (audit: +$3,081.59) | $0 | shadow | clean n=8/300, gate=ACCUMULATING; vegas-match patch confirmed working |
| T7 (NBA/NHL) | 0 | 0 | $0 | $0 | shadow | $2K bankroll; missed G1 conf finals window (40h Odds API outage); now live |
| T5 (catalyst finder) | — | — | — | — | infrastructure | unchanged |
| T4 (Polymarket arb) | — | — | — | — | archived | unchanged |

**T6 historical "lifetime" P&L of $+3,081.59 across 45 audit closes** preserved in ledger but quarantined from milestone gate. 37 of 45 closes flagged contaminated (open_ts pre-2026-05-11 + Δ>12h). Clean n=8 since validation clock reset session 5.

**T6 open positions: 0** — last 2 pre-fix carryovers closed early May 16 UTC (MIATB -$187.18, NYYNYM -$203.33). Engine is waiting on vegas data signal to fire fresh after Odds API restoration this session.

---

## Engine States

### T1 — Kalshi Weather Ensemble (ARCHIVED 2026-05-16)

**Status:** archived this session. Bankroll $0. 6 daemons killed. engines.json mode=archived, active=false, category=archived. **Reactivation requires structural change** — paid proprietary forecast API or new ensemble methodology. Spec deprecated for the current data architecture.

### T2 — Kalshi Catalyst Book (ARCHIVED)

Unchanged. Archived 2026-05-09.

### T3a — Kalshi Fed Decisions

Unchanged. Scanner running, no opens. Next FOMC June 17-18. Deferred HIGHs (H5-H9) pre-FOMC pass.

### T3b — Kalshi CPI Nowcast

**Status:** running (paper_trader pid 35587, settlement_reconciler launchd-managed pid 40443). **Kalshi logger relaunched detached via nohup pid 6325 (PYTHON) + 6327 (caffeinate), TTY=?? confirmed — NOT launchd-managed.** Will not survive system reboot.

**Session 6 changes:**
- May 12 CPI settlement: 3 NO positions lost (T3.5, T3.6, T3.7), -$14.01 total. Reconciler closed at 14:34 UTC, no errors. Note: T3.5 was opened between session 5 close and settlement by the engine's auto-opener (handoff inventory only captured T3.6, T3.7).
- Orphan T3b kalshi_logger pid 73159 + duplicate settlement_reconciler pid 73161 killed (20-day uptime, parallel to launchd-managed reconciler — race risk on settlement events).
- Launchd plist attempt for kalshi-logger failed exit 78 across 3 different installation approaches (heredoc, sed-clone of working reconciler plist, sudo bootout + modern bootstrap). Pivoted to nohup detach. **Proper launchd plist is OPEN priority before mid-June CPI release.**

**Friday agent (session 7): debug the launchd plist exit-78 mystery before mid-June CPI release.** Likely culprit: macOS Ventura+ Background Items approval. Path forward: System Settings → General → Login Items & Extensions → Background Items → enable `com.t3b.kalshi-logger`, OR investigate why a byte-identical clone of the working settlement-reconciler plist still hits 78 when its template does not. The script itself is fine (hand-run produces exit 0).

**Deferred HIGH (still post-May 13):** H11 (annul-close ordering bug, inert today).

### T3c — Kalshi Initial Jobless Claims

**Status:** running (paper_trader + kalshi_logger + reconciler). Mode=shadow.

**Session 6 changes:**
- May 14 ICSA settlement: 3 NO positions lost (-210K, -200K, -195K), -$25.00 total. Reconciler ran cleanly via Cowork tasks `t3c-icsa-freshness-precheck-may14` and `t3c-icsa-reconcile-may14`. No errors, no AlreadyClosedError.

**Deferred HIGH (still post-May 14):** H15 (annul-close ordering, inert today).

### T6 — Kalshi MLB Game Markets (vegas-match patch CONFIRMED on real fires)

**Status:** running, $13K bankroll (bumped from $12K this session). PIDs from session 5: ws=60083, trader=60087, reconciler=60091 (uptime ~6 days at session 6, still cycling per logs). **NOT visible in portfolio_status.sh** — see open item #3 below.

**Session 6 changes:**
- Vegas-match patch verified on 8 clean post-2026-05-11 fires: Δ=0.0h on every fire, zero contamination, rejection bucket actively firing.
- Lines puller starved 40h during Odds API outage (2026-05-15 01:14 UTC → key rotation 2026-05-16 17:26 UTC). Last pre-stall fire was 2026-05-14 17:56 UTC. After key restoration, 15 MLB games + sharp consensus restored.
- Bankroll $12K → $13K (T1 archive recycle).
- Bankroll readout in paper_trader.log aligned to clean realized (verified false alarm on initial discrepancy).

**Concentration flags at n=8 (NOT actionable, monitor at n=50):**
- 3-4pp delta bucket = 66.6% of clean P&L (>40% threshold)
- Tampa Bay Rays = 47.4% of clean P&L (>30% threshold)

**Friday agent (next): T6 should start firing on G1 conf finals MLB games as conf finals plays out alongside regular season. Verify Δ=0.0h on every new fire. n moves toward 50 then 200/300 gates.**

**Open: rolling 30-day median KL diagnostic** in Monday milestone output — still queued from session 4, not shipped.

### T7 — Kalshi NBA/NHL Playoffs (RUNWAY COMPRESSED — missed G1)

**Status:** ✅ live shadow, $2K bankroll. **Redeployed session 6 post-key-rotation.** PIDs: ws=3918, trader=3922, reconciler=3926, lines_puller=3930. All TTY=??.

**Session 6 changes:**
- T7 had ZERO fires during the conf finals G1 window because Odds API key was 401-ing the entire window.
- Last good T7 lines pull: 2026-05-15 01:12 UTC.
- First post-key-rotation pull: 2026-05-16 17:21 UTC — 2 NBA games + 2 NHL games with sharp consensus (conf finals G1s exactly).
- Engine cycling every 30 min but no fires yet at session-6 close — vegas data only just restored, edge calcs starting fresh.

**Runway impact:** Conf finals universe is ~12-16 G1/G2 games combined. Losing G1 of one or both conferences eliminates ~4-6 games from the validation universe. ARCHITECTURE_CONFIRMED gate (n≥5) is still reachable via G2 + Finals G1/G2 (~8-10 remaining games) but margin is tighter than session 5's assumption.

**Friday agent (next): T7 first fires arrive in next 48h on G2 markets and forward. Audit every fire's Δ=0.0h (±18h for NBA/NHL date-only tickers). Watch the cluster-by-sport and series distribution.**

### T8, T5, T4 — unchanged

---

## Audit findings — full disposition (post-session-6)

### CRITICAL (3, all resolved)

- ✅ C1 (session 4): redeploy_t7_all.sh `set -e` aborts before EC capture — fixed.
- ✅ C2 (session 5): T6+T7 vegas-match wrong-day binding — fixed; CONFIRMED on real fires this session.
- ✅ C3 (session 6): Odds API monthly quota exhaustion blocking T6+T7 vegas data → key rotated to 20K/month tier.

### HIGH (new this session)

- ⏳ **H17 (NEW session 6):** T3b kalshi-logger launchd plist returns exit 78 (EX_CONFIG) across all installation paths. Same script runs cleanly via hand invocation. Stderr file never created — launchd rejecting before script starts. Likely macOS Background Items approval. **Must be resolved before mid-June CPI** (without it, T3b open-side scanner skips every cycle).
- ⏳ **H18 (NEW session 6):** `portfolio_status.sh` does not list T6 or T7 daemons even though they are running and writing logs. Status-script `ps` filter doesn't match `terminal6_*` or `terminal7_*` process names. Steve's standard health check is blind to the two most important engines. Fix the filter, add to portfolio_status.sh.
- ⏳ **H19 (NEW session 6):** `entropy_collapse_detector` scheduler job exit=124 (shell timeout) on last run. 5-min cadence job hitting timeout. Inspect: stuck network call, growing input, or genuine bug. Won't fire trades but worth investigating.

### MEDIUM (new + carried)

- M1-M5 (session 4 T7) — opportunistic cleanup, deferred.
- M6 (session 5, SGO trial credential cleanup) — `.sgo_api_key` already absent. Dashboard cancel still operator-side, due May 17.
- ⏳ **M7 (NEW session 6):** Scheduler exit=0 on `t6_mlb_lines_puller` despite the puller logging `[fatal] 401 unauthorized — API key bad`. The script returns 0 on "pull: 0 games returned" — exit code does not reflect actual data flow. Add a "no fresh data in N cycles" check at scheduler level or change the puller's exit code on persistent 401.
- ⏳ **M8 (NEW session 6):** PID 4476 holding a read handle on `kalshi_KXCPIYOY-26MAY_2026-05-09.jsonl` (7-day-old file). Some long-running analysis script with a stale handle. Identify and clean up.

### LOW (4, opportunistic)

- L1-L4 unchanged from session 4.

### NEGATIVE TEST FINDINGS

- N1, N2 unchanged from session 4.

---

## Bug-fix queue (post-session-6)

1. **T3b kalshi-logger launchd plist exit-78 debug** (NEW, **CRITICAL PRIORITY before mid-June CPI**). Try System Settings approval first. If that doesn't work, capture `log show --predicate 'subsystem == "com.apple.xpc.launchd"' --last 5m | grep kalshi-logger` to see launchd's actual reason. Currently running as nohup detach via `~/Documents/relaunch_t3b_logger.sh` — survives terminal close but NOT a system reboot.

2. **portfolio_status.sh T6/T7 visibility** (NEW, HIGH). Update ps filter to include `terminal6_*` and `terminal7_*` patterns.

3. **entropy_collapse_detector exit=124 investigation** (NEW, HIGH).

4. **Lines puller quiet-failure scheduler signal** (NEW, MEDIUM). Either the puller should exit non-zero on persistent 401, or the scheduler should flag "no data in N cycles."

5. **Rolling 30-day median KL in Monday milestone output** (carried, ENGINEERING TASK). Modify `terminal6_milestone_check.py`.

6. **`AlreadyClosedError` class in `shadow_pnl_core.py`** (carried). Replaces string-match fragility across reconcilers. ~10 min refactor.

7. **T3b annul-close ordering** (H11): mirror canonical `compute_state` from `shadow_pnl_core`. Post-June 11 (next CPI).

8. **T3c annul-close ordering** (H15): same fix. Post-May 21 (next ICSA).

9. **T3a hardening pass** (H5-H9): pre-June 17 FOMC.

10. **T7 MEDIUMs M1-M5** (session-4 review): opportunistic cleanup.

11. **Defensive isinstance guards in `detect_sport`/`parse_kalshi_event_teams`** (N2): one-line fix.

12. **PID 4476 stale read handle** (NEW, MEDIUM). Identify and clean up.

---

## Active Daemons & Schedules — verified at session-6 close

### Scheduler (`portfolio_scheduler.py`)

4 jobs configured. **t2_daily_picks no longer in scheduler_status.json** (was DISABLED at session 5; likely pruned). At session-6 close:

| Job | Cadence | Last exit | Notes |
|---|---|---|---|
| t1_nws_actuals | 6h | 0 | **T1 archived this session — this job is now no-op orphan; should be removed** |
| t3c_claims_data | 12h | 0 | enabled |
| t6_mlb_lines_puller | 90min | 0 | enabled (but exit=0 was misleading during 401 outage) |
| entropy_collapse_detector | 5min | 124 | **TIMEOUT — investigate** |

### Long-running daemons

| Engine | PID (session 6) | Daemon | Notes |
|---|---|---|---|
| T6 ws_logger | 60083 (carried) | `terminal6_mlb_kalshi_ws.py` | session 5 PID, still cycling |
| T6 paper trader | 60087 (carried) | `terminal6_mlb_paper_trader.py --live --interval-sec 1800` | session 5 PID |
| T6 reconciler | 60091 (carried) | `terminal6_mlb_settlement_reconciler.py --interval-sec 3600` | session 5 PID |
| T3b paper trader | 35587 | `terminal3b_paper_trader.py --interval-sec 1800` | nohup |
| T3b reconciler | 40443 | launchd KeepAlive `com.t3b.settlement-reconciler` | launchd |
| T3b kalshi-logger | 6325 + 6327 | `terminal3b_kalshi_logger.py --interval-sec 300` (nohup detach + caffeinate) | **NEW session 6; TTY=??; NOT launchd; will not survive reboot** |
| T3c paper trader | 35593 | nohup | unchanged |
| T3c kalshi_logger | 80218 | nohup | unchanged |
| T7 ws_logger | 3918 | `terminal7_kalshi_ws.py` | RESTARTED this session |
| T7 paper trader | 3922 | `terminal7_paper_trader.py --live --interval-sec 1800` | RESTARTED this session |
| T7 reconciler | 3926 | `terminal7_settlement_reconciler.py --interval-sec 3600` | RESTARTED this session |
| T7 lines puller | 3930 | `terminal7_lines_puller.py --interval-sec 900` | RESTARTED this session |

PIDs change per session boundary. TTY=?? confirmed at restart.

### Scheduled Cowork tasks

- `t1-edge-rerun-weekly` — Mondays 08:03 PT **(should be disabled; T1 archived this session)**
- `t6-shadow-trading-milestone-checks` — Mondays 09:09 PT
- `entropy-detector-calibration` — daily 08:09 PT
- ~~`t3b-cpi-settlement-verify-may13`~~ — fired and complete
- ~~`t3c-icsa-reconcile-may14`~~ — fired and complete
- ~~`t3c-icsa-freshness-precheck-may14`~~ — fired and complete

### Status command

```
bash ~/Documents/portfolio_status.sh
```

(Note: status script does NOT show T6 or T7 daemons. Use `ps aux | grep terminal[67]_` for explicit T6/T7 visibility until H18 is fixed.)

---

## Critical Operational Lessons

### Net new from session 6

**56.** Odds API monthly quota exhaustion presents as HTTP 401 with body "API key invalid" — same error as a revoked key. To disambiguate: hit `/v4/sports?apiKey=$KEY` (zero-quota endpoint). If 401 there too, the key is genuinely dead. If 200 there but 401 on data endpoints, you've exhausted quota and need a new key or tier upgrade. The quota-trail signal is the `x-requests-remaining` response header which the pullers log per-call.

**57.** Archiving an engine shifts the macro concentration cap denominator. Total deployed shrinks, macro stays the same → macro ratio rises. Plan the reallocation in lockstep with the archive: if you don't bump another engine's bankroll, the cap goes from AT to OVER. The macro cap is the structural discipline test, not a real-money risk control (this is paper trading) — but the discipline matters because it forces explicit choice rather than drift.

**58.** `launchctl load -w` (legacy) succeeds with "load: OK" but can silently re-attach to broken cached state if a prior bootout failed. Modern `launchctl bootstrap gui/$(id -u) <plist>` is required. Modern `launchctl bootout gui/$(id -u)/<label>` is required for clean state — if it returns "Input/output error", you need `sudo launchctl bootout`. If sudo bootout still fails or yields cached behavior, only a reboot fully clears the registration. **All launchd state mutations should pair bootout with bootstrap, never load + load.**

**59.** exit code 78 (EX_CONFIG) from launchd-managed jobs is NOT necessarily a plist content error. A byte-identical clone of a working LaunchAgent plist (same template, only Label + program path + log paths + interval swapped via sed) can still return exit 78 in `launchctl list` while the original template works fine. Most likely cause on Ventura+ macOS: Background Items approval. New plists must be enabled in System Settings → General → Login Items & Extensions → Background Items. The `launchctl load` / `launchctl bootstrap` commands succeed without flagging this — they only register the plist, they don't check approval. Approval is enforced at spawn time, surfacing as EX_CONFIG. **Do not burn time on plist debugging when the same template works for one job and not another; check Background Items first.**

**60.** `tail -f <log>` showing in `pgrep -fl <pattern>` is not a daemon. It's a shell command holding a file open with the pattern in its argv. Don't kill it as part of daemon cleanup — leave shell commands alone. The cleanup script should `grep -v "tail -f"` or pattern-match more strictly.

**61.** Don't hardcode parameter values (like an API key fingerprint) into audit log lines without freshly reading them. Stale parameter values in audit logs are worse than no audit log — they create false traceability. Read the live value at the moment of logging.

**62.** When the operator says "this is paper trading, no reason to be limited by money" — the dollar-figure framing of bankroll changes ("freed up $1K") is misleading. Bankroll is a sizing parameter for Kelly + a denominator for the concentration cap. There is no real capital. The structural cap is still meaningful as a discipline test. Use "parameter" or "denominator slot" language, not "capital freed."

### Carried forward (still binding — see HANDOFF_2026-05-10_session5.md and earlier for full text)

Lessons 1-55 still apply. Of particular relevance to the next agent:

- **48** (sandbox vs host filesystem path mismatch — `Path.home()` returns sandbox user home, NOT operator's host home)
- **49** (vegas-match team-pair-only matching unsafe — confirmed in practice this session)
- **50** (adversarial review needs join-key disambiguator question — held up this session)
- **53** (zsh globs abort entire command-line on no-match — relevant for multi-command pastes)
- **54** (sandbox bash calls are isolated, no env/cwd carryover)

---

## File Map (additions/changes session 6)

```
~/Documents/
  HANDOFF_2026-05-16_session6.md       (NEW — this file, supersedes session5)
  HANDOFF_2026-05-10_session5.md       (prior; keep for audit)

  # SESSION-6 CHANGES
  shadow_pnl/engines.json              (MODIFIED — T1 archived; T6 bankroll $12K→$13K)
  .odds_api_key                        (MODIFIED — new key fingerprint ff12...a796, 20K/month tier)
  odds_api_burn.log                    (NEW — daily burn rate audit log; baseline + correction)
  relaunch_t3b_logger.sh               (NEW — wrapper for nohup+caffeinate+setsid detach of T3b kalshi logger; survives terminal close but NOT reboot)

  # SESSION-6 REMOVED
  macro_cap_alarm.flag                 (REMOVED — stale from 2026-05-14)
  ~/Library/LaunchAgents/com.t3b.kalshi-logger.plist  (REMOVED — exit-78 failed; queued for session-7 debug)
```

---

## Pending Actions (ordered)

### TOMORROW (May 17) — SGO trial cleanup deadline

**1. Cancel SGO Pro $299/mo trial in their billing dashboard.** Auto-bill triggers May 17 ~midnight if not cancelled. `.sgo_api_key` is already removed; only the dashboard cancel remains. Operator action — must happen in SGO billing UI.

### Before mid-June CPI (next CPI release ~June 11) — T3b kalshi-logger launchd

**2. Debug exit-78 on `com.t3b.kalshi-logger.plist`.** First: open System Settings → General → Login Items & Extensions → Background Items, look for any disabled `com.t3b.*` entry. If found, enable and retry bootstrap. If not present, check macOS Console / `log show` for the actual rejection reason. Until fixed, T3b kalshi-logger runs via nohup detach (relaunch_t3b_logger.sh) and will not survive a system reboot.

### Before next FOMC (June 17-18) — T3a hardening

**3. T3a pre-FOMC pass:** H5-H9 from session 3 audit.

### Daily — Odds API burn-rate audit

**4. Append daily snapshot to `~/Documents/odds_api_burn.log`** to track actual vs projected ~48 calls/day. Reminder placed for ~17:30 UTC daily.

### Mondays — scheduled

- 08:03 PT: `t1-edge-rerun-weekly` — **should be disabled or removed; T1 archived**
- 09:09 PT: `t6-shadow-trading-milestone-checks` — most-watched
- daily 08:09 PT: `entropy-detector-calibration`

### Engine slot #2 candidate selection (no rush)

Replacement for archived T8. Candidates: JOLTS+Indeed Hiring Lab, PMI Employment Index, high-frequency wage data (Homebase/Paychex), Truflation. Filter: independent leading indicator outside Kalshi AND costly enough that retail can't easily access it.

---

## §11 — DIRECTION FOR NEXT SESSION (session 7)

**You're picking up after session 6's heavy work — Odds API key rotation, T1 full archive, T6 bankroll bump, orphan T3b kill, T3b kalshi-logger nohup detach pivot, vegas-match patch confirmation on real fires. Carry forward the open items below. Direct, no fluff. CRITICAL/HIGH to Steve first; MEDIUM/LOW autonomous.**

### Resume sequence (priority order)

**1. Daemon health snapshot.**

```
bash ~/Documents/portfolio_status.sh
```

Confirm: scheduler clean (all exit=0), no freshness alarm, macro cap exit=1 (AT CAP, restored to 50.0% exact), all daemons TTY=??.

**Watch-for:** portfolio_status.sh does NOT list T6 or T7 daemons (H18 from session 6). Use `pgrep -fl "terminal[67]_"` for explicit visibility until that filter is fixed.

Also verify T3b kalshi-logger (pid 6325 + 6327 at session-6 close — will change if Mac was rebooted) is still running TTY=??:

```
pgrep -fl terminal3b_kalshi_logger
```

If absent, the Mac was rebooted since session 6 close and the nohup detach didn't survive. Re-run:

```
bash ~/Documents/relaunch_t3b_logger.sh
```

**2. T3b kalshi-logger launchd plist debug (BLOCKER for mid-June CPI).**

Open System Settings → General → Login Items & Extensions → Background Items. Look for `com.t3b.kalshi-logger` or any disabled `com.t3b.*` entry. If found, enable. Otherwise capture `log show --predicate 'subsystem == "com.apple.xpc.launchd"' --last 30m | grep -iE "kalshi-logger|t3b" | head -30` and inspect.

The plist content is verified clean (byte-identical sed-clone of `com.t3b.settlement-reconciler.plist` with 5 fields swapped — that template works). Issue is not the plist; issue is macOS approval or some launchd-domain state. Don't waste cycles regenerating the plist again — fix the approval/state.

Once `launchctl list | grep t3b.kalshi-logger` shows a numeric PID and exit=0, kill the nohup-detached pid 6325 + 6327 so launchd is the sole manager.

**3. T6 + T7 active fires audit.**

```
python3 ~/Documents/terminal6_milestone_check.py
python3 ~/Documents/terminal7_milestone_check.py
```

T6: clean n should be growing past 8. Spot-check 2-3 recent fires for Δ=0.0h between ticker datetime and commence_time_utc. Concentration flags (3-4pp delta bucket 66.6%, Tampa Bay Rays 47.4%) recompute at n=50 → not actionable yet.

T7: first fires should arrive on G2 conf finals games (NBA + NHL G2 markets list as `KX(NBA|NHL)GAME-26MAYDD...`). For each fire, verify ticker date within ±18h of commence_time_utc, event_title contains "Game 1" or "Game 2", and series_id concentration is at most 2 per series. Conf finals G1 was MISSED due to the Odds API outage — G2 onward is the architecture-confirmation runway.

**4. Odds API burn rate check.**

```
tail -5 ~/Documents/odds_api_burn.log
grep -h "odds-api" ~/Documents/terminal6_data/lines_puller.log ~/Documents/terminal7_data/lines_puller.log | tail -10
```

Projected: ~48 calls/day → 1,440/month. Cap: 20K/month. Headroom: ~14x. If observed 24h delta is >100 calls, investigate — cadence may have changed or a debug loop is running.

**5. Macro concentration cap.**

```
/opt/homebrew/bin/python3 ~/Documents/portfolio_macro_concentration.py; echo "exit=$?"
```

Expected: 50.0% AT CAP exit=1 (restored at session 6 close after T6 bankroll bump). If exit≥2, the cap has tripped — investigate.

Check alarm flags:

```
ls -la ~/Documents/freshness_alarm.flag ~/Documents/macro_cap_alarm.flag 2>&1
```

Both should be absent.

**6. SGO May 17 cleanup confirmation.**

Confirm operator cancelled SGO Pro trial in dashboard before May 17 auto-bill. The `.sgo_api_key` is already removed. If operator missed the cancel window and got billed, escalate immediately for credit-card-side cancellation.

**7. Bug-fix queue progress.**

See "Bug-fix queue (post-session-6)" section. Priority order: H17 (T3b launchd) → H18 (portfolio_status visibility) → H19 (entropy timeout) → M7 (lines-puller exit-code on 401).

### Items the prior agent verified before going dark

- Odds API key rotated (ff12...a796), both pullers confirmed pulling games with sharp consensus.
- T1 fully archived (6 daemons killed, engines.json updated).
- T6 bankroll bumped to $13K, macro cap restored to 50.0% exact.
- Orphan T3b processes killed (pid 73159, 73161 + caffeinate wrappers).
- T3b kalshi-logger relaunched detached (pid 6325 + 6327, TTY=??).
- T6 vegas-match patch verified on 8 clean fires (Δ=0.0h on every one).
- macro_cap_alarm.flag removed (stale).

### Watch-fors (likely traps for the next agent)

1. **portfolio_status.sh blindness to T6/T7.** Don't conclude "T6 daemon is missing" from the status script's absence — use `pgrep -fl "terminal[67]_"` instead.

2. **T3b kalshi-logger pid 6325/6327 die on reboot.** If Mac was rebooted between sessions, T3b open-side scanner will be skipping cycles silently. Check first.

3. **Scheduler exit=0 is not data-flow confirmation.** During the May 15 outage, scheduler showed all exits=0 while T6+T7 were starved. Don't trust scheduler exit codes for data-flow validation.

4. **T1 archive cleanup leftovers:** `t1_nws_actuals` scheduler job still exists, `t1-edge-rerun-weekly` Cowork task still scheduled. Both are no-op orphans. Disable when convenient.

5. **macOS Background Items approval is the most likely launchd exit-78 root cause.** Don't regenerate the plist again. The plist is fine.

6. **NHL Mammoth / Utah Hockey Club** — H3+H6 normalization handles both, but not yet live-tested.

7. **Conf finals G1 window missed.** T7 runway compressed; ARCHITECTURE_CONFIRMED n≥5 gate still reachable via G2 + Finals.

8. **Odds API burn-rate is project, not measured.** First full 24h of observed burn will validate or invalidate the 1,440/month projection.

### Files to read before any new bet or engine deploy

```
~/Documents/HANDOFF_2026-05-16_session6.md       (this file)
~/Documents/HANDOFF_2026-05-10_session5.md       (prior; reference for vegas-match fix details)
~/Documents/terminal6_mlb_paper_trader.py        (vegas-match fix in evaluate_market)
~/Documents/terminal7_paper_trader.py            (same fix, NBA/NHL)
~/Documents/terminal6_milestone_check.py         (contamination filter)
~/Documents/relaunch_t3b_logger.sh               (fallback for T3b kalshi-logger restart)
```

### Verification gates before any new engine deploy (unchanged from session 5)

1. Edge analysis lower-95 CI > 0 after fees on clean closes only
2. Bucket concentration check (no single bucket >40% of profit)
3. Entropy collapse calibration hit rate
4. Macro concentration cap (50% hard limit) — currently AT CAP, blocks new macro
5. T6 milestone gate not in EARLY_KILL or DEAD state on clean closes
6. T7 milestone gate not in ARCHITECTURE_FAIL or INSUFFICIENT_LIQUIDITY state
7. Adversarial review checkpoints cleared
8. Vegas-match (or equivalent join) verification — for any engine joining Kalshi tickers to external sharp data, the join key MUST be disambiguated by the ticker's encoded date
9. Review-checklist question "for every join key, what could match the wrong record? Show me the disambiguator." answered explicitly

Real-money path does not exist in any engine yet. Clean n=300 is the gate.

---

## How to resume in a new thread

Paste this prompt to the next agent:

> Read `~/Documents/HANDOFF_2026-05-16_session6.md` for full context. Resume per §11.
>
> Run `bash ~/Documents/portfolio_status.sh`, then `pgrep -fl "terminal[67]_"` (portfolio_status.sh has a T6/T7 visibility gap per H18), then `pgrep -fl terminal3b_kalshi_logger` (verify nohup detach still alive — relaunch via `bash ~/Documents/relaunch_t3b_logger.sh` if absent).
>
> Then `python3 ~/Documents/terminal6_milestone_check.py` and `python3 ~/Documents/terminal7_milestone_check.py` for engine state.
>
> Then `/opt/homebrew/bin/python3 ~/Documents/portfolio_macro_concentration.py` to verify cap still at 50.0% exact exit=1.
>
> Report: (a) T6 clean n growth past 8, ΔH=0.0h on recent fires; (b) T7 first conf-finals G2 fires audit — count, sport/series distribution, Δ within ±18h; (c) T3b kalshi-logger plist debug status — primary suspect is macOS Background Items approval (Settings → General → Login Items & Extensions → Background Items); (d) Odds API burn rate observed vs projected ~48/day; (e) SGO Pro trial dashboard cancellation confirmation (was due May 17); (f) macro cap status; (g) any new issues since session 6 closed.
>
> CRITICAL/HIGH go to Steve first; MEDIUM/LOW autonomous. Show diff before commit on critical-path changes. One terminal prompt at a time. Direct, no fluff.
