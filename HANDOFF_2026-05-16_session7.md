# HANDOFF — Session 7 (2026-05-16)

## 1. TL;DR

- Macro family killed. T3a paused indefinite, T3b + T3c archived. $10K reallocated to T6 (now $18K).
- engines.json promoted to **single source of truth**. Watchdog, scheduler, and dashboard all derive their active-engine list from it dynamically. Flip `active: false` → engine disappears from all monitoring with no other edits.
- Freshness watchdog scrubbed of all archived-engine ghosts (T1, T2, T3b, T3c). T6 dry-run gate is open. T7 added with sport-runway-aware thresholds.
- New shadow_dashboard.py written from scratch — rich library, 4 Hz repaint, side-by-side T6/T7 cards with identical 9-row Table layout, shimmering progress bar on the T6 validator gate, pulsing status dots, live UTC clock.
- T3b launchd plist abandoned. 5-attempt diagnostic isolated it conclusively to BTM Label-database corruption on `com.t3b.kalshi-logger` — byte-clone of working reconciler with only Label swapped also hits exit 78. Engine archived; problem boxed for any future revival.

## 2. Decisions locked

**T6 and T7 are the only active trading engines. Everything else is archived.**

No exceptions without explicit operator approval. Specifically: do NOT add engines back to engines.json `active: true` to "test something." If you need a placeholder for cap accounting (e.g. T3a's $5K macro slot), use `mode: paused` + `monitoring.show_in_dashboard: false` — that pattern is in place.

The single-source-of-truth refactor exists so any future archive flip is a one-line change. Do not bypass it.

## 3. Engine states

**T6 — Kalshi MLB Game Markets**
- Bankroll $18,000 (was $13K; absorbed $5K T3b + $5K T3c archive recycle)
- Clean realized **-$609.32** (n=8 closes, 2W/6L) — from terminal6_milestone_check, NOT raw ledger
- Raw ledger contains 37 vegas-match-contaminated closes (+$3,690.91) — excluded, audit-only
- Gate: **ACCUMULATING** n=8/300, target validator gate
- Daemon: ws_logger.log ticking every 5s under Kalshi WS feed
- Freshness watchdog fixed; **dry-run gate unblocked** — trader will actually take signals on next fire
- Kelly cap unchanged at 5% → max single position $900; 50% exposure cap → $9K

**T7 — Kalshi NBA/NHL Playoffs (Game 1-2 only)**
- Bankroll $2,000 (architecture-confirmation, not edge validator)
- 0 fires lifetime — awaiting Game 1-2 windows in upcoming series
- NBA Finals Game 1 ≈ June 5; NHL Stanley Cup Final Game 1 ≈ June 7
- Daemon: ws_logger.log alive; lines_puller running
- Per-series cap=2 via synthetic team-pair series_id; liquidity floor OI<100 AND vol_24h<50

## 4. Dashboard

`python3 ~/Documents/shadow_dashboard.py`

- Live single screen, rich library, 4 Hz repaint cadence, 30 s data re-gather
- Side-by-side T6 + T7 cards with identical 9-row Table layout
- Reads engines.json dynamically — `monitoring.show_in_dashboard: true` filter
- Hero shows: portfolio realized, starting capital ($20K = T6+T7 only), macro cap %, Odds API credits, live UTC clock, status dots
- T6 gate as shimmering gradient progress bar; T7 gate as ACTIVE / awaiting-close subline
- Alerts panel only shows when something needs attention; collapses to `✓ All systems nominal` otherwise
- `q` or `Ctrl+C` to exit

Smoke test (no rich, JSON dump):
`python3 ~/Documents/shadow_dashboard.py --once`

## 5. Known open items

**T3b launchd plist exit 78 — boxed, not solved.**

Five session-7 attempts isolated this to **BTM Label-database corruption** on `com.t3b.kalshi-logger`. Diagnostic chain:

1. macOS Background Items approval — RULED OUT (BTM disposition log: `enabled, allowed, notified`)
2. `caffeinate` wrap — RULED OUT (python3-direct also exit 78)
3. Process collision — RULED OUT (killed old nohup, still exit 78)
4. File permissions — RULED OUT (.out/.err writable)
5. Byte-clone of working reconciler plist — STILL exit 78 with only Label changed

The Label `com.t3b.kalshi-logger` is poisoned in BTM's database. Session 6's three failed install attempts corrupted the entry. Escape hatches: use a new Label (e.g. `com.t3b.kalshi-logger-v2`) or run `sfltool resetbtm` + reboot.

**Currently running** via nohup detach (`relaunch_t3b_logger.sh`, pid TTY=??). Will not survive reboot.

**Not urgent — T3b is archived.** Daemon serves no engine. The diagnostic is preserved in this handoff and in T3b's archive notes in engines.json so it's recoverable if the engine is ever revived.

## 6. Lessons

**engines.json is the single source of truth.** Watchdog, scheduler, and dashboard all derive the active-engine list from it dynamically. Archiving an engine removes it from all monitoring automatically. Pre-session-7, three separate hardcoded engine lists drifted — that's what fired the T1-ghost freshness alarm that forced T6 into dry-run for hours.

Corollary: when adding a new engine, the only file edits required are (a) add the block to engines.json with a `monitoring` section and (b) add scheduler jobs to scheduler_jobs.json with an `engine:` field. Watchdog and dashboard pick it up automatically.

Macro family was a 3-engine sunk cost. T3a/T3b/T3c collectively earned ~$0 useful signal across two months. Cutting them was overdue. **An engine that hasn't earned its operational complexity should be killed, not debugged.** Time spent on the T3b BTM hairball was time NOT spent on T6.

Exit 78 on a LaunchAgent plist is **not** necessarily a content error. Byte-identical clones can fail. BTM Label database can poison itself. If exit 78 reappears: capture `log show --predicate 'eventMessage CONTAINS "<label>"' --last 5m --info` BEFORE retrying. Don't burn cycles on plist content edits without that log first.

The freshness watchdog forcing dry-run on T6 because of stale T1 logs is a textbook case of "alarm without action": the alarm fired correctly, but the action it triggered (dry-run) was destructive to the active engine. Always check the action's blast radius when an alarm fires.

## 7. Next session priorities

**Let T6 run.** Do NOT touch engine logic. Check milestone output. Only intervene if the dashboard shows an alert.

If T6 is firing and accumulating closes: continue toward n=300.
If T6 hits n=200 with lower_95 < -$0.50: archive immediately, do not wait for n=300 (the early-kill gate). Reallocate $18K.
If T7 fires its first G1 in playoffs: verify the trade structure (per-series cap, liquidity floor) without touching code.

## 8. First commands

```bash
git log --oneline | head -10
python3 ~/Documents/shadow_dashboard.py
tail -20 ~/Documents/terminal6_data/paper_trader.log
```
