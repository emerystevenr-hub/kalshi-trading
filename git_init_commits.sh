#!/usr/bin/env bash
# =============================================================================
# Git initial commits — Kalshi Trading Portfolio
# =============================================================================
# Initializes the repo and creates 13 logical commits, one per component.
# Idempotent re-runs are safe: if a commit's files are already committed,
# `git commit` becomes a no-op for that section.
#
# Run once from ~/Documents: bash git_init_commits.sh
# =============================================================================

set -u
cd ~/Documents

# Clear any stale lock from sandbox-side attempts
rm -f .git/index.lock

# Init (no-op if .git/ already exists)
if [ ! -d .git ]; then
    git init -b main
fi

# Configure local identity
git config user.email "emery.stevenr@gmail.com"
git config user.name "Steve Emery"

# Enable nullglob so missing patterns don't error
shopt -s nullglob

commit_q() {
    # commit only if there's something staged
    if git diff --cached --quiet; then
        echo "  (nothing to commit for: $1)"
    else
        git commit -q -m "$1" -m "$2"
        echo "  committed: $1"
    fi
}

echo "==> C1: .gitignore + .dockerignore"
git add .gitignore .dockerignore
commit_q "chore: .gitignore and .dockerignore" "Exclude engine runtime data (~19GB across terminal*_data), logs, stdout/stderr captures, secrets (.odds_api_key, kalshi_private_key.pem, .env), trade ledger, scheduler runtime state, OS/editor junk, and non-project folders. Keep .py/.sh/.md/.plist/Dockerfile/fly.toml/engines.json tracked."

echo "==> C2: project docs (handoffs, specs, audits, operating principles)"
git add HANDOFF_*.md AUDIT_*.md DEPLOY_ENGINE3.md T8_VERDICT_*.md \
    KALSHI_PROJECT_STATE.md OPERATING_FILE.md SESSION_STATE.md \
    terminal*_spec.md terminal*_phase*.md terminal*_handoff*.md \
    terminal*_data_schema.md terminal*_ecmwf*.md t6_status_*.md \
    calibration_trend.md sgo_*.md polymarket_handoff*.md polymarket_recon_*.md \
    session_continuation_*.md terminal2_audit_*.md terminal3_survey_*.md \
    thesis_review_*.md entropy_calibration_*.md
commit_q "docs: portfolio operating principles, session handoffs, engine specs" "- HANDOFF_2026-05-04 through HANDOFF_2026-05-16_session6 (10 handoffs). Each documents portfolio state, decisions locked, lessons learned, pending actions, direction for next session.
- OPERATING_FILE.md, SESSION_STATE.md, KALSHI_PROJECT_STATE.md: operator preferences and portfolio strategic context.
- AUDIT_2026-05-09.md, DEPLOY_ENGINE3.md, T8_VERDICT_2026-05-09.md: mid-stream audit, T3 deploy plan, T8 NFP nowcast archive verdict.
- terminal{1,3b,3c,6,7}_spec.md: per-engine design specs.
- terminal1_phase1_report*.md: weather ensemble validation reports.
- entropy_calibration_*.md: daily entropy collapse calibration reports.
- polymarket_*.md, thesis_review_*.md: T4 polymarket recon (US-restricted, archived).
- sgo_*.md: SGO Pro \$299/mo trial backtest design + CANCEL verdict (session 5)."

echo "==> C3: portfolio infrastructure"
git add portfolio_scheduler.py portfolio_macro_concentration.py portfolio_freshness_watchdog.py \
    shadow_pnl_core.py shadow_dashboard.py \
    entropy_collapse_detector.py entropy_alert_helpers.py \
    config.py config_additions.py execution.py \
    scheduler_jobs.json
commit_q "infra: portfolio scheduler, macro cap, freshness, shadow PnL core" "- portfolio_scheduler.py: central cadence-driven scheduler for engine jobs (t1_nws_actuals, t3c_claims_data, t6_mlb_lines_puller, entropy_collapse_detector).
- portfolio_macro_concentration.py: 50% hard cap across macro engines (T3a+T3b+T3c). Exit code surfaces AT-CAP / OVER-CAP / BREACHED state.
- portfolio_freshness_watchdog.py: writes freshness_alarm.flag which traders read each cycle to force dry-run when data is stale.
- shadow_pnl_core.py: canonical compute_state for paper-trade ledger (open/close/annul/reshadow semantics).
- shadow_dashboard.py: portfolio-wide P&L view; applies T6 contamination filter (session-5 vegas-match fix).
- entropy_collapse_detector.py: 5-min cadence detector for market-distribution collapse signals.
- entropy_alert_helpers.py: shared helpers consumed by per-engine traders.
- config.py / config_additions.py / execution.py: shared configuration + execution primitives.
- scheduler_jobs.json: job roster + cadence definitions."

echo "==> C4: T1 weather ensemble (archived)"
git add terminal1_*.py t1_*.py
commit_q "engine T1: Kalshi Weather Ensemble (ARCHIVED 2026-05-16)" "NWS/MET weather ensemble vs Kalshi weather contracts (per-station highs/lows).
ARCHIVED session 6: 40% win rate, n=311 lifetime closes, mean -\$0.40 after fees. The 40-50¢ entry subset (reduced bankroll gating, session 4) did not produce defensible edge. Public forecast data offers no proprietary advantage. All 6 T1 daemons killed; engines.json T1 archived.

- terminal1_phase2_paper_trader.py: subset-gated paper trader
- terminal1_kalshi_logger.py / terminal1_model_pullers.py / terminal1_metar_taf_puller.py / terminal1_nws_actuals.py: data feeds
- terminal1_settlement_reconciler.py: close + P&L
- terminal1_ensemble_backtest.py, terminal1_fit_empirical_sigma.py: ensemble methodology + per-lead sigma fits
- terminal1_calibration_*.py, terminal1_station_bias.py: calibration studies
- terminal1_diagnose_logger.py, terminal1_fix_apr27_partial.py: ops repairs
- t1_edge_analysis.py, t1_settlement_analysis.py: post-mortem"

echo "==> C5: T2 catalyst book (archived)"
git add terminal2_*.py t2_*.py
commit_q "engine T2: Kalshi Catalyst Book (ARCHIVED 2026-05-09)" "Manual catalyst-driven shadow trades on political/news markets.
ARCHIVED 2026-05-09 (session 2): 0/8 settled losses (-\$161.48). Root cause: T5 thesis factory 'our_prob' was a price multiplier, not a prior. Phase 0 backtest was tautological. \$5K bankroll reallocated to T6.

- terminal2_catalyst.py: opener
- terminal2_daily_t5_picks.py: pipeline from T5 catalyst finder
- terminal2_thesis_backtest.py: phase 0 backtest (later identified as tautological)
- terminal2_reshadow.py / terminal2_mark_drift.py: re-shadow + drift studies
- terminal2_settlement_reconciler.py: close + P&L
- t2_archetype_calibration.py: archetype calibration"

echo "==> C6: T3 macro engines (Fed / CPI / Claims)"
git add terminal3_*.py terminal3a_*.py terminal3b_*.py terminal3c_*.py t3b_*.py
commit_q "engines T3a/T3b/T3c: macro nowcasts (Fed decisions, CPI, jobless claims)" "Three macro engines on Kalshi event markets:

T3a (Fed Decisions, KXFEDDECISION + KXFED):
- terminal3a_fed_scanner.py: scanner across exclusive series
- terminal3a_fomc_observer.py: FOMC ladder tracking
- terminal3a_shadow_executor.py: dutch-arb + near-miss detection
- terminal3a_settlement_reconciler.py: close + P&L
- Next FOMC: 2026-06-17. No opens to date.

T3b (CPI nowcast, KXCPIYOY):
- terminal3b_paper_trader.py: edge-based opener vs Cleveland Fed nowcast
- terminal3b_kalshi_logger.py: per-event 5-min snapshot logger
- terminal3b_nowcast_puller.py / terminal3b_nowcast_backfill.py: Cleveland Fed pull + historical backfill (151 target months, 651 nowcast/actual pairs)
- terminal3b_bls_actuals.py: BLS settled actuals
- terminal3b_settlement_reconciler.py: close + P&L (launchd KeepAlive)
- May 12 CPI: 3 NO positions lost (-\$14.01).

T3c (Initial Jobless Claims, KXJOBLESSCLAIMS):
- terminal3c_paper_trader.py: edge-based opener vs DOL/FRED
- terminal3c_kalshi_logger.py: 5-min snapshot
- terminal3c_claims_data.py: DOL/FRED claims pull
- terminal3c_settlement_reconciler.py: close + P&L
- terminal3c_stability_check.py: pre-print freshness check
- May 14 ICSA: 3 NO positions lost (-\$25.00).

Shared T3 infrastructure:
- terminal3_cleveland_fed_pull.py / terminal3_kalshi_macro_depth.py / terminal3_diag_orderbook.py
- t3b_settlement_analysis.py"

echo "==> C7: T6 MLB engine (thesis validator for sharp-sports vertical)"
git add terminal6_*.py mlb_*.py
commit_q "engine T6: Kalshi MLB Game Markets (THESIS VALIDATOR)" "Sharp Vegas (Pinnacle/DK/FD/MGM/Caesars consensus, de-vigged) vs Kalshi KXMLBGAME.
Active 2026-05-08, \$13K bankroll. Kelly + fees + floating bankroll + entropy gate.
Validation criterion: clean n=300 with positive lower-95 CI after fees.
Early-kill: n=200 with lower-95 < -\$0.50.
THESIS VALIDATOR for the entire sharp-sports vertical — T7 and T11/NFL gate on T6's outcome.

- terminal6_mlb_paper_trader.py: 30-min cycle trader. Vegas-match ticker-date narrowing (session-5 patch, CONFIRMED session 6: 8 clean fires, Δ=0.0h on every one).
- terminal6_mlb_kalshi_ws.py: WS market feed
- terminal6_mlb_kalshi_logger.py: snapshot logger
- terminal6_mlb_lines_puller.py: Odds API consensus pull (90 min cadence)
- terminal6_mlb_settlement_reconciler.py: close + P&L
- terminal6_dashboard.py / terminal6_milestone_check.py: dashboard + Monday milestone with contamination filter
- mlb_model.py, mlb_edge_tracker.py, mlb_scanner.py, mlb_futures_*.py, mlb_debug_dump.py: analysis utilities

Session 6 state: clean n=8, gate=ACCUMULATING, realized -\$609.32."

echo "==> C8: T7 NBA/NHL playoffs engine (architecture confirmation)"
git add terminal7_*.py t7_*.py
commit_q "engine T7: Kalshi NBA/NHL Playoffs Game 1-2 only (architecture confirmation)" "Same sharp-vs-Kalshi pattern as T6, restricted to Game 1 and Game 2 of each playoff series via event-title regex. Per-series cap=2 via synthetic team-pair series_id.
Active 2026-05-10, \$2K bankroll. Architecture-confirmation engine — runway-constrained (~12-16 G1/G2 games combined NBA+NHL through Finals).

- terminal7_paper_trader.py: 30-min cycle trader. Ticker-date narrowing ±18h (NBA/NHL tickers are date-only, anchored 19:30 ET) — same patch class as T6.
- terminal7_kalshi_ws.py / terminal7_kalshi_logger.py: WS feed + snapshot
- terminal7_lines_puller.py: Odds API consensus for basketball_nba + icehockey_nhl
- terminal7_settlement_reconciler.py: close + P&L
- terminal7_milestone_check.py: ARCHITECTURE_CONFIRMED gate (n≥5)
- t7_kalshi_market_probe.py: pre-deploy probe utility

Session 6 state: missed conf finals G1 window (40h Odds API outage). G2+ live."

echo "==> C9: T5 catalyst finder + Kalshi/MM screeners (T5 dormant)"
git add kalshi_thesis_factory.py kalshi_catalyst*.py kalshi_complement_survey.py \
    kalshi_shadow*.py kalshi_3way_survey.py kalshi_polymarket_scanner.py \
    correlated_scanner.py inequality_scanner.py political_scanner.py screener.py \
    find_venue_pairs.py mm_event_curator*.py paper_mm.py live_mm.py
commit_q "engine T5 + screeners: catalyst finder, market-making, cross-venue scanners" "T5 Catalyst Finder — scanner only, no capital deployed. T2 downstream archived 2026-05-09. Kept dormant pending decision to archive entirely.

- kalshi_thesis_factory.py: catalyst classifier (our_prob bug surfaced in T2 archive root cause)
- kalshi_catalyst*.py / kalshi_complement_survey.py / kalshi_shadow*.py: catalyst + complement market surveys, shadow trade simulation
- kalshi_3way_survey.py, kalshi_polymarket_scanner.py: cross-venue (Kalshi vs Polymarket) 3-way arb survey

Screeners:
- correlated_scanner.py, inequality_scanner.py: market-pair statistical scanners
- political_scanner.py: political contract scanner
- screener.py: general market screener
- find_venue_pairs.py: venue-mirror discovery

Market-making (paper/live):
- mm_event_curator.py, mm_event_curator_v4.py: event curator for MM universe
- paper_mm.py: paper-trade MM
- live_mm.py: live MM scaffold"

echo "==> C10: T4 Polymarket (archived, US-restricted)"
git add polymarket_*.py
commit_q "engine T4: Polymarket scaffolding (ARCHIVED, US-restricted)" "Polymarket integration scaffolding. ARCHIVED — US users cannot trade on Polymarket per CFTC. Reactivation-ready if CFTC reverses. Bankroll \$0, does not count toward concentration calculation.

- polymarket_parser.py / polymarket_parser_v2.py: market data parser
- polymarket_recon.py: reconnaissance utility
- polymarket_ws_probe.py: WS feed probe"

echo "==> C11: diagnostic + analysis scripts"
git add diag_*.py debug_events.py adp_nfp_*.py sgo_*.py
commit_q "diagnostics + analysis: ad-hoc utilities, SGO trial, ADP/NFP residual study" "Diagnostic / debug utilities used for incident investigation and pre-deploy validation:
- diag_book_endpoint.py, diag_kalshi.py, diag_kalshi_inspect.py, diag_kalshi_v2.py: Kalshi API/book diagnostics
- diag_spread_distribution.py, diag_tick_size.py: market microstructure diagnostics
- debug_events.py: event subscription debug

SGO Pro \$299/mo trial deliverables (session 5, DECISION = CANCEL):
- sgo_d_pull.py: 30-day chunked open/close pull (403 finalized MLB games)
- sgo_d_analyze.py: three-signal analysis (Pinnacle move vs retail, lead time, alignment)
- Decision rule: subscribe if Pinnacle leads retail by ≥3pp on >20% games for ≥30min. Failed decisively: Pinnacle moves SMALLER than retail by median 3.09pp; 4-min timing lead (sub-spread).

ADP/NFP residual study (T8 NFP nowcast, archived):
- adp_nfp_extended_analysis.py, adp_nfp_residual_study.py: residual correlation study that informed the T8 archive decision."

echo "==> C12: deploy / install / restart scripts"
git add deploy_*.sh redeploy_*.sh relaunch_*.sh install_*.sh \
    portfolio_status.sh terminal1_status.sh terminal1_backfill.sh \
    start_t3c_watch.sh stop_t3c_watch.sh
commit_q "ops: deploy, redeploy, install, status, relaunch scripts" "Operator-side shell scripts for daemon lifecycle:

Deploy / redeploy (idempotent — kills existing, restarts fresh session-detached via double-fork + setsid):
- deploy_t6.sh, deploy_t6_shadow.sh, deploy_t6_ws.sh: T6 deploy variants
- redeploy_t6_all.sh: full T6 stack restart
- redeploy_t7_all.sh: full T7 stack restart
- redeploy_entropy_phase1.sh: entropy detector restart
- relaunch_t3b_logger.sh: T3b kalshi logger relaunch (nohup+caffeinate+setsid fallback after launchd plist install hit exit 78; queued for proper plist before mid-June CPI)

Install (LaunchAgent registration helpers):
- install_freshness_watchdog.sh
- install_t3b_bls_actuals.sh
- install_t3c_claims_data.sh

Status / monitoring:
- portfolio_status.sh: full portfolio daemon + scheduler + cap snapshot
  (KNOWN ISSUE: does not list T6 or T7 daemons — H18 from session 6, ps filter needs update)
- terminal1_status.sh: T1-only status
- terminal1_backfill.sh: T1 historical backfill helper
- start_t3c_watch.sh / stop_t3c_watch.sh: T3c watch process control"

echo "==> C13: launchd LaunchAgent plists"
git add com.*.plist
commit_q "launchd: LaunchAgent plists for daemonized engines" "macOS launchd plist definitions:

- com.t3b.settlement-reconciler.plist: T3b reconciler with KeepAlive=SuccessfulExit:false, ThrottleInterval=30 (currently pid 40443, healthy)
- com.t3b.bls-actuals.plist: T3b BLS actuals daily pull
- com.terminal1.nws-actuals.plist: T1 NWS actuals (now no-op after T1 archive, should be removed)
- com.terminal3c.claims-data.plist: T3c DOL/FRED claims data
- com.portfolio.freshness-watchdog.plist: portfolio-wide freshness check
- com.steve.polymarket-sniper.plist: T4 polymarket sniper (archived)

NOT YET ADDED (queued for session 7):
- com.t3b.kalshi-logger.plist: install hit exit 78 in session 6 across three approaches (heredoc, sed-clone of working reconciler plist, sudo bootout + modern bootstrap). Likely macOS Background Items approval (Ventura+). T3b kalshi logger currently running via nohup detach (relaunch_t3b_logger.sh). MUST BE LAUNCHD-MANAGED BEFORE MID-JUNE CPI."

echo "==> C14: engine config (engines.json) + Docker/Fly deployment artifacts"
git add shadow_pnl/engines.json Dockerfile fly.toml
commit_q "config: engines.json roster + Docker/Fly deployment artifacts" "- shadow_pnl/engines.json: engine roster with bankroll, mode, category, active flag, notes. Source of truth for portfolio_macro_concentration cap calc. Session-6 state: T1 archived (\$0), T2/T4 archived (\$0), T3a/T3b/T3c \$5K each, T5 infrastructure (\$0), T6 \$13K (bumped from \$12K to restore 50.0% macro cap after T1 archive), T7 \$2K. Total \$30K deployed, macro 50.0% AT CAP.
- Dockerfile + fly.toml: Fly.io deployment artifacts (container image + app config). Used by an earlier deployment iteration; not currently in production."

echo
echo "==> FINAL: commit log"
git log --oneline

echo
echo "==> tracked file count"
git ls-files | wc -l
echo "==> untracked file count (should be 0)"
git ls-files --others --exclude-standard | wc -l
