# SGO Pro $299/mo — Decision Report (D analysis)

_Generated 2026-05-10. Based on 30-day MLB open/close + lastUpdatedAt pull from SGO Pro REST trial._

## Verdict

**CANCEL the trial. Stay on The Odds API.**

The data converges across all three D signals: Pinnacle does not lead retail by enough, in either magnitude or timing, to justify a $299/mo data swap. The lead is ~4 minutes — sub-spread per Steve's locked decision rule.

## Three signals, 403 finalized MLB games (April 10 – May 9, 2026)

### Signal 1 — open→close magnitude (Steve's primary D rule)

For each game: `Δ = (close - open)` in implied probability, computed against `openBookOdds` (market consensus open) for both Pinnacle and the DK/FD/MGM consensus close.

| Outcome | Count | Pct |
|---|---:|---:|
| Retail moved farther by > 0.5pp | 327 | 81.1% |
| Pinnacle moved farther by > 0.5pp | 58 | 14.4% |
| Within 0.5pp of each other | 18 | 4.5% |

Median `|Δ_pinn| / |Δ_retail|` ratio: **0.928** (Pinnacle moves ~7% less than retail).
Median `|Δ_pinn| − |Δ_retail|`: **−3.09pp** (Pinnacle ends ~3pp closer to consensus open than retail does).

**Per Steve's locked rule** ("Pinnacle's open→close move LARGER in same direction = structural value"): Pinnacle does NOT move farther. Result fails the rule by 6:1 in the wrong direction.

### Signal 2 — directional alignment

| Outcome | Count | Pct |
|---|---:|---:|
| Same direction | 391 | 97.0% |
| Opposite direction | 12 | 3.0% |

Retail and Pinnacle agree on direction in 97% of games. Combined with Signal 1, retail captures the same signal Pinnacle captures — and then some. **Retail does not lag Pinnacle in direction.**

### Signal 3 — lastUpdatedAt timing

Median (Pinnacle.lastUpdatedAt − retail-median.lastUpdatedAt): **−236 seconds (−3.9 min)**.

| Outcome | Count | Pct |
|---|---:|---:|
| Pinnacle updated BEFORE retail (lead) | 251 | 62.3% |
| Pinnacle updated AFTER retail (lag) | 150 | 37.2% |
| Same time | 2 | 0.5% |

Pinnacle leads retail by ~4 minutes on the most-recent-change timestamp. **This is sub-spread per the decision rule (cancel ≤ 5 min, subscribe ≥ 30 min).** It is also consistent with the session-4 single-snapshot probe (median delta 0.69pp, sub-spread).

Note: `lastUpdatedAt` is the timestamp of the most recent change, not a movement curve. A 4-min lead on the latest change is at best a coarse signal; the underlying intraday movement could differ. But it's the only timing signal available without forward-polling.

## Why Pinnacle "moves less" — interpretation

Possible structural readings (in order of likelihood):

1. **Pinnacle anchors and retail overshoots.** Pinnacle posts the sharp price first (consistent with Signal 3's 4-min lead). Retail follows but adds vig/balance pressure that produces a larger move from open to close. Net: retail ends up farther from consensus open than Pinnacle does.

2. **Retail incorporates more inputs.** Retail prices in customer flow + Pinnacle + sharp money + their own model. Pinnacle is more conservative. Retail moves farther because it's combining more signals.

3. **`openBookOdds` is consensus-weighted, not Pinnacle-specific.** Δ_pinn is therefore "Pinnacle close vs consensus open," not "Pinnacle close vs Pinnacle open." This confounds the measurement to some extent. If Pinnacle's actual open is closer to its own close than consensus open is, the magnitude advantage Pinnacle "loses" is partly an artifact of using the consensus baseline.

In all three readings, the actionable conclusion is the same: **a T6/T7 trader running on retail consensus is not missing meaningful Pinnacle-specific information.**

## Decision rule application

Steve's locked rules:

> **Q3 framing:** "If retail mirrors Pinnacle within 5 minutes on average, the data upgrade is sub-spread noise and you stay on The Odds API."

Signal 3 → median lead 3.9 min. **Below the 5-minute threshold.** → CANCEL.

> **D framing:** "If Pinnacle's open→close move is consistently larger than DK/FD/MGM consensus move in the same direction, Pinnacle is incorporating more information over the course of the day. That's structural value even without knowing the timing. If they move the same amount in the same direction, the retail books are already capturing the same signal — Pinnacle adds nothing."

Signal 1+2 → retail moves farther in same direction 81% of the time. **Retail is capturing the same signal AND more.** → CANCEL.

Both rules agree. Decision is robust.

## Should we still run B?

Steve's pre-stated threshold for skipping B: "If [D] already shows convergence, B answers the timing question but D may already tell you the answer is no."

D shows decisive convergence:
- Signal 1: 81% retail-bigger, median 3pp deficit on Pinnacle
- Signal 2: 97% directional alignment
- Signal 3: 4-min lead, sub-spread

**Recommendation: skip B.** The forward poll would refine Signal 3's precision but is unlikely to flip the verdict. A 4-min median lead doesn't become a 30-min lead by switching from REST to forward poll — they measure approximately the same thing on different cadences.

If Steve wants confirmation rather than a clean halt: run B as a passive 7-day poll (~24K trial events, well within budget), reach the May 17 trial deadline, then cancel.

## Action items

1. **Cancel SGO Pro trial before May 17.** Set a calendar reminder.
2. **No data-layer change to T6 or T7.** Continue on The Odds API.
3. **Document this decision in the next handoff.** SGO Pro REST is the wrong product for the lead-time question; AllStar WebSocket would be a different (and untested) decision.
4. **Update §11 conclusion in handoff.** The Pinnacle-leads-retail thesis is dead at the granularity SGO Pro REST exposes. Sub-spread, not a tradeable mispricing.

## Methodology caveats (preserve for audit)

- Per-book opening odds not exposed by SGO Pro REST. Δ uses consensus `openBookOdds`, not Pinnacle's own open. Confound is bounded but real.
- `lastUpdatedAt` is the latest change timestamp, not a full movement curve. 4-min lead approximates the timing-of-most-recent-move, not the timing-of-first-move-on-news.
- 30-day window covers MLB regular season only. Playoff lead times may differ; the T7 NBA/NHL question is not settled by this analysis.
- Sample of 403 games. Statistical strength: signal-1 magnitude difference is overwhelming (327 vs 58, p << 0.001 by sign test). Decision is robust.

## Raw data

`~/Documents/terminal7_data/sgo_open_close_30d.jsonl` — 403 rows, one per finalized game.

`~/Documents/sgo_d_pull.py` — pull script with chunkable date ranges.

`~/Documents/sgo_d_analyze.py` — analysis with all three signals, JSON summary at end of stdout.
