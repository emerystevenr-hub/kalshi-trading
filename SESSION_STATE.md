# Session State — tomorrow AM (2026-04-24)

**Last updated:** 2026-04-24 05:30 UTC (late night before)
**Purpose:** read this FIRST when you open the laptop. Gives you current state and exact steps.

---

## Current deployed shadow positions (as of last night)

| Engine | Open | Cost tied up | Notes |
|---|---|---|---|
| T1 | 12 | ~$57 | Apr 24 weather bets across NYC + LAX. Settle tonight (~22:00 PDT). |
| T2 | 3 | $14.80 | Hormuz, Trump-China, DHS Fund. Long-dated (resolve May-Jun). |
| Total | 15 | ~$72 | 0.2% of $40k portfolio deployed |

## Running background processes (verify with `ps aux | grep python3`)

1. Kalshi weather logger (PID 8332/8333)
2. T3a Fed scanner (PID 14600) — continuous
3. T3a shadow executor — daemon, waiting for real arbs
4. T1 Phase 2 paper trader — daemon, polls every 30 min
5. T1 Settlement reconciler — daemon, polls every hour

---

## Morning sequence — do these in order

### Step 1 — Check T1 settlement (the primary data point)

```bash
python3 ~/Documents/shadow_pnl_core.py status
```

Look at the T1 row. It should show:
- `real` column: non-zero (hopefully positive)
- `open` dropped from 12 → closer to 0
- `W` and `L` counts filled in

If the reconciler hasn't run yet (e.g. before NWS actuals landed), force it:

```bash
python3 ~/Documents/terminal1_settlement_reconciler.py --once
```

### Step 2 — Calibration analysis

Compute hit rate vs `our_p` for T1 positions:

```bash
python3 << 'EOF'
import json
from collections import defaultdict
from pathlib import Path

ledger = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"
opens = {}
closes = []
with open(ledger) as f:
    for line in f:
        r = json.loads(line)
        if r.get("engine") != "T1":
            continue
        if r["type"] == "open":
            opens[r["position_id"]] = r
        elif r["type"] == "close":
            closes.append(r)

predicted = []
actual_wins = 0
for c in closes:
    op = opens.get(c["position_id"])
    if op is None:
        continue
    our_p = op.get("signal_metadata", {}).get("our_p", 0)
    # Was this position a win? YES if outcome==win
    won = c["outcome"] == "win"
    if won:
        actual_wins += 1
    predicted.append(our_p)

n = len(closes)
expected = sum(predicted)
print(f"Closed positions: {n}")
print(f"Expected wins (sum of our_p): {expected:.2f}")
print(f"Actual wins: {actual_wins}")
if n > 0:
    print(f"Calibration ratio: {actual_wins / expected:.2f} "
          f"(1.0 = perfect; <0.7 = over-confident; >1.3 = under-confident)")
EOF
```

**Interpretation:**
- **Ratio 0.8 – 1.2:** calibration OK → GO on Phase 3 build
- **Ratio < 0.7:** over-confident (our σ is too tight) → inflate σ before Phase 3
- **Ratio > 1.3:** under-confident (good problem) → investigate if we can be more aggressive

### Step 3 — Decide on T2 batch deploy

If T1 calibration is OK or inconclusive (too few data points), deploy the 5 T2 tail_probability trades:

```bash
# Preview:
python3 ~/Documents/deploy_t2_tail_batch.py --dry-run

# Deploy (interactive confirm):
python3 ~/Documents/deploy_t2_tail_batch.py
```

If T1 calibration is bad, HOLD the T2 batch until investigation done.

### Step 4 — Re-run thesis factory for fresh candidates

```bash
python3 ~/Documents/kalshi_thesis_factory.py
```

Takes ~15 min. Produces new timestamped `thesis_candidates_*.json` and `thesis_review_*.md`.

Compare to yesterday's 49 candidates — any new high-edge ones?

### Step 5 — Launch dashboard to watch

```bash
python3 ~/Documents/shadow_dashboard.py
```

---

## Queued work (in priority order)

### Next build: T1 Phase 3 late-execution layer

**Gate:** Phase 2 calibration ratio between 0.7 and 1.3 on ≥30 settled positions.

**Spec:** `~/Documents/terminal1_phase3_spec.md` (written last night)

**Summary:** METAR + TAF + station bias at T-6 to T-13 hours, tightens uncertainty to ~1.5°F, targets 97% vs 90% market implied. ~12 hours of work.

### Also queued

- **Station expansion:** add DEN, ATL, MIA, PHX, DFW → 5× T1 signal volume. Requires updating logger + model pullers + actuals puller + running new backfills.
- **T3c GDP/Claims scanner:** clone T3a pattern, swap series.
- **T3b CPI:** restart with prospective logger approach (Cleveland Fed archive blocked).
- **T5 catalyst finder (thesis-level):** merge into the existing thesis factory instead of building standalone.
- **Thesis factory filter patches:** PGA, soccer, RT, carbon, Spotify — already patched last night.

---

## Active theses being tracked

### T2 Catalyst (3 live + 5 queued for AM deploy)

**Live:**
1. KXDHSFUND-26JUN01 — NO @ $0.37. Thesis: DHS funding rarely passes on schedule.
2. KXTRUMPCHINA-26-MAY15 — NO @ $0.42. Thesis: trade deal speculation overweighted.
3. KXHORMUZNORM-26MAR17-B260601 — NO @ $0.69. Thesis: Hormuz transit calls fade (already partially played — was 62% YES on Apr 20, now 34%).

**Queued for AM (batch script ready at `deploy_t2_tail_batch.py`):**
1. KXGOVTSHUTLENGTH-26FEB07-G100 — NO 75 @ $0.64. Highest conviction. 100-day shutdown is historical outlier.
2. KXMJSCHEDULE-27 — NO 50 @ $0.54. Cannabis rescheduling delayed.
3. KXFDAAPPROVALPSYCHEDELIC-27-ANYPSYCH — NO 50 @ $0.60. FDA slow.
4. KXTRUMPADMINLEAVE-26DEC31-KLEA — NO 50 @ $0.54. Press sec stability year 1.
5. KXTRUMPADMINLEAVE-26DEC31-PHEG — NO 25 @ $0.58. Reduced size (correlated with #4).

### T1 Weather (12 live)

All for Apr 24 target date. Mix of NYC + LAX, HIGH and LOW, bucket and threshold strikes. Settle tonight.

### T3a Fed (0 live)

Executor watching for real Dutch arbs on KXFEDDECISION. All alerts so far are near-miss (4-5¢ from arb). April 29-30 FOMC meeting is a week out — potential catalyst.

---

## Key numbers to remember

- **$40,000 total shadow portfolio** across 8 engines at $5k each
- **T1 edge per signal (backtest):** ~$0.067
- **Fees per 10-contract trade:** ~$0.20 ($0.02/contract)
- **Avg LAX hit rate expected:** ~14–19% (from backtest buckets)
- **Avg NYC hit rate expected:** ~11–17% at edge ≥$0.08
- **ORD excluded:** reverse-monotonic edge metric, reenter only after calibration investigation

---

## Known issues / caveats

1. **Dashboard mark column:** shows YES mid for ALL positions (including NO). The unrealized P&L calc is still correct; just the display column is confusing for NO positions. Patched in code — relaunch dashboard to apply.
2. **HRRR watcher auto-run failed:** subprocess Python stdin init error. Backtest re-ran manually, all good. Watcher probably still running but no longer useful. Can `pkill -f terminal1_watch_hrrr` if desired.
3. **T5 naive catalyst finder produced 0 alerts:** hotness threshold too high for live Kalshi. Superseded by thesis factory.
4. **Stale `fed_scanner_alerts.jsonl` entries:** 9 KXFED false positives from pre-patch smoke test. Executor correctly skips them. Can clean with: `grep -v '"is_exclusive": null' ~/Documents/terminal3a_data/fed_scanner_alerts.jsonl > /tmp/clean.jsonl && mv /tmp/clean.jsonl ~/Documents/terminal3a_data/fed_scanner_alerts.jsonl`

---

## Decision framework for tomorrow

**Scenario A: T1 calibration OK + hit rates broadly match backtest**
→ Phase 2 validated. Deploy T2 batch. Start building Phase 3 late-layer. Start planning station expansion.

**Scenario B: T1 calibration over-confident (ratio < 0.7)**
→ Ensemble σ is too tight. Don't build Phase 3 yet. Instead: inflate ensemble σ by 1.5–2x in the paper trader, re-run for 3 more days, see if hit rates align.

**Scenario C: T1 calibration directionally correct but P&L negative**
→ Edge exists but fees ate it. Solutions: (a) raise min_edge threshold to $0.12, (b) reduce trade count to high-conviction only, (c) increase size on surviving signals.

**Scenario D: T1 completely flat or very red**
→ Something fundamental is wrong. Pause T1. Investigate. Check if NWS actuals match what the reconciler computed (sanity check ticker parsing).

---

*This file is the handoff. Update it before closing the laptop each night.*
