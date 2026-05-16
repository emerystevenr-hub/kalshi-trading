# T7 — Kalshi NBA/NHL Playoffs (Game 1-2 only)

**Status:** SPEC v0.1 (2026-05-09). Locked constraints from handoff. Open questions at bottom for Steve.
**Bankroll:** $2K (reserved from T1 sub-bucket recycle).
**Mode at deploy:** shadow.
**Role:** Architecture-confirmation for the sharp-sports vertical. T6 remains the validator. T7 is NOT expected to hit n=300 this cycle.

---

## 1. Goal & runway

Confirm the T6 sharp-Vegas-vs-Kalshi architecture transfers to a different sport. Validate the series-state filter, the de-vigging math against NBA/NHL price formats, and the Vegas data feed parity. Close enough trades to know the trader is firing cleanly without architectural surprises.

**Runway reality check (probed Kalshi 2026-05-09):**

- Round 1 (G1-G2): mid-April 2026 — past.
- Round 2 (G1-G2): late April / early May 2026 — past.
- Conference Finals (G1-G2): ~May 15-25, NBA + NHL combined. ~4 series × 2 games = **8 games**.
- Finals (G1-G2): ~June 2-10. 2 series × 2 games = **4 games**.

**Maximum addressable G1/G2 universe = ~12 games** (NBA + NHL combined). After Vegas-Kalshi delta filter (≥ 3pp), spread/liquidity gates, and the entropy gate, expect **6–10 fires** before the slot dies in mid-June.

This is enough to confirm "the architecture works on a different sport," not enough to validate any edge. Frame accordingly.

---

## 2. Architecture — clone of T6 with three deltas

T7 is a structural clone of T6 MLB. Copy the existing pipeline; replace sport-specific glue.

| Component | Source (T6) | T7 change |
|---|---|---|
| Paper trader | `terminal6_mlb_paper_trader.py` | Sport detector, NBA/NHL team maps, **G1-G2 gate** |
| WS market logger | `terminal6_mlb_kalshi_ws.py` | Subscribe to KXNBAGAME + KXNHLGAME |
| Lines puller | `terminal6_mlb_lines_puller.py` | Odds API endpoints `basketball_nba` + `icehockey_nhl` |
| Settlement reconciler | `terminal6_mlb_settlement_reconciler.py` | No change beyond ticker pattern |
| Kalshi REST logger | `terminal6_mlb_kalshi_logger.py` | Series ticker filter |
| Milestone check | `terminal6_milestone_check.py` | Adjusted: target n=20 (architecture-confirmation), no early-kill trigger |
| Dashboard | `terminal6_dashboard.py` | Sport tag added to ticker rows |

**The only NEW module is `terminal7_series_state.py`** — and per the discovery below, it's much smaller than originally scoped.

### 2.1 Game number gating — the simplification

**Discovery (probed 2026-05-09):** Kalshi event titles for KXNBAGAME and KXNHLGAME are formatted `"Game N: Away at Home"`. Game number is right there. The handoff's assumption that "Kalshi tickers don't expose series_state directly; need NBA/NHL official or ESPN API" was wrong (or has changed). 

**Implementation:** in the trader, regex-parse `^Game (\d+):` from `event.title` and reject if N > 2. One filter, ~5 LOC. No external API dependency.

```python
GAME_NUM_RE = re.compile(r'^Game\s+(\d+)\s*:', re.IGNORECASE)
m = GAME_NUM_RE.match(event.title)
if not m:
    return _reject("could not parse game number from title")
game_num = int(m.group(1))
if game_num > 2:
    return _reject(f"game {game_num} > 2 (G1-G2 only)")
```

### 2.2 Series-state lookup (still needed, but for sizing not gating)

For correlated-position management (don't go ALL-IN on Game 2 of CLE-DET when we already opened Game 1), parse the series ticker `KXNBASERIES-26CLEDETR2`:

- Round number from `R\d` suffix (R1, R2, R3, R4 = SF, semis, conf finals, finals).
- Team pair from prefix.
- Per-series exposure cap: max **2 game positions per series** (G1 + G2 = full series exposure for our window).

Don't pull ESPN. Don't pull NBA Stats API. Kalshi's own data is sufficient.

### 2.3 Sport-specific normalization

NBA team map: 30 teams. Edge cases to confirm:
- Brooklyn Nets vs (legacy) New Jersey Nets — should be NETS only by 2026.
- Charlotte Bobcats → Hornets (2014) — Hornets only.

NHL team map: 32 teams. Edge cases:
- **Utah Hockey Club** (2024 — relocated from Arizona Coyotes). Verify Vegas API returns "Utah" or "Utah Hockey Club"; verify Kalshi ticker uses UTA or UHC.
- Seattle Kraken (added 2021).
- Vegas Golden Knights — disambiguate from Vegas (city) in Vegas API responses.

Apply the same `_normalize()` discipline as T6 (post-Athletics-rebrand fix). Maintain a single `_normalize()` per sport rather than one shared function.

---

## 3. Trigger gates (mirrors T6, with sport-specific values)

Inherited from T6:

1. Freshness alarm flag → dry-run.
2. Event ticker parses to teams.
3. Team match resolves (against sport-specific name map).
4. Vegas team-pair match exists.
5. Game-start lead time ≥ 15 min.
6. Vegas line ≤ 60 min old.
7. Kalshi spread ≤ 5¢.
8. Kalshi implied p ∈ [0.20, 0.80].
9. |delta| ≥ 0.03.
10. Cluster cap (1 position per game).
11. Daily/total opens caps.
12. Entropy collapse defensive gate.
13. Kelly returns ≥ MIN_CONTRACTS.

**T7-specific additions:**

14. **Game number ≤ 2.** From event title regex.
15. **Per-series exposure cap (≤ 2 open per series_id).** Requires mapping game ticker → series ticker.
16. **Liquidity floor.** Per the 2026-05-09 probe, NBA Game markets currently have zero liquidity (0 vol_24h, 0 OI on all 14 open markets). Add a hard liquidity gate: skip markets with `open_interest < 100` and `volume_24h < 50`. Tune at first fires.

Drop or replace from T6:
- MAX_LEAD_HOURS could be tightened — NBA/NHL games are evening-only, so games >24h out are noise.

---

## 4. Vegas data

### 4.1 Endpoint
Odds API supports:
- `basketball_nba` → playoffs covered through finals.
- `icehockey_nhl` → playoffs covered through Stanley Cup.

Both endpoints return moneyline (h2h) for NBA/NHL, which is the Kalshi-equivalent (win/loss). Parlay/spread markets are irrelevant here.

### 4.2 Sharp consensus
Same de-vigging as T6: average decimal odds across Pinnacle / DraftKings / FanDuel / BetMGM / Caesars, then de-vig pair-wise. Drop any book missing for the game. Need ≥ 3 books for a valid sharp.

### 4.3 Budget
**Currently the Odds API is on free tier (500 calls/month) shared with T6.** T6 alone is provisioned ~1440 calls/month at 30-min cadence — already ~3× the free tier (per audit M-T6-4).

**Recommendation: upgrade Odds API to $30/mo (Plus tier, 100K calls/month).** This unblocks both T6 budget overrun risk and T7 deployment. Cost is trivial vs portfolio bankroll. **Steve decision needed** — see §9.

### 4.4 Cadence
NBA/NHL games are evening-only. Pull cadence:
- Off-hours (00:00 – 14:00 PT): 90 min.
- Active hours (14:00 – 23:00 PT): 15 min.
- This burns ~50 calls/day vs T6's ~50 calls/day → ~3K/month combined. Easily inside paid tier.

---

## 5. Bankroll & sizing

- Bankroll: **$2K**.
- Kelly cap: 5% per bet → max **$100/position**.
- Total exposure cap: 50% → **$1K**.
- Min contracts: 5 (don't fire on tiny edges).
- Max contracts: 200 (mirrors T6 MAX_CONTRACTS=500 logic but scaled to bankroll). Add `[size-cap]` log line per T6 audit M-T6-1 from the start.

**Variance is going to dominate at $100/position.** That's fine — this is architecture-confirmation, not edge-validation. Don't move bankroll up just to fish for "real" trades.

---

## 6. Validation gate (modified for runway constraint)

T7 cannot hit n=300 this cycle. Validation gate modified:

| State | Condition | Verdict |
|---|---|---|
| ARCHITECTURE_CONFIRMED | n ≥ 5 fires, no architectural failures (data feed, parsing, settlement) | OK to keep running through finals |
| ARCHITECTURE_FAIL | Any settlement/parsing/feed bug discovered after a fire | Pause, fix, document, restart |
| INSUFFICIENT_LIQUIDITY | n < 3 fires by May 25 due to liquidity floor | Pause; revisit at NBA regular-season open Oct 2026 |
| END_OF_RUNWAY | NBA + NHL Finals concluded | Archive T7 with summary |

**No early-kill thesis trigger** — sample is too small to be statistically meaningful. The point of T7 this cycle is "did the architecture transfer cleanly," not "is sharp NBA/NHL profitable."

After Finals (mid-June): write a T7 retrospective. If n ≥ 5 with no architectural surprises, queue T11 NFL prep (when T6 also validates). If T7 hit any of the architectural failure modes, T11 budget/timeline gets reassessed.

---

## 7. Files to create

```
~/Documents/
  terminal7_nba_nhl_spec.md             (this file)
  terminal7_paper_trader.py             (clone of terminal6_mlb_paper_trader.py)
  terminal7_kalshi_ws.py                (clone)
  terminal7_lines_puller.py             (clone)
  terminal7_settlement_reconciler.py    (clone)
  terminal7_kalshi_logger.py            (clone)
  terminal7_milestone_check.py          (clone with adjusted gates)
  terminal7_data/                       (data dir, mirror of terminal6_data)
  redeploy_t7_all.sh                    (clone of redeploy_t6_all.sh)
  shadow_pnl/engines.json               (T7 entry: bankroll=2000, mode→shadow, active=true)
```

Estimated build effort: **2-3 days** including adversarial review checkpoints. The clone is mechanical; the new logic is the title regex + series cap + sport normalization (~150 LOC delta vs T6).

---

## 8. Adversarial review checkpoints (per handoff Lesson #adversarial-review)

1. **After spec lock, before code:** send this spec to a second Claude session for adversarial review. Look for:
   - Tautological logic (any "edge" formula that reduces to price × constant)
   - Validation that's post-hoc on settled data (T2-style)
   - Hidden assumptions about NBA/NHL series structure
   - Hidden Vegas data quirks
2. **After backtest is written, before any shadow capital:** send backtest methodology + sample output to second model. Specifically: walk-forward only, no in-sample fitting.
3. **After first 5 closed positions:** review for surprises. Anything ≥ ±2σ from expected EV warrants a stop-and-investigate.

The T2 post-mortem is the template. **Don't deploy capital — even shadow — without checkpoint #2 cleared.**

---

## 9. Open questions for Steve

| # | Question | Default if no answer |
|---|---|---|
| 1 | Upgrade Odds API to $30/mo Plus tier? | YES — T6 audit shows current free tier is being exceeded anyway. Decision is "do it now or wait until T6 hits the wall." |
| 2 | NBA + NHL together, or NBA-only first? | **Both.** The NBA team map is more familiar to operators but NHL adds another ~12 games to the addressable universe. Marginal architecture risk is small. |
| 3 | Bankroll: stay at $2K or scale down to $1K so Kelly position cap is $50 (allows 2x position count for same risk)? | **$2K as locked.** Don't shave bankroll for sample-count reasons; if the architecture works it works. |
| 4 | Liquidity floor (open_interest ≥ 100, vol_24h ≥ 50): start there or different threshold? | **Start there, tune after first 3 fires.** No data on Kalshi NBA/NHL liquidity in your ledger yet. |
| 5 | Series exposure cap: 2 positions per series, or 1? | **2** — G1 + G2 are independent in the spec definition. G1 result will leak into G2 sharp prices, but that's already in the de-vigged Vegas number. |
| 6 | Paid Odds API key delivery channel? | None of your secrets are in env vars currently (everything reads `~/Documents/.odds_api_key`). Consistent — keep that pattern. |

---

## 10. Sequenced build plan

**Day 1: scaffolding + data feeds**
- Copy T6 files to terminal7_*. Strip out MLB-specific bits.
- Wire up Odds API basketball_nba + icehockey_nhl endpoints.
- Wire up Kalshi WS subscription to KXNBAGAME + KXNHLGAME.
- NBA + NHL team maps (probably 30 + 32 = 62 entries, easy).
- Game-number regex parser, unit test.

**Day 2: gates + reconciler**
- Wire up Game 1-2 gate.
- Wire up per-series exposure cap (parse `R\d` from series_ticker).
- Sport-specific `_normalize()`.
- Test settlement reconciler on a synthetic G3 settlement (won't actually fire since G3+ is rejected, but reconciler must handle correctly).

**Day 3: dry runs + adversarial review**
- Dry-run trader against current open Kalshi NBA/NHL markets (no positions opened).
- Confirm Vegas matching.
- Send to second Claude session: review code + sample dry-run output before deploying capital.

**Day 4: deploy shadow.**

If conf finals haven't started by Day 4, T7 will be idle until G1 of conf finals (~May 18-22). That's fine — runs idle, costs nothing.

---

## 11. Risks & known unknowns

- **NBA Game market liquidity is currently zero.** All 14 open KXNBAGAME markets I probed had vol_24h=0 and OI=0. Kalshi's basketball product may be too thin for any meaningful trades. If conf finals open with sub-liquidity, T7 fires few or zero trades. This isn't a code defect; it's a market reality. INSUFFICIENT_LIQUIDITY is the likely outcome and the spec acknowledges it.
- **NHL Game market liquidity unknown** — also zero on the markets I probed but it's the same Saturday window. Probe again at game time (5 PM ET tonight).
- **Vegas books for NHL playoff games may be thinner than NBA.** Pinnacle has solid NHL coverage, but FD/MGM may not list every game until close to puck-drop.
- **Title parsing fragility.** If Kalshi changes the title format (e.g., "Game 3 of 7: ..." or "Conference Final Game 1: ...") the regex breaks silently and trades stop firing — same failure mode as T6's MATCH-SOONEST bug. **Mitigation:** add a per-cycle log line counting "title-parse-failed" rejections. If that count > 0 on any cycle, alert.
- **Series ticker round-suffix changes.** R1/R2/R3/R4 today. If Kalshi adds a "Play-In" round (NBA does), the parser needs to handle it.
- **Adversarial review may find a problem.** Build it in.

---

## 12. What this is not

- **Not a bet on NBA/NHL trading being profitable.** $2K bankroll, $100 max position, ~6-10 fires this cycle. Not enough sample. If the architecture is valid, T7 in October 2026 (regular season, full schedule) is the real revenue play.
- **Not a substitute for T6 validation.** T6 is still the linchpin. T7 building does not change the T6 close-rate concern.
- **Not a NFL precursor decision.** T11 (NFL) gates on T6 outcome at n=300, not on T7. T7 confirms architecture; T6 validates edge.
