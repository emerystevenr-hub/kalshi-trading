#!/usr/bin/env python3
"""T6 weekly milestone check + explicit early-kill criteria.

Runs every Monday at 9:09 AM PDT via the Cowork scheduled task
`t6-shadow-trading-milestone-checks`. Also runnable on demand via
`python3 terminal6_milestone_check.py` to inspect status anytime.

Born 2026-05-09 after the T2 0/8 thesis kill exposed that no engine had
explicit early-fail criteria — losses compounded because the operator
kept waiting for "more data" without a written threshold for when to
admit the thesis was dead. This file IS that threshold for T6.

T6 is the thesis validator for the entire sharp-sports vertical.
T7 (NBA/NHL playoffs) and T11 (NFL) both gate on T6 outcome.
If T6 fails, the sports vertical fails. If T6 validates, the
architecture is proven and downstream engines build on it.

═══════════════════════════════════════════════════════════════════════
DECISION CRITERIA (locked 2026-05-09 with Steve)
═══════════════════════════════════════════════════════════════════════

Each closed T6 position contributes one P&L observation (entry to
settlement, after Kalshi taker fee). Compute:
    n            = count of closed positions
    mean         = sum(realized_pnl) / n
    sd           = stdev(realized_pnl)
    lower_95     = mean - 1.96 * sd / sqrt(n)
    upper_95     = mean + 1.96 * sd / sqrt(n)

Apply gates in priority order. Earlier gates short-circuit — the
first matching gate is the verdict for the week.

  GATE 1 — EARLY KILL
    if n >= 200 and lower_95 < -0.50:
        verdict = "EARLY KILL — archive T6, bury sports vertical,
                   reallocate $12K. Don't wait for n=300."
    Rationale: at n=200 with lower-95 < -$0.50, the 95% CI rules out
    breakeven by $0.50/close. Continuing to n=300 just bleeds ~$50
    more in expectation with no chance of reversing the verdict.

  GATE 2 — VALIDATED
    if n >= 300 and lower_95 >= 0.0:
        verdict = "VALIDATED — ship NFL (T11) build, queue T7 regular
                   season for October, real-money decision unlocked."

  GATE 3 — INCONCLUSIVE
    if n >= 300 and mean > 0 and lower_95 < 0:
        verdict = "INCONCLUSIVE — extend shadow to n=500 for tighter CI.
                   Do NOT advance to real money. Do NOT ship NFL build.
                   Mean is positive but the 95% lower bound straddles 0."

  GATE 4 — DEAD
    if n >= 300 and mean <= 0:
        verdict = "DEAD — archive T6 same as gate 1. Sports vertical
                   buried. T7 and T11 do not build."

  GATE 5 — STILL ACCUMULATING
    else:
        verdict = "ACCUMULATING — n={n}, target 300. ETA roughly
                   (300-n)/(close_rate). No action this week."

Bucket diagnostics (run at every milestone, regardless of gate):
  - book_attribution: at n≥100, compute per-book (Pinnacle, DK, FD, MGM,
    Caesars) win rate. If Pinnacle alone ≥ 5-book consensus, simplify
    lines puller to Pinnacle-only.
  - delta_buckets: P&L by entry-delta range (3-4pp, 4-5pp, >5pp).
    Watch for single-bucket dominance >40% of total profit (concentration
    risk — see T2 lesson).
  - team_concentration: P&L by team. Single-team dominance >30% means
    edge is sport-specific not architecture-general.

Output: human-readable summary printed to stdout, one-line headline
written to ~/Documents/terminal6_data/milestone_latest.txt for the
dashboard. JSON summary available via --json.

Usage:
    python3 terminal6_milestone_check.py
    python3 terminal6_milestone_check.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import stdev
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

DOCS = Path.home() / "Documents"
LEDGER = DOCS / "shadow_pnl" / "ledger.jsonl"
LATEST_TXT = DOCS / "terminal6_data" / "milestone_latest.txt"

# Gate thresholds — locked with Steve 2026-05-09. Do not relax these
# without a documented architectural change and a new rationale.
EARLY_KILL_N = 200
EARLY_KILL_LOWER_95 = -0.50
VALIDATE_N = 300
INCONCLUSIVE_EXTEND_TO = 500
BUCKET_CONCENTRATION_FLAG = 0.40   # >40% of profit from one bucket = concentration risk
TEAM_CONCENTRATION_FLAG = 0.30     # >30% from single team = sport-specific not general

# 2026-05-10 — vegas-match contamination filter.
# A bug in terminal6_mlb_paper_trader.py (fixed same day) bound a Kalshi
# ticker for one game-day to the *next* day's Vegas line via team-pair-
# only matching. Every closed position before the fix is contaminated:
# the matched commence_time is ~20h offset from the ticker's encoded
# game datetime. Filter those out so the gate verdict reflects real
# edge measurement, not stale-ticker arbitrage.
CONTAMINATION_WINDOW = timedelta(hours=12)
_TICKER_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_ticker_dt_utc(event_ticker: str) -> Optional[datetime]:
    """Mirror of paper_trader.parse_ticker_game_dt_utc — kept independent
    here to avoid import cycles and so a future T6 file rename doesn't
    break the milestone check."""
    if not event_ticker or not event_ticker.startswith("KXMLBGAME-"):
        return None
    rest = event_ticker[len("KXMLBGAME-"):]
    if len(rest) < 11:
        return None
    try:
        yy = int(rest[0:2]); mmm = rest[2:5].upper()
        dd = int(rest[5:7]); hh = int(rest[7:9]); mm = int(rest[9:11])
    except ValueError:
        return None
    if mmm not in _TICKER_MONTHS:
        return None
    try:
        local = datetime(2000 + yy, _TICKER_MONTHS[mmm], dd, hh, mm)
    except ValueError:
        return None
    return local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def _is_contaminated(close: dict) -> bool:
    """True if the close's open was bound to a Vegas line whose commence_time
    is more than CONTAMINATION_WINDOW from the ticker's encoded date."""
    meta = close.get("signal_metadata") or {}
    ev_ticker = meta.get("event_ticker") or ""
    ticker_dt = _parse_ticker_dt_utc(ev_ticker)
    commence_str = meta.get("commence_time_utc") or ""
    if not ticker_dt or not commence_str:
        return False  # missing metadata — fail-open, count as clean
    try:
        commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return abs(commence_dt - ticker_dt) > CONTAMINATION_WINDOW


def load_t6_closed() -> List[dict]:
    """Pair open/close ledger entries for engine=T6, return closed positions."""
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
            if r.get("engine") != "T6":
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
    mean = stats["mean"]
    lower_95 = stats["lower_95"]

    if n >= EARLY_KILL_N and lower_95 < EARLY_KILL_LOWER_95:
        return {
            "gate": "EARLY_KILL",
            "verdict": "EARLY KILL — archive T6, bury sports vertical, reallocate $12K. Do not wait for n=300.",
            "action_required": True,
        }
    if n >= VALIDATE_N and lower_95 >= 0.0:
        return {
            "gate": "VALIDATED",
            "verdict": "VALIDATED — ship NFL (T11) build, queue T7 regular season for October, real-money decision unlocked.",
            "action_required": True,
        }
    if n >= VALIDATE_N and mean > 0 and lower_95 < 0:
        return {
            "gate": "INCONCLUSIVE",
            "verdict": f"INCONCLUSIVE — extend shadow to n={INCONCLUSIVE_EXTEND_TO}. Do NOT ship NFL. Do NOT advance real money.",
            "action_required": False,
        }
    if n >= VALIDATE_N and mean <= 0:
        return {
            "gate": "DEAD",
            "verdict": "DEAD — archive T6, sports vertical buried. T7 and T11 do not build.",
            "action_required": True,
        }
    return {
        "gate": "ACCUMULATING",
        "verdict": f"ACCUMULATING — n={n}, target {VALIDATE_N}. No action this week.",
        "action_required": False,
    }


def bucket_analysis(closes: List[dict]) -> dict:
    """Concentration checks: per-delta-bucket and per-team P&L distribution."""
    if not closes:
        return {}
    delta_buckets: Dict[str, float] = defaultdict(float)
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
        team = meta.get("team_name", "unknown")
        team_pnl[team] += pnl
    delta_share = {b: (v / total if total else 0.0) for b, v in delta_buckets.items()}
    team_share_max = max((v / total if total else 0.0) for v in team_pnl.values()) if team_pnl else 0.0
    flags = []
    for b, share in delta_share.items():
        if abs(share) > BUCKET_CONCENTRATION_FLAG:
            flags.append(f"delta bucket '{b}' = {share*100:.1f}% of P&L (>{BUCKET_CONCENTRATION_FLAG*100:.0f}% threshold)")
    if abs(team_share_max) > TEAM_CONCENTRATION_FLAG:
        top_team = max(team_pnl, key=lambda t: abs(team_pnl[t]))
        flags.append(f"team '{top_team}' = {team_share_max*100:.1f}% of P&L (>{TEAM_CONCENTRATION_FLAG*100:.0f}% threshold)")
    return {
        "delta_share": delta_share,
        "team_pnl_top": dict(sorted(team_pnl.items(), key=lambda kv: -abs(kv[1]))[:5]),
        "concentration_flags": flags,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--include-contaminated", action="store_true",
                    help="Include vegas-match-contaminated closes (audit only). "
                         "Default: exclude per the 2026-05-10 fix.")
    args = ap.parse_args()

    all_closes = load_t6_closed()
    contaminated = [c for c in all_closes if _is_contaminated(c)]
    if args.include_contaminated:
        closes = all_closes
    else:
        closes = [c for c in all_closes if not _is_contaminated(c)]
    stats = compute_stats(closes)
    gate = evaluate_gate(stats)
    buckets = bucket_analysis(closes)

    summary = {
        "stats": stats,
        "gate": gate,
        "buckets": buckets,
        "contamination": {
            "total_closes": len(all_closes),
            "contaminated_closes": len(contaminated),
            "clean_closes": len(closes),
            "contaminated_pnl_excluded": sum(c["realized_pnl_usd"] for c in contaminated),
        },
    }

    headline = f"T6 milestone: {gate['gate']} | n={stats['n']} mean=${stats['mean']:.3f} lower95=${stats['lower_95']:.3f}"
    LATEST_TXT.parent.mkdir(parents=True, exist_ok=True)
    LATEST_TXT.write_text(headline + "\n")

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print("=" * 70)
    print(" T6 MILESTONE CHECK")
    print("=" * 70)
    contam = summary.get("contamination", {})
    if contam.get("contaminated_closes"):
        print(f"  CONTAMINATION : {contam['contaminated_closes']}/{contam['total_closes']} closes "
              f"excluded (vegas-match bug fixed 2026-05-10)")
        print(f"                  excluded P&L: ${contam['contaminated_pnl_excluded']:+.2f}")
        print()
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
        if buckets.get("concentration_flags"):
            print("  CONCENTRATION FLAGS:")
            for f in buckets["concentration_flags"]:
                print(f"    - {f}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
