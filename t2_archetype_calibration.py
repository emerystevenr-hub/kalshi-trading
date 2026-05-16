"""Terminal 2 — Archetype Calibration.

Computes per-archetype (sub_engine) win rate from settled T2 closes,
writes ~/Documents/terminal2_data/archetype_cal.json, and exposes a
single function `blocked_archetypes(min_n, min_win_rate)` for the
daily picks pipeline to consume.

Self-correcting pipeline contract:
    - any archetype with n ≥ MIN_N settled AND win_rate < MIN_WIN_RATE
      gets blocked from tomorrow's picks
    - calibration is recomputed every pick run from raw ledger
      (no stale state to sync)
    - first 8 settled losses (Apr 26 + May 2 batches) seed the table

Usage:
    python3 ~/Documents/t2_archetype_calibration.py
    python3 ~/Documents/t2_archetype_calibration.py --min-n 3 --min-rate 0.30
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set

sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import _read_ledger  # noqa: E402

DATA_DIR = Path.home() / "Documents" / "terminal2_data"
CAL_PATH = DATA_DIR / "archetype_cal.json"
ENGINE = "T2"

DEFAULT_MIN_N = 3
DEFAULT_MIN_WIN_RATE = 0.30


def _archetype(md: dict) -> str:
    """Map a position's signal_metadata to a single archetype label."""
    if not md:
        return "unknown"
    src = (md.get("source") or "").lower()
    sub = (md.get("sub_engine") or "").lower()
    # Picks-pipeline-driven sources carry a sub_engine cleanly
    if sub:
        return sub
    # Thesis factory direct outputs encode archetype in source
    if "tail_probability" in src:
        return "tail_probability"
    if "calendar_fade" in src:
        return "calendar_fade"
    if "reshadow" in src:
        return "reshadow_manual"
    return "unknown"


def compute_calibration() -> Dict[str, dict]:
    """Replay ledger → archetype-level win/loss/cost/pnl rollup."""
    opens: Dict[str, dict] = {}
    closes: List[dict] = []
    annul: Dict[str, Set[str]] = defaultdict(set)
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        t = r.get("type")
        pid = r.get("position_id")
        if t == "open":
            opens[pid] = r
        elif t == "close":
            closes.append(r)
        elif t == "annul_close":
            annul[pid].add(r.get("annulled_close_ts"))

    rollup: Dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "losses": 0, "cost_usd": 0.0,
                 "realized_pnl_usd": 0.0, "tickers": []}
    )
    for c in closes:
        if c.get("ts") in annul.get(c.get("position_id"), set()):
            continue
        op = opens.get(c.get("position_id"))
        if not op:
            continue
        arc = _archetype(op.get("signal_metadata") or {})
        s = rollup[arc]
        s["n"] += 1
        if c.get("outcome") == "win":
            s["wins"] += 1
        elif c.get("outcome") == "loss":
            s["losses"] += 1
        s["cost_usd"] += op.get("cost_usd", 0)
        s["realized_pnl_usd"] += c.get("realized_pnl_usd", 0)
        s["tickers"].append(op.get("ticker", ""))

    # Add derived stats
    out: Dict[str, dict] = {}
    for arc, s in rollup.items():
        win_rate = s["wins"] / s["n"] if s["n"] else 0.0
        roi = (s["realized_pnl_usd"] / s["cost_usd"] * 100) if s["cost_usd"] else 0.0
        out[arc] = {
            **s,
            "win_rate": round(win_rate, 4),
            "roi_pct": round(roi, 2),
        }
    return out


def blocked_archetypes(min_n: int = DEFAULT_MIN_N,
                       min_win_rate: float = DEFAULT_MIN_WIN_RATE) -> Set[str]:
    """Return set of archetypes that should be EXCLUDED from picks."""
    cal = compute_calibration()
    return {
        arc for arc, s in cal.items()
        if s["n"] >= min_n and s["win_rate"] < min_win_rate
    }


def write_calibration() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cal = compute_calibration()
    blocked = sorted(blocked_archetypes())
    payload = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "min_n": DEFAULT_MIN_N,
        "min_win_rate": DEFAULT_MIN_WIN_RATE,
        "calibration": cal,
        "blocked_archetypes": blocked,
    }
    with open(CAL_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    ap.add_argument("--min-rate", type=float, default=DEFAULT_MIN_WIN_RATE)
    args = ap.parse_args()

    payload = write_calibration()
    cal = payload["calibration"]
    blocked = payload["blocked_archetypes"]

    print(f"=== T2 Archetype Calibration ===")
    print(f"  cal_path: {CAL_PATH}")
    print(f"  min_n={args.min_n}  min_win_rate={args.min_rate}")
    print()
    if not cal:
        print("  no settled T2 closes yet — table empty")
        return 0
    print(f"  {'ARCHETYPE':<22} {'N':>3} {'W':>3} {'L':>3} {'WIN%':>6} "
          f"{'COST':>9} {'PNL':>9} {'ROI%':>7} {'STATUS':<8}")
    for arc in sorted(cal.keys(), key=lambda a: -cal[a]["n"]):
        s = cal[arc]
        status = "BLOCKED" if arc in blocked else "ok"
        print(f"  {arc:<22} {s['n']:>3} {s['wins']:>3} {s['losses']:>3} "
              f"{s['win_rate']*100:>5.0f}% ${s['cost_usd']:>7.2f} "
              f"${s['realized_pnl_usd']:>+7.2f} {s['roi_pct']:>+6.1f}% {status:<8}")
    print()
    if blocked:
        print(f"  Blocked archetypes (n≥{args.min_n}, win<{args.min_rate*100:.0f}%): {blocked}")
    else:
        print(f"  No archetypes blocked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
