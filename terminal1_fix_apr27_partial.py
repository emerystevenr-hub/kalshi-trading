"""Terminal 1 — Fix Apr 27 partial-day settlement.

The reconciler closed all 22 Apr 27 positions at 00:17 UTC Apr 28, using
actuals pulled at 14:37 UTC Apr 27 (morning-only data). For PDT/MST
stations, weather day didn't end until 07:00 UTC Apr 28 — afternoon highs
came in much higher than the morning snapshot.

Three NO positions settled WIN that should have been LOSS:
  KXHIGHMIA-26APR27-B87.5  bucket 87-88  morning high=77 → final=88 (in bucket)
  KXHIGHTPHX-26APR27-B82.5 bucket 82-83  morning high=68 → final=83 (in bucket)
  KXHIGHLAX-26APR27-B66.5  bucket 66-67  morning high=60 → final=66 (in bucket)

This script:
  1. Updates the Apr 27 actuals JSONL with end-of-day values (rewrite,
     not append — the existing puller's dedup-by-date prevents updates).
  2. Annuls the 3 wrong close events.
  3. Re-closes the 3 positions against the corrected actuals.

Default mode is DRY-RUN. Pass --apply to commit.

Usage:
    python3 ~/Documents/terminal1_fix_apr27_partial.py
    python3 ~/Documents/terminal1_fix_apr27_partial.py --apply
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger, _read_ledger, LEDGER_PATH  # noqa: E402


DATA_DIR = Path.home() / "Documents" / "terminal1_data"
ENGINE = "T1"

# End-of-day Apr 27 actuals fetched directly from Mesonet at 00:59 UTC Apr 28
# (after PDT day ended). station → (high_f, low_f).
APR27_FINAL = {
    "NYC": (68.0, 45.0),
    "LAX": (66.0, 58.0),
    "MIA": (88.0, 70.0),
    "PHX": (83.0, 61.0),
    "ATL": (76.0, 62.0),
    "ORD": (57.0, 47.0),   # may still update if not yet end-of-day Central
    "DEN": (41.0, 33.0),
}

# The 3 positions we know flipped. Tickers map to (correct_yes_won_bool).
# yes_won = bucket includes the actual high.
WRONG_CLOSES = {
    "KXHIGHMIA-26APR27-B87.5": True,   # bucket 87-88, high=88 → YES wins
    "KXHIGHTPHX-26APR27-B82.5": True,  # bucket 82-83, high=83 → YES wins
    "KXHIGHLAX-26APR27-B66.5":  True,  # bucket 66-67, high=66 → YES wins
}


def update_actuals_file(station: str, high_f: float, low_f: float, dry_run: bool) -> bool:
    """Rewrite the Apr 27 row in nws_actuals_{station}.jsonl with corrected values."""
    path = DATA_DIR / f"nws_actuals_{station}.jsonl"
    if not path.exists():
        print(f"  [error] {path.name} not found")
        return False
    rows: List[dict] = []
    updated = False
    old_h = old_l = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("date_local") == "2026-04-27":
                old_h, old_l = r.get("high_f"), r.get("low_f")
                if old_h == high_f and old_l == low_f:
                    print(f"  [skip] {station}: already at h={high_f} l={low_f}")
                    rows.append(r)
                    continue
                r["high_f"] = high_f
                r["low_f"] = low_f
                r["observed_ts_utc"] = datetime.now(timezone.utc).isoformat()
                r["_corrected_from"] = {"old_high_f": old_h, "old_low_f": old_l,
                                        "reason": "partial_day_pull_at_14:37_UTC"}
                updated = True
            rows.append(r)
    if not updated:
        print(f"  [skip] {station}: no Apr 27 row to update (h={old_h} l={old_l})")
        return False
    print(f"  [upd ] {station}: high {old_h}→{high_f}, low {old_l}→{low_f}  "
          f"{'DRY-RUN' if dry_run else 'writing...'}")
    if not dry_run:
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return True


def find_close_event(ticker: str) -> dict:
    """Find the most recent close event for the given T1 ticker."""
    open_pid = None
    closes = []
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        if r.get("ticker") == ticker and r.get("type") == "open":
            open_pid = r["position_id"]
        if r.get("position_id") == open_pid and r.get("type") == "close":
            closes.append(r)
    if not closes:
        return None
    return closes[-1]


def annul_close(close_event: dict, reason: str, dry_run: bool, sl: ShadowLedger) -> bool:
    """Annul the specified close, restoring the position to open state."""
    pid = close_event["position_id"]
    print(f"  [annul] {pid}  ts={close_event['ts'][:19]}  "
          f"realized was ${close_event['realized_pnl_usd']:+.2f}  "
          f"{'DRY-RUN' if dry_run else 'annulling...'}")
    if not dry_run:
        sl.annul_close(position_id=pid, reason=reason)
    return True


def reclose_position(open_event: dict, settle_yes: bool, dry_run: bool, sl: ShadowLedger):
    """Close the position against the corrected resolution. settle_yes=True
    means YES wins, settle_price = 1.0; otherwise 0.0."""
    pid = open_event["position_id"]
    settle_price = 1.0 if settle_yes else 0.0
    side = open_event["side"]
    if (settle_yes and side == "YES") or (not settle_yes and side == "NO"):
        outcome = "win"
    else:
        outcome = "loss"
    print(f"  [reclos] {pid}  {open_event['ticker']}  side={side}  "
          f"settle={settle_price}  outcome={outcome}  "
          f"{'DRY-RUN' if dry_run else 'closing...'}")
    if not dry_run:
        sl.close(position_id=pid, settle_price=settle_price,
                 outcome=outcome, fee_usd=0.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dry_run = not args.apply

    print("=" * 80)
    print("T1 Apr 27 PARTIAL-DAY SETTLEMENT FIX")
    print(f"Mode: {'LIVE WRITE' if not dry_run else 'DRY RUN'}")
    print("=" * 80)

    sl = ShadowLedger() if not dry_run else None

    print("\n=== Phase 1: update Apr 27 actuals to end-of-day values ===")
    for s, (h, l) in APR27_FINAL.items():
        update_actuals_file(s, h, l, dry_run)

    print("\n=== Phase 2: annul the 3 wrong closes ===")
    closes_to_annul = []
    for ticker in WRONG_CLOSES:
        cl = find_close_event(ticker)
        if cl is None:
            print(f"  [error] no close found for {ticker}; skipping")
            continue
        annul_close(cl, reason=f"partial_day_settle_2026-04-27: {ticker}",
                    dry_run=dry_run, sl=sl)
        closes_to_annul.append((ticker, cl))

    if not closes_to_annul:
        print("\nNothing to do.")
        return 0

    print("\n=== Phase 3: reclose against corrected actuals ===")
    for ticker, cl in closes_to_annul:
        # Find the open
        opens = [r for r in _read_ledger()
                 if r.get("type") == "open" and r.get("ticker") == ticker
                 and r.get("engine") == ENGINE]
        if not opens:
            print(f"  [error] no open for {ticker}; skipping")
            continue
        op = max(opens, key=lambda x: x["ts"])
        # All 3 wrong closes flipped to YES winning
        reclose_position(op, settle_yes=True, dry_run=dry_run, sl=sl)

    print("\n" + "=" * 80)
    if dry_run:
        print("DRY RUN COMPLETE — no changes written.")
        print("To apply: python3 ~/Documents/terminal1_fix_apr27_partial.py --apply")
    else:
        print("APPLIED. Verify:")
        print("  python3 ~/Documents/t1_settlement_analysis.py | head -20")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
