#!/usr/bin/env python3
"""T7 milestone check — modified gates per spec §6.

T7 cannot reach n=300 this cycle (~12 G1/G2 games combined NBA+NHL,
expect 6-10 fires before mid-June Finals end). Validation is
ARCHITECTURE-CONFIRMATION, not edge-validation. T6 remains the edge
validator for the sharp-sports vertical.

═══════════════════════════════════════════════════════════════════════
DECISION CRITERIA (per spec §6, locked 2026-05-09 with Steve)
═══════════════════════════════════════════════════════════════════════

Each closed T7 position contributes one P&L observation. Compute the
same stats as T6 (mean, sd, lower_95, etc.) but apply T7-specific gates:

  GATE 1 — ARCHITECTURE_CONFIRMED
    if n >= 5 and architectural_failures == 0:
        verdict = "ARCHITECTURE CONFIRMED — keep running through finals.
                   Sharp-sports thesis transferred cleanly across sports.
                   Queue T11 NFL prep when T6 also validates."

  GATE 2 — ARCHITECTURE_FAIL
    if any settlement/parsing/feed bug discovered after a fire:
        verdict = "ARCHITECTURE FAIL — pause, fix, document, restart."

  GATE 3 — INSUFFICIENT_LIQUIDITY
    if n < 3 by 2026-05-25 due to liquidity floor rejections:
        verdict = "INSUFFICIENT LIQUIDITY — pause; revisit at NBA regular
                   season open Oct 2026 with more book volume."

  GATE 4 — END_OF_RUNWAY
    if NBA + NHL Finals concluded:
        verdict = "END OF RUNWAY — write retrospective; archive engine."

  GATE 5 — ACCUMULATING
    else:
        verdict = "ACCUMULATING — n=N, target 5 fires for confirmation."

NO EARLY-KILL THESIS TRIGGER. Sample is too small to be statistically
meaningful. T7 is architecture-confirmation only this cycle.

Runway dates (probed 2026-05-09):
  - Conf Finals G1-G2: ~May 15-25 (~8 games)
  - Finals G1-G2: ~June 2-10 (~4 games)
  - Total G1/G2 universe: ~12 games combined NBA+NHL

Output: human-readable summary + headline to milestone_latest.txt.
JSON via --json. Architecture-failure flagging is manual (operator
sets ARCHITECTURE_FAIL_FLAG file when a code-level bug is found).

Usage:
    python3 terminal7_milestone_check.py
    python3 terminal7_milestone_check.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import stdev
from typing import Dict, List, Optional

DOCS = Path.home() / "Documents"
LEDGER = DOCS / "shadow_pnl" / "ledger.jsonl"
LATEST_TXT = DOCS / "terminal7_data" / "milestone_latest.txt"
ARCH_FAIL_FLAG = DOCS / "terminal7_data" / "architecture_fail.flag"

# Gate thresholds — locked with Steve 2026-05-09 per spec §6.
ARCHITECTURE_CONFIRM_N = 5      # n>=5 with no failures = confirmed
INSUFFICIENT_LIQUIDITY_N = 3    # n<3 by 2026-05-25 = liquidity gate failed
INSUFFICIENT_LIQUIDITY_DEADLINE = "2026-05-25"
END_OF_RUNWAY_DATE = "2026-06-15"   # post-Finals window
BUCKET_CONCENTRATION_FLAG = 0.40    # mirrors T6 — diagnostic, not gate
SPORT_CONCENTRATION_FLAG = 0.50     # NEW for T7 — single-sport dominance


def load_t7_closed() -> List[dict]:
    if not LEDGER.exists():
        return []
    opens: Dict[str, dict] = {}
    closes: List[dict] = []
    with open(LEDGER) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("engine") != "T7":
                continue
            t = r.get("type")
            pid = r.get("position_id")
            if t == "open" and pid:
                opens[pid] = r
            elif t == "close" and pid and pid in opens:
                op = opens[pid]
                closes.append({
                    "position_id": pid,
                    "ticker": op.get("ticker"),
                    "side": op.get("side"),
                    "entry_price": op.get("price"),
                    "size": op.get("size"),
                    "realized_pnl_usd": r.get("realized_pnl_usd", 0.0),
                    "open_ts": op.get("ts"),
                    "close_ts": r.get("ts"),
                    "signal_metadata": op.get("signal_metadata", {}),
                })
    return closes


def compute_stats(closes: List[dict]) -> dict:
    n = len(closes)
    pnls = [c["realized_pnl_usd"] for c in closes]
    if n == 0:
        return {"n": 0, "mean": 0.0, "sd": 0.0, "lower_95": 0.0, "upper_95": 0.0,
                "total_pnl": 0.0, "wins": 0, "losses": 0}
    mean = sum(pnls) / n
    sd = stdev(pnls) if n >= 2 else 0.0
    se = sd / math.sqrt(n) if n >= 2 else 0.0
    lower_95 = mean - 1.96 * se
    upper_95 = mean + 1.96 * se
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    return {
        "n": n,
        "mean": mean,
        "sd": sd,
        "lower_95": lower_95,
        "upper_95": upper_95,
        "total_pnl": sum(pnls),
        "wins": wins,
        "losses": losses,
    }


def evaluate_gate(stats: dict) -> dict:
    n = stats["n"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if ARCH_FAIL_FLAG.exists():
        try:
            reason = ARCH_FAIL_FLAG.read_text().strip()
        except OSError:
            reason = "(flag set, no detail)"
        return {
            "gate": "ARCHITECTURE_FAIL",
            "verdict": f"ARCHITECTURE FAIL — pause T7. Flag set: {reason}",
            "action_required": True,
        }

    if today >= END_OF_RUNWAY_DATE:
        return {
            "gate": "END_OF_RUNWAY",
            "verdict": "END OF RUNWAY — Finals concluded. Write retrospective; archive T7.",
            "action_required": True,
        }

    if n >= ARCHITECTURE_CONFIRM_N:
        return {
            "gate": "ARCHITECTURE_CONFIRMED",
            "verdict": (
                "ARCHITECTURE CONFIRMED — sharp-sports thesis transferred cleanly. "
                "Keep T7 running through Finals. Queue T11 NFL prep when T6 also "
                "validates."
            ),
            "action_required": False,
        }

    if today >= INSUFFICIENT_LIQUIDITY_DEADLINE and n < INSUFFICIENT_LIQUIDITY_N:
        return {
            "gate": "INSUFFICIENT_LIQUIDITY",
            "verdict": (
                f"INSUFFICIENT LIQUIDITY — n={n} fires by {today} "
                f"(deadline {INSUFFICIENT_LIQUIDITY_DEADLINE}). "
                "Pause T7; revisit at NBA regular season open Oct 2026."
            ),
            "action_required": True,
        }

    return {
        "gate": "ACCUMULATING",
        "verdict": f"ACCUMULATING — n={n}, target {ARCHITECTURE_CONFIRM_N} fires for confirmation.",
        "action_required": False,
    }


def bucket_analysis(closes: List[dict]) -> dict:
    if not closes:
        return {}
    delta_buckets: Dict[str, float] = defaultdict(float)
    sport_pnl: Dict[str, float] = defaultdict(float)
    team_pnl: Dict[str, float] = defaultdict(float)
    total = 0.0
    for c in closes:
        pnl = c["realized_pnl_usd"]
        total += pnl
        meta = c.get("signal_metadata", {}) or {}
        delta = abs(meta.get("delta", 0))
        if delta < 0.04:
            bucket = "3-4pp"
        elif delta < 0.05:
            bucket = "4-5pp"
        else:
            bucket = ">5pp"
        delta_buckets[bucket] += pnl
        sport = meta.get("sport", "unknown")
        sport_pnl[sport] += pnl
        team = meta.get("team_name", "unknown")
        team_pnl[team] += pnl
    delta_share = {b: (v / total if total else 0.0) for b, v in delta_buckets.items()}
    sport_share = {s: (v / total if total else 0.0) for s, v in sport_pnl.items()}
    flags = []
    for b, share in delta_share.items():
        if abs(share) > BUCKET_CONCENTRATION_FLAG:
            flags.append(f"delta bucket '{b}' = {share*100:.1f}% of P&L (>{BUCKET_CONCENTRATION_FLAG*100:.0f}% threshold)")
    for s, share in sport_share.items():
        if abs(share) > SPORT_CONCENTRATION_FLAG:
            flags.append(f"sport '{s}' = {share*100:.1f}% of P&L (>{SPORT_CONCENTRATION_FLAG*100:.0f}% threshold)")
    return {
        "delta_share": delta_share,
        "sport_share": sport_share,
        "team_pnl_top": dict(sorted(team_pnl.items(), key=lambda kv: -abs(kv[1]))[:5]),
        "concentration_flags": flags,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    closes = load_t7_closed()
    stats = compute_stats(closes)
    gate = evaluate_gate(stats)
    buckets = bucket_analysis(closes)

    summary = {
        "stats": stats,
        "gate": gate,
        "buckets": buckets,
    }

    headline = f"T7 milestone: {gate['gate']} | n={stats['n']} mean=${stats['mean']:.3f} lower95=${stats['lower_95']:.3f}"
    LATEST_TXT.parent.mkdir(parents=True, exist_ok=True)
    LATEST_TXT.write_text(headline + "\n")

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print("=" * 70)
    print(" T7 MILESTONE CHECK")
    print("=" * 70)
    print(f"  closes        : {stats['n']}")
    print(f"  wins / losses : {stats['wins']} / {stats['losses']}")
    print(f"  total P&L     : ${stats['total_pnl']:+.2f}")
    print(f"  mean per close: ${stats['mean']:+.4f}")
    print(f"  std dev       : ${stats['sd']:.4f}")
    print(f"  95% CI        : [${stats['lower_95']:+.4f}, ${stats['upper_95']:+.4f}]")
    print()
    print(f"  GATE          : {gate['gate']}")
    print(f"  VERDICT       : {gate['verdict']}")
    if gate["action_required"]:
        print(f"  ** ACTION REQUIRED THIS WEEK **")
    print()
    if buckets:
        print("  delta bucket P&L share:")
        for b, share in sorted(buckets["delta_share"].items()):
            print(f"    {b:<6} {share*100:+6.1f}%")
        print("  sport P&L share:")
        for s, share in sorted(buckets["sport_share"].items()):
            print(f"    {s:<6} {share*100:+6.1f}%")
        if buckets.get("concentration_flags"):
            print("  CONCENTRATION FLAGS:")
            for f in buckets["concentration_flags"]:
                print(f"    - {f}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
