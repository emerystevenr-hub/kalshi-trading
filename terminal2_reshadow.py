"""Terminal 2 — One-shot Phase 0 evidence-driven rebalance.

Closes 5 long-dated tail_probability positions (Phase 0 backtest showed
NO rate = 50% on this thesis category — no edge), opens 4 new short-DTR
calendar_fade positions (Phase 0 NO rate = 94%, validated edge).

Default mode is DRY-RUN. Pass --apply to actually write to the ledger.

Audit trail:
  - Every close event uses ShadowLedger.close() (standard schema)
  - A companion `reshadow_note` event is appended to the ledger with the
    full reasoning. Replay code matches on type∈{open,close,annul_close}
    so the note is preserved as audit metadata without disturbing P&L
    accounting.
  - Every new open carries full signal_metadata: sub_engine, target_price,
    stop_price, correlation_group, thesis — so terminal2_mark_drift.py can
    score them from day one.

Source of truth for the plan: T5 thesis_review_latest.md (post-patch),
2026-04-26 ~17:50 UTC. Top short-DTR calendar_fade candidates picked one
per correlation group, sized to ≤$75 per position.

Usage:
    # See what would happen — no writes:
    python3 ~/Documents/terminal2_reshadow.py

    # Commit the rebalance:
    python3 ~/Documents/terminal2_reshadow.py --apply
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger, _read_ledger, LEDGER_PATH  # noqa: E402


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ENGINE = "T2"
RESHADOW_TAG = "reshadow_phase0_evidence_2026-04-26"

# --- Plan: positions to CLOSE (the 5 long-dated tail_probability bets) ---
CLOSE_TICKERS = [
    "KXGOVTSHUTLENGTH-26FEB07-G100",
    "KXMJSCHEDULE-27",
    "KXFDAAPPROVALPSYCHEDELIC-27-ANYPSYCH",
    "KXTRUMPADMINLEAVE-26DEC31-KLEA",
    "KXTRUMPADMINLEAVE-26DEC31-PHEG",
]

CLOSE_REASON = (
    "Phase 0 thesis backtest (2026-04-26, n=26 settled markets in T2-relevant "
    "series) showed tail_probability NO rate = 50% (no edge), vs calendar_fade "
    "NO rate = 94% (validated). Closing all 5 long-DTR tail positions to "
    "redeploy capital into validated short-DTR calendar_fade book."
)

# --- Plan: new positions to OPEN ---
OPEN_PLAN = [
    {
        "ticker": "KXKASHOUT-26APR-JUN01",
        "side": "NO",
        "max_entry": 0.47,
        "size": 150,
        "target_price": 0.57,
        "stop_price": 0.26,
        "our_probability": 0.29,
        "sub_engine": "calendar_fade",
        "correlation_group": "KXKASHOUT-26APR",
        "thesis": (
            "Kash Patel out as FBI Director by Jun 1, 2026. Market 57% YES, "
            "base-rate prior ~29%, edge $0.285. T5 score 0.101 (top-ranked "
            "in 2026-04-26 scan). DTR 35d."
        ),
    },
    {
        "ticker": "KXFISAEXTEND-26APR-MAY01",
        "side": "NO",
        "max_entry": 0.45,
        "size": 167,
        "target_price": 0.55,
        "stop_price": 0.24,
        "our_probability": 0.29,
        "sub_engine": "calendar_fade",
        "correlation_group": "KXFISAEXTEND-26APR",
        "thesis": (
            "FISA 702 reauthorization legislation becomes law by May 1, 2026. "
            "Market 58% YES, base-rate prior ~29%, edge $0.292. DTR 5d — "
            "fastest-settling new position; first data point ~May 1."
        ),
    },
    {
        "ticker": "KXVOTEHUBTRUMPUPDOWN-26APR30",
        "side": "NO",
        "max_entry": 0.54,
        "size": 139,
        "target_price": 0.64,
        "stop_price": 0.33,
        "our_probability": 0.25,
        "sub_engine": "calendar_fade",
        "correlation_group": "KXVOTEHUBTRUMPUPDOWN-26APR30",
        "thesis": (
            "Trump approval >38.7% on Apr 30. Market 50% YES, base-rate prior "
            "~25%, edge $0.247. DTR 4d — fastest-settling. Tests calendar_fade "
            "thesis on a non-departure-style market."
        ),
    },
    {
        "ticker": "KXLEAVEPOWELLGOV-26AUG01-JUN",
        "side": "NO",
        "max_entry": 0.54,
        "size": 139,
        "target_price": 0.64,
        "stop_price": 0.33,
        "our_probability": 0.25,
        "sub_engine": "calendar_fade",
        "correlation_group": "KXLEAVEPOWELLGOV-26AUG01",
        "thesis": (
            "Powell leaves Fed governor role by Aug 1. Market 50% YES, "
            "base-rate prior ~25%, edge $0.247. DTR 35d. Diversifies the "
            "book into Fed/monetary thesis space."
        ),
    },
]


# --------------------------------------------------------------------------
# Kalshi mark fetch
# --------------------------------------------------------------------------

def fetch_market(ticker: str) -> Optional[dict]:
    """Fetch with backoff on 429 (Kalshi rate-limits aggressively when
    multiple market lookups fire in quick succession)."""
    import time as _time
    backoff = 1.0
    for attempt in range(5):
        try:
            r = requests.get(
                f"{KALSHI_BASE}/markets/{ticker}",
                timeout=20,
                proxies={"http": None, "https": None},
            )
            if r.status_code == 429:
                print(f"  [rate-limit] {ticker} backing off {backoff:.1f}s "
                      f"(attempt {attempt+1}/5)")
                _time.sleep(backoff)
                backoff = min(backoff * 2, 16.0)
                continue
            r.raise_for_status()
            return r.json().get("market")
        except requests.RequestException as e:
            print(f"  [error] fetch {ticker} attempt {attempt+1}: {e}")
            _time.sleep(backoff)
            backoff = min(backoff * 2, 16.0)
    print(f"  [error] fetch {ticker}: gave up after 5 attempts")
    return None


def _f(v) -> float:
    try:
        return float(v) if v is not None and v != "" else 0.0
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# Open-position discovery
# --------------------------------------------------------------------------

def find_open_t2_by_ticker(ticker: str) -> Optional[dict]:
    """Find the currently-open T2 position for this ticker, or None."""
    opens = {}
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        pid = r.get("position_id")
        t = r.get("type")
        if t == "open" and r.get("ticker") == ticker:
            opens[pid] = r
        elif t == "close" and pid in opens:
            opens.pop(pid, None)
    if not opens:
        return None
    return max(opens.values(), key=lambda x: x.get("ts", ""))


# --------------------------------------------------------------------------
# Close (Phase 1)
# --------------------------------------------------------------------------

def close_position(ticker: str, dry_run: bool, sl: Optional[ShadowLedger]) -> Optional[float]:
    pos = find_open_t2_by_ticker(ticker)
    if pos is None:
        print(f"  [skip] no open T2 position for {ticker}")
        return None

    market = fetch_market(ticker)
    if market is None:
        print(f"  [error] cannot fetch market for {ticker}; skipping")
        return None

    yes_bid = _f(market.get("yes_bid_dollars"))
    yes_ask = _f(market.get("yes_ask_dollars"))
    side = pos["side"]

    # settle_price for early close = the YES-side market price we'd cross at.
    # shadow_pnl_core's close() computes proceeds:
    #   YES position: proceeds = settle_price × size  (sell at yes_bid)
    #   NO  position: proceeds = (1 - settle_price) × size  (cross to yes_ask)
    if side == "YES":
        settle_price = yes_bid if yes_bid > 0 else 0.5
    else:
        settle_price = yes_ask if yes_ask > 0 else (1.0 - yes_bid if yes_bid > 0 else 0.5)

    entry = pos["price"]
    size = pos["size"]
    cost = pos["cost_usd"]
    open_fee = pos.get("fee_usd", 0.0)
    proceeds = (1.0 - settle_price) * size if side == "NO" else settle_price * size
    realized = round(proceeds - cost - open_fee, 4)
    outcome = "win" if realized > 0 else "loss"

    tag = "DRY-RUN" if dry_run else "closing..."
    print(f"  [close] {pos['position_id']}  {ticker:<42} {side} "
          f"entry=${entry:.2f}  settle=${settle_price:.2f}  proceeds=${proceeds:.2f}  "
          f"realized=${realized:+.2f}  outcome={outcome}  {tag}")

    if not dry_run and sl is not None:
        sl.close(
            position_id=pos["position_id"],
            settle_price=settle_price,
            outcome=outcome,
            fee_usd=0.0,
        )
        # Audit note — separate event, doesn't disturb P&L replay.
        note = {
            "type": "reshadow_note",
            "ts": datetime.now(timezone.utc).isoformat(),
            "position_id": pos["position_id"],
            "ticker": ticker,
            "engine": ENGINE,
            "phase": "close",
            "tag": RESHADOW_TAG,
            "reason": CLOSE_REASON,
            "context": {
                "entry": entry,
                "settle_price": settle_price,
                "realized_pnl": realized,
                "side": side,
                "yes_bid_at_close": yes_bid,
                "yes_ask_at_close": yes_ask,
            },
        }
        with open(LEDGER_PATH, "a") as f:
            f.write(json.dumps(note) + "\n")

    return realized


# --------------------------------------------------------------------------
# Open (Phase 2)
# --------------------------------------------------------------------------

def open_position(plan: dict, dry_run: bool, sl: Optional[ShadowLedger]) -> bool:
    ticker = plan["ticker"]
    market = fetch_market(ticker)
    if market is None:
        print(f"  [error] cannot fetch market for {ticker}; skipping")
        return False

    yes_bid = _f(market.get("yes_bid_dollars"))
    yes_ask = _f(market.get("yes_ask_dollars"))
    no_ask = _f(market.get("no_ask_dollars"))

    # Entry price (what we pay to open):
    #   NO: no_ask if quoted, else (1 - yes_bid)
    #   YES: yes_ask
    side = plan["side"]
    if side == "NO":
        entry = no_ask if no_ask > 0 else (1.0 - yes_bid if yes_bid > 0 else 0)
    else:
        entry = yes_ask if yes_ask > 0 else 0

    if entry <= 0 or entry >= 1:
        print(f"  [skip] {ticker:<42} bad entry={entry} (yes_bid={yes_bid}, yes_ask={yes_ask})")
        return False

    if entry > plan["max_entry"]:
        print(f"  [skip] {ticker:<42} {side} entry=${entry:.3f} > max=${plan['max_entry']:.3f} "
              f"(market moved against us)")
        return False

    size = plan["size"]
    cost = round(entry * size, 4)
    tag = "DRY-RUN" if dry_run else "opening..."
    print(f"  [open ] {ticker:<42} {side} @ ${entry:.3f}  size={size}  "
          f"cost=${cost:.2f}  target=${plan['target_price']:.2f}  "
          f"stop=${plan['stop_price']:.2f}  {tag}")

    if not dry_run and sl is not None:
        signal_metadata = {
            "sub_engine": plan["sub_engine"],
            "source": f"terminal2_reshadow ({RESHADOW_TAG})",
            "target_price": plan["target_price"],
            "stop_price": plan["stop_price"],
            "our_probability": plan["our_probability"],
            "correlation_group": plan["correlation_group"],
            "thesis": plan["thesis"],
            "reshadow_phase": "open",
            "yes_bid_at_open": yes_bid,
            "yes_ask_at_open": yes_ask,
        }
        pid = sl.open(
            engine=ENGINE,
            venue="kalshi",
            ticker=ticker,
            side=side,
            price=entry,
            size=size,
            fee_usd=0.0,
            reason=f"{RESHADOW_TAG}: " + plan["thesis"][:240],
            signal_metadata=signal_metadata,
        )
        print(f"           opened pid={pid}")
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write to ledger (default: dry-run)")
    args = ap.parse_args()
    dry_run = not args.apply

    sl = ShadowLedger() if not dry_run else None

    print("=" * 80)
    print("T2 RESHADOW — Phase 0 evidence-driven rebalance")
    print(f"Mode: {'LIVE WRITE (--apply)' if not dry_run else 'DRY RUN (default)'}")
    print(f"Tag:  {RESHADOW_TAG}")
    print("=" * 80)

    print(f"\n--- Phase 1: closing {len(CLOSE_TICKERS)} long-dated tail_probability positions ---")
    realized_total = 0.0
    closed_count = 0
    for tk in CLOSE_TICKERS:
        result = close_position(tk, dry_run, sl)
        if result is not None:
            realized_total += result
            closed_count += 1
    print(f"\nClosed: {closed_count}/{len(CLOSE_TICKERS)}  total realized=${realized_total:+.2f}")

    print(f"\n--- Phase 2: opening {len(OPEN_PLAN)} new short-DTR calendar_fade positions ---")
    opened_count = 0
    estimated_cost = 0.0
    for plan in OPEN_PLAN:
        if open_position(plan, dry_run, sl):
            opened_count += 1
            estimated_cost += plan["max_entry"] * plan["size"]
    print(f"\nOpened: {opened_count}/{len(OPEN_PLAN)}  estimated max-entry cost=${estimated_cost:.2f}")

    print()
    print("=" * 80)
    if dry_run:
        print("DRY RUN COMPLETE — no changes written.")
        print("To apply: python3 ~/Documents/terminal2_reshadow.py --apply")
    else:
        print("APPLIED.")
        print("Verify with: python3 ~/Documents/terminal2_mark_drift.py --report")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
