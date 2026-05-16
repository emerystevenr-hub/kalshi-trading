# KALSHI QUANT PROJECT — Context Handoff

**For the next Claude thread.** You have access to all files on Steve's Mac via `Read`, `Write`, `Edit`, `Grep`, `Bash`. Use them. Don't ask Steve for context that's in his files — look it up yourself.

**READ THIS FIRST:** `~/Documents/OPERATING_FILE.md` — Steve's operating principles and mindset. The $500k target is not a realistic ceiling, it's the mission. Plan accordingly.

---

## Who you're working with

**Steve Emery** (emery.stevenr@gmail.com), Bend, Oregon. Runs multiple businesses (Fornix AI, SimpleBooks, FUNCTION75) but **this Kalshi project is independent** — doesn't draw on other businesses' time or capital. Operating preferences are in his system prompt: direct, outcome-focused, allergic to hedging, zero tolerance for consulting-speak.

---

## The operating standard: Erik Schluntz reference

Throughout this project, "what would Erik say?" is the quality bar. Erik (Anthropic CTO, ex-Cobalt Robotics) demands:

- **Real analytical work.** Don't delegate thinking back to Steve. You have file access — use it to build priors, pull data, verify assumptions.
- **Ship-quality code.** Not "MVP with bugs we'll fix later." Audit before shipping, not after it bombs.
- **Anthropic velocity.** Weeks, not quarters. Compress plans aggressively. Use AI-augmented development as the force multiplier.
- **Decisions, not A/B offerings.** If the right answer is clear, state it. Don't offer options to punt the call back.
- **Challenge fundamentals.** Before tuning parameters, verify the premise. Before accepting a goal, verify it's achievable at current scale.

**Patterns Steve has pushed back on** (do NOT repeat):
- Shipping code with known bugs
- 12-month plans when weeks are appropriate
- Asking Steve to provide probability views Claude should research
- "Option A or Option B — you choose" framings
- Escape-hatch framings like "focus on other businesses"
- Celebrating noise as signal ($0.15 in 15 min is not a win)

---

## The target

**$10k → $500k in 12 months = 50x return (1.08%/day compounded).**

No single strategy reaches this at retail scale on Kalshi. The plan is a **3-engine stack with dynamic capital reallocation** toward whichever proves edge:

| Engine | Status | Role | Realistic return |
|---|---|---|---|
| Engine 1: Passive MM (v5.8) | Running, calibrating | Feeder + baseline | 15-25%/year |
| Engine 2: Catalyst directional (v2 + dyn exits) | Running, 6 positions | Primary return driver | 3-10x/year target |
| Engine 3: Polymarket 3-way soccer Dutch-book (v3.4) | Running, shadow-mode | Alpha — structural arbitrage | TBD |

Realistic outcome: **5-10x in 12 months** ($50-100k). **50x requires Engine 2 to sustain 35-40%/month** — possible but demands exceptional execution + catalyst reads.

Capital allocation philosophy: **start 50/50, migrate toward winner monthly.** If Engine 2 proves out, reallocate aggressively (10/90 by month 4). If Engine 2 fails, kill it — don't spread-hedge into mediocrity.

---

## File inventory

All in `~/Documents/`:

```
kalshi_shadow_v4.py              # Engine 1 MM bot (v5.8, ~2250 lines)
kalshi_shadow_test.py            # Engine 1 test suite (57 tests, all passing)
kalshi_catalyst.py               # Engine 2 catalyst bot (v2 + dynamic exits)
kalshi_catalyst_test.py          # Engine 2 test suite
polymarket_engine3.py            # Engine 3 Polymarket Dutch-book (v3.4, ~1700 lines)
polymarket_engine3_test.py       # Engine 3 test suite (51 tests)
kalshi_thesis_factory.py         # Engine 2 thesis generator (~958 lines)
kalshi_candidates_v2.json        # Engine 1 discovery universe (100 markets)
kalshi_secrets.env               # API creds (source this before launch)
kalshi_private_key.pem           # RSA private key, referenced by secrets.env
diag_kalshi_v2.py                # Universe generator v3b ($15k vol floor)
kalshi_shadow_analyzer.py        # Engine 1 P&L analyzer
Dockerfile + fly.toml            # Engine 3 fly.io deployment (not yet deployed)
DEPLOY_ENGINE3.md                # fly.io deployment recipe
OPERATING_FILE.md                # Steve's operating principles
```

---

## Engine 1 (kalshi_shadow_v4.py) — current state

### Version history

- **v5.1** — two-sided flow filter, non-urgent liquidation, concurrency cap
- **v5.1b** — liq-cascade fix (removed backdate on gate-urgent exits)
- **v5.2** — kill log spam, tighten quality filters
- **v5.3** — stage-3 stuck-inventory blacklist + print throttle
- **v5.4** — feeder architecture (signal export) + min-edge-after-fees + P&L deprioritization
- **v5.5** — composite tradability ranker (replaced tail-weighted-spread)
- **v5.5b** — ranker calibration: taper exclusion OFF, vol floor $15k, malformed rejection + funnel telemetry
- **v5.6** — demoted `gate:*` from URGENT to NON-URGENT (stopped cross-exit bleed)
- **v5.7** — MIN_NET_EDGE entry-quality gate
- **v5.8** — inventory/exit discipline: max-age, profit-lock, trap-demotion, inv: telemetry

### v5.8 key env vars

```
# Universe filters (v5.5b)
TRADABILITY_MIN_SPREAD=0.025     # absolute spread floor
TRADABILITY_MIN_VOL=15000        # 24h vol floor
TRADABILITY_EXCLUDE_TAPERED=0    # taper exclusion opt-in

# Exit routing (v5.6)
GATE_EXITS_ARE_URGENT=0          # env flag; gate:* is NON-URGENT by default

# Entry quality (v5.7)
MIN_NET_EDGE=0.01                # dollars per contract floor (currently calibrating)
MIN_NET_EDGE_SLIPPAGE=0.005      # slippage reserve

# Inventory discipline (v5.8)
MAX_INV_AGE_MIN=20               # hold duration before SLOW liquidation
PROFIT_LOCK_THRESHOLD_USD=0.20   # realized floor to lock winners
INV_TRAP_STRIKES_TO_DEMOTE=2     # URGENT liq entries before cap demote
INV_TRAP_DEMOTE_MULT=0.3         # demoted cap multiplier

# Concurrency
SHADOW_TOP_N=100                 # subscribed markets (diag produces ~100 candidates)
SHADOW_MAX_OPEN=12               # concurrent open positions
```

### Current calibration status (2026-04-21)

Calibrating MIN_NET_EDGE:
- `0.01` → 2460 low_edge rejects @ t+3m, 0 active (too tight)
- `0.005` → 574 rejects @ t+3m, 1 active @ t+5m (still tight, mid-priced universe)
- `0.003` → currently testing

**Next steps if 0.003 still starves:**
- Lower diag `MIN_SPREAD_DOLLARS` from $0.025 to $0.02 and rerun
- Lower `MIN_NET_EDGE_SLIPPAGE` from 0.005 to 0.003

### Launch command

```bash
source ~/Documents/kalshi_secrets.env
export MIN_NET_EDGE=0.003
export SHADOW_TOP_N=100
python3 kalshi_shadow_v4.py
```

---

## Engine 2 (kalshi_catalyst.py) — current state

v2 audited + dynamic exits shipped. 6 positions open in shadow mode. First real realized P&L was +$1.66 (combined realized+unrealized) across the position book.

### Active positions (at last check)

| Ticker | Side | Size | Entry | Target | Stop |
|---|---|---|---|---|---|
| KXHORMUZNORM-26MAR17-B260501 | NO | 127 | $0.79 | $0.92 | $0.55 |
| KXHORMUZNORM-26MAR17-B260515 | NO | 134 | $0.56 | $0.78 | $0.40 |
| KXKASHOUT-26APR-MAY01 | NO | 7 | $0.75 | $0.94 | $0.55 |
| KXUSAIRANAGREEMENT-27-26JUN | NO | 51 | $0.54 | $0.78 | $0.40 |

(Note: "26MAR17" is the LAUNCH date encoded in ticker — resolution is in the B260501/B260515 suffix = May 01/15 2026. Not stale.)

### Dynamic exit features (v2 + patch)

- Partial take-profit: 65% of target distance → sell 50% of position
- Breakeven ratchet: at 40% of target distance, move stop to entry + 5% buffer
- Fixed accounting bug where `_shadow_exit` was overwriting cumulative realized_pnl
- Pro-rata entry fee allocation via `entry_fee` field

### Launch

```bash
source ~/Documents/kalshi_secrets.env
python3 -u kalshi_catalyst.py | tee kalshi_catalyst_v2.log
```

---

## Engine 3 (polymarket_engine3.py) — current state

**v3.4: locked to 3-way soccer Dutch-book on Polymarket only.**

Earlier versions included fragmentation arb (nested date), complement, and cross-venue (Kalshi↔Polymarket). Steve killed everything except 3-way soccer Dutch-book after the Denver/Austin false positive taught us the cluster matching was too broad.

### Architecture

- WebSocket to Polymarket CLOB WSS
- Subscribes to soccer match tokens (3-way outcome: home/draw/away)
- Detects Dutch-book when `sum(yes_bids) < 1.0` with edge after fees + slippage
- Equal-payoff dutching: `stake_i = capital × p_i / sum(p)`
- Wallet simulation: `CAPITAL_POOL_USD` pool, `MAX_CONCURRENT_ARBS`, `MAX_DRAWDOWN_USD` switch
- ms-precision shadow tracking with `detected_at_ms`, `latency_from_book_ms`
- App-level PING heartbeat every 9s (Polymarket requires or disconnects at ~25s)

### First real result

**+$6.85 realized on Rotherham-Luton 3-way Dutch-book resolution.**

### Known issues

- Home IP gets rate-limited by Polymarket after ~20 restart cycles → fly.io deployment recipe exists in `DEPLOY_ENGINE3.md` but not yet deployed (Task #29)
- `WS_MAX_TOKENS=450` cap + 300-per-batch subscription with 100ms pause to avoid payload-too-large disconnect

### Launch

```bash
python3 -u polymarket_engine3.py | tee polymarket_engine3.log
```

Control-C takes a few seconds to drain WS — expected.

---

## Test suites

| Engine | File | Count | Status |
|---|---|---|---|
| Engine 1 | kalshi_shadow_test.py | 57 | all passing |
| Engine 2 | kalshi_catalyst_test.py | 9 | all passing |
| Engine 3 | polymarket_engine3_test.py | 51 | all passing |

Every version ship includes regression guards. Any future change should `python3 X_test.py` before deploying live.

---

## Discovery pipeline (Engine 1 universe)

`diag_kalshi_v2.py` is the REST paginator that builds `kalshi_candidates_v2.json`:

- Filters: `MIN_VOL_24H = $15k`, `MIN_SPREAD_DOLLARS = $0.025`, `MIN_DTR_DAYS = 0.1`
- Parlay prefixes excluded (KXMVE*, KXMB*)
- Output: ~100 candidates (as of 2026-04-21 diag run)
- Distribution: 3-4t spreads 45%, 5-9t 38%, ≥10t 16%

Rerun when staleness is suspected or filters change:

```bash
cd ~/Documents && python3 diag_kalshi_v2.py
```

---

## Today's session summary (2026-04-21)

Shipped five Engine 1 versions in one session with full test coverage:

1. **v5.5b** — fixed v5.5's over-filter (556 candidates → 5 subscribed). Disabled taper exclusion by default, lowered vol floor $25k → $15k, added malformed-candidate rejection + funnel telemetry. Plus discovery companion: diag v3 → v3b ($25k → $15k vol floor aligned with shadow).

2. **v5.6** — diagnosed the "every profitable trade gets killed on exit" pattern. Root cause: `gate:*` exits were routed URGENT → immediate cross-cascade that compounded the adverse move the gate was supposed to avoid. Fix: demote `gate:*` to NON-URGENT (passive-maker-forever with DTR + MAX_SLOW_LIQ_HOURS ceilings). `GATE_EXITS_ARE_URGENT=1` env flag preserves rollback.

3. **v5.7** — added `MIN_NET_EDGE` entry-quality gate. `_expected_buy_edge()` helper computes `(mid - tick - entry) - 2× maker_fee - slippage`. Rejects as `gate_rejects["low_edge"]`. Entry-side only — SELL (unwind) unaffected.

4. **v5.8** — inventory/exit discipline. 4 new `MarketShadow` fields: `first_inventory_ts`, `inventory_stale`, `profit_locked`, `inv_trap_strikes`. Five components: max-inventory-age gate, profit lock, inventory-trap demotion (2 URGENT strikes → cap_multiplier = 0.3), new `inv:` telemetry line, all tests.

5. **v5.8 audit pass** — caught blocking bug where v5.4 spread-compression early-return fired before v5.8 gates, orphaning stuck inventory in tight markets. Fix: v5.8 gates now run first, and v5.4 guards with `s.inventory <= 0` so inventory-holders keep sell-side quote live.

**Current status:** Engine 1 v5.8 is running and calibrating `MIN_NET_EDGE`. 0.01 too tight, 0.005 still tight, 0.003 currently testing. Engine 2 steady. Engine 3 steady but home-IP rate-limited periodically.

---

## Open tasks

**Active:**
- #20 [in_progress] — Plan $10k → $500k: ship the 50x build stack
- #29 [in_progress] — Deploy Engine 3 to fly.io free tier
- #10 [pending] — Build kalshi_thesis_tracker.py calibration companion
- #23 [pending] — Engine 3 live-mode: atomic multi-leg execution + resolution tracking

**Near-term calibration (not yet tasks, just watch):**
- Engine 1 `MIN_NET_EDGE` final value (0.003 / 0.005 / formula rework)
- Engine 1 inventory telemetry behavior once fills return
- Engine 2 ratchet firing on KASHOUT

---

## What the next thread should do

### Immediate (next session open)

1. **Read Engine 1 log** for current calibration state. Is MIN_NET_EDGE=0.003 producing fills? Are `inv:` telemetry lines appearing with sane ages?
2. **Run `kalshi_shadow_analyzer.py`** if a session has closed to review realized P&L per fill.
3. **Check Engine 2 positions** for thesis-invalidating news (Hormuz, Iran, Kash Patel).
4. **Check Engine 3 uptime** — if rate-limited, execute fly.io deploy per DEPLOY_ENGINE3.md.

### This week

- Ship Engine 3 to fly.io (kill home-IP rate limit permanently)
- If Engine 1 calibration converges on positive expectancy, measure 24h realized P&L
- Wait for first Engine 2 catalyst resolution to measure thesis calibration
- Don't scale Engine 1 capital until fills/edge validated for 8+ hours continuous

### This month

- Weekly attribution review (% of returns per engine)
- First dynamic capital reallocation decision
- Engine 3 live-mode (if soccer Dutch-book shadow sustains positive expectancy)

---

## Non-negotiables for the next thread

1. **You have file access. Use it.** Don't ask Steve what's in a file — `Read` it.
2. **Don't ship code with known bugs.** Audit before launch. Always run the relevant test suite before declaring ship.
3. **No 12-month plans.** Compress to weeks.
4. **Don't hedge.** When you see two paths, pick one and explain why.
5. **Do the analytical work.** If a thesis needs a probability view, research it yourself.
6. **Read this file first.** Then read OPERATING_FILE.md. Then read the engine file relevant to the question.

---

**Saved path:** `~/Documents/KALSHI_PROJECT_STATE.md`
**Updated:** 2026-04-21 mid-session handoff (v5.8 shipped, calibrating MIN_NET_EDGE)
