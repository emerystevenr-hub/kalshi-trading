"""Shadow P&L Ledger — single source of truth for simulated trades across all
engines (T1 weather, T2 catalyst, T3a Fed, etc.).

Each engine has a configured starting bankroll (default $5,000). Positions are
opened and closed through this module; realized P&L is computed from the
ledger by replay. Ledger is append-only JSONL — crash-safe, auditable.

Files:
    ~/Documents/shadow_pnl/engines.json  — engine metadata + starting bankroll
    ~/Documents/shadow_pnl/ledger.jsonl  — append-only events (open/close)

Row schema:
    {
      "type": "open",
      "ts": "2026-04-23T23:30:00+00:00",
      "engine": "T2",
      "position_id": "8f6...",
      "venue": "kalshi",
      "ticker": "KXHIGHNYC-26APR24-B73.5",
      "side": "YES",           # "YES" or "NO"
      "price": 0.45,            # entry price in dollars (0.01..0.99)
      "size": 10,               # contracts
      "cost_usd": 4.50,         # size × price
      "fee_usd": 0.00,          # optional, defaults to 0 for shadow
      "max_payout_usd": 10.00,  # size × $1 (if settles in your favor)
      "reason": "Weather model ensemble says 73.5F avg over 3 runs",
      "signal_metadata": {...}  # engine-specific context
    }

    {
      "type": "close",
      "ts": "...",
      "position_id": "8f6...",
      "settle_price": 1.00,         # 1.00 if YES resolved true, 0 if false
      "proceeds_usd": 10.00,         # payout received
      "fee_usd": 0.00,
      "realized_pnl_usd": 5.50,      # proceeds - cost - fees
      "outcome": "win"               # "win", "loss", "partial", "refund"
    }

Usage as a library:
    from shadow_pnl_core import ShadowLedger
    sl = ShadowLedger()
    pos_id = sl.open(engine="T2", venue="kalshi",
                     ticker="KXHIGHNYC-26APR24-B73.5",
                     side="YES", price=0.45, size=10,
                     reason="manual pick — forecast agrees")
    # later, on settlement:
    sl.close(position_id=pos_id, settle_price=1.00, outcome="win")

Usage as a CLI:
    python3 shadow_pnl_core.py status
    python3 shadow_pnl_core.py list-open
    python3 shadow_pnl_core.py list-open --engine T2
"""

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


SHADOW_DIR = Path.home() / "Documents" / "shadow_pnl"
ENGINES_PATH = SHADOW_DIR / "engines.json"
LEDGER_PATH = SHADOW_DIR / "ledger.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_engines() -> Dict[str, dict]:
    if not ENGINES_PATH.exists():
        raise FileNotFoundError(
            f"Missing {ENGINES_PATH}. Create it with engine metadata first."
        )
    return json.loads(ENGINES_PATH.read_text())


def _append(row: dict) -> None:
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")


def _read_ledger() -> List[dict]:
    if not LEDGER_PATH.exists():
        return []
    out = []
    with open(LEDGER_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


class ShadowLedger:
    """Thin wrapper — library interface for engines to log shadow trades."""

    def __init__(self):
        self.engines = _read_engines()

    def open(
        self,
        engine: str,
        venue: str,
        ticker: str,
        side: str,
        price: float,
        size: int,
        reason: str = "",
        fee_usd: float = 0.0,
        signal_metadata: Optional[dict] = None,
    ) -> str:
        if engine not in self.engines:
            raise ValueError(f"Unknown engine: {engine}. See engines.json.")
        if side not in ("YES", "NO"):
            raise ValueError(f"side must be YES or NO, got {side!r}")
        if not 0.0 < price < 1.0:
            raise ValueError(f"price must be in (0,1) dollars, got {price}")
        if size <= 0:
            raise ValueError(f"size must be positive, got {size}")
        position_id = uuid.uuid4().hex[:12]
        cost = round(price * size, 4)
        row = {
            "type": "open",
            "ts": _now(),
            "engine": engine,
            "position_id": position_id,
            "venue": venue,
            "ticker": ticker,
            "side": side,
            "price": price,
            "size": size,
            "cost_usd": cost,
            "fee_usd": fee_usd,
            "max_payout_usd": round(float(size), 4),
            "reason": reason,
            "signal_metadata": signal_metadata or {},
        }
        _append(row)
        return position_id

    def close(
        self,
        position_id: str,
        settle_price: float,
        outcome: str = "win",
        fee_usd: float = 0.0,
    ) -> dict:
        """Close a position. settle_price is the contract's terminal value:
        1.00 if YES resolved true (full payout), 0.00 if false. For partial
        unwinds or early exits, pass the exit market price instead (0..1).
        """
        ledger = _read_ledger()
        open_row = None
        for r in ledger:
            if r["type"] == "open" and r["position_id"] == position_id:
                open_row = r
                break
        if open_row is None:
            raise ValueError(f"No open position with id {position_id}")
        # Determine current state by finding the LAST event for this position_id.
        # An annul_close after a close means the close was reversed → still open.
        # The most recent event determines current state.
        last_event = None
        for r in ledger:
            if r.get("position_id") == position_id:
                last_event = r
        if last_event is None or last_event["type"] == "close":
            if last_event is not None and last_event["type"] == "close":
                raise ValueError(
                    f"Position {position_id} already closed at {last_event['ts']}"
                )

        size = open_row["size"]
        cost = open_row["cost_usd"]
        open_fee = open_row.get("fee_usd", 0.0)

        if open_row["side"] == "YES":
            proceeds = round(settle_price * size, 4)
        else:  # NO: pays 1 if YES resolved false
            proceeds = round((1.0 - settle_price) * size, 4)

        realized_pnl = round(proceeds - cost - open_fee - fee_usd, 4)

        row = {
            "type": "close",
            "ts": _now(),
            "position_id": position_id,
            "engine": open_row["engine"],
            "settle_price": settle_price,
            "proceeds_usd": proceeds,
            "fee_usd": fee_usd,
            "realized_pnl_usd": realized_pnl,
            "outcome": outcome,
        }
        _append(row)
        return row

    def annul_close(
        self,
        position_id: str,
        reason: str = "",
    ) -> dict:
        """Reverse a previous close event. Use when a position was closed
        prematurely (e.g. on partial-day data) and needs to be returned to
        open state. Subtracts the closed P&L from realized totals during
        replay; the position appears open again until a new close fires.

        After annul, the position can be closed again normally — the most
        recent event for the position_id is now the annul_close, which
        compute_state treats as "open."
        """
        ledger = _read_ledger()
        # Find the most recent close for this pid that hasn't been annulled
        last_close = None
        last_annul = None
        for r in ledger:
            if r.get("position_id") != position_id:
                continue
            if r["type"] == "close":
                last_close = r
                last_annul = None  # reset annul tracking when a new close fires
            elif r["type"] == "annul_close":
                last_annul = r
        if last_close is None:
            raise ValueError(f"No close event found for {position_id} to annul")
        if last_annul is not None:
            raise ValueError(
                f"Position {position_id} most recent close already annulled at "
                f"{last_annul['ts']}"
            )

        row = {
            "type": "annul_close",
            "ts": _now(),
            "position_id": position_id,
            "engine": last_close.get("engine"),
            "annulled_close_ts": last_close["ts"],
            "annulled_realized_pnl_usd": last_close["realized_pnl_usd"],
            "reason": reason or "manual annul",
        }
        _append(row)
        return row


# ---------------------------------------------------------------------------
# State reconstruction — current balance + open positions per engine
# ---------------------------------------------------------------------------

def compute_state() -> Dict[str, dict]:
    """Replay ledger, return per-engine state:
        {engine_id: {
            bankroll_start_usd, realized_pnl_usd, cost_tied_up_usd,
            current_balance_usd, n_open, n_closed_win, n_closed_loss,
            positions_open: [...], recent_closes: [...]
        }}
    """
    engines = _read_engines()
    ledger = _read_ledger()

    state: Dict[str, dict] = {}
    for eid, meta in engines.items():
        state[eid] = {
            "name": meta["name"],
            "mode": meta.get("mode", "unknown"),
            "active": meta.get("active", False),
            "bankroll_start_usd": meta["bankroll_usd"],
            "realized_pnl_usd": 0.0,
            "cost_tied_up_usd": 0.0,
            "n_open": 0,
            "n_closed_win": 0,
            "n_closed_loss": 0,
            "n_closed_other": 0,
            "positions_open": [],
            "recent_closes": [],
        }

    # Replay events in chronological order. open/close mutate state directly.
    # annul_close removes the most recent close for a position (which itself
    # came earlier in the log), restores the position to open, and excludes
    # the close's P&L from realized totals.
    open_positions: Dict[str, dict] = {}
    open_record_by_pid: Dict[str, dict] = {}   # most-recent open ever seen
    close_events: List[dict] = []
    annulled_close_ts_by_pid: Dict[str, set] = {}  # pid → set of annulled close ts

    for r in ledger:
        t = r.get("type")
        pid = r.get("position_id")
        if t == "open":
            open_positions[pid] = r
            open_record_by_pid[pid] = r
        elif t == "close":
            close_events.append(r)
            open_positions.pop(pid, None)
        elif t == "annul_close":
            # Mark the annulled close ts so we can exclude its P&L below.
            annul_ts = r.get("annulled_close_ts")
            annulled_close_ts_by_pid.setdefault(pid, set()).add(annul_ts)
            # Restore the position to open state.
            orig_open = open_record_by_pid.get(pid)
            if orig_open is not None:
                open_positions[pid] = orig_open

    # Filter out annulled closes from the realized P&L pass below.
    close_events = [
        c for c in close_events
        if c["ts"] not in annulled_close_ts_by_pid.get(c.get("position_id"), set())
    ]

    for pid, p in open_positions.items():
        eid = p["engine"]
        if eid not in state:
            continue  # engine removed from config
        state[eid]["n_open"] += 1
        state[eid]["cost_tied_up_usd"] += p["cost_usd"] + p.get("fee_usd", 0.0)
        state[eid]["positions_open"].append({
            "position_id": pid,
            "ticker": p["ticker"],
            "side": p["side"],
            "price": p["price"],
            "size": p["size"],
            "cost_usd": p["cost_usd"],
            "opened_ts": p["ts"],
            "reason": p.get("reason", ""),
        })

    for c in close_events:
        eid = c.get("engine")
        if eid not in state:
            continue
        state[eid]["realized_pnl_usd"] += c["realized_pnl_usd"]
        if c["outcome"] == "win":
            state[eid]["n_closed_win"] += 1
        elif c["outcome"] == "loss":
            state[eid]["n_closed_loss"] += 1
        else:
            state[eid]["n_closed_other"] += 1

    # Keep last 5 closes per engine
    for c in close_events[-50:]:  # iterate over recent batch
        eid = c.get("engine")
        if eid in state:
            state[eid]["recent_closes"].append(c)
    for eid in state:
        state[eid]["recent_closes"] = state[eid]["recent_closes"][-5:]

    # Final: current balance = starting + realized - cost_tied_up_in_open
    for eid, s in state.items():
        s["realized_pnl_usd"] = round(s["realized_pnl_usd"], 2)
        s["cost_tied_up_usd"] = round(s["cost_tied_up_usd"], 2)
        s["current_balance_usd"] = round(
            s["bankroll_start_usd"] + s["realized_pnl_usd"] - s["cost_tied_up_usd"],
            2,
        )

    return state


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    state = compute_state()
    print(f"{'engine':<6} {'name':<32} {'mode':<10} {'start':>9} {'real_PnL':>10} {'tied_up':>10} {'curr_bal':>10} {'open':>5} {'W':>3} {'L':>3}")
    print("-" * 112)
    total_start = total_real = total_tied = total_bal = 0.0
    for eid, s in state.items():
        mark = "✓" if s["active"] else "·"
        name = s["name"][:30]
        print(f"{mark} {eid:<4} {name:<32} {s['mode']:<10} "
              f"${s['bankroll_start_usd']:>7,.0f} "
              f"${s['realized_pnl_usd']:>+8,.2f} "
              f"${s['cost_tied_up_usd']:>8,.2f} "
              f"${s['current_balance_usd']:>8,.2f} "
              f"{s['n_open']:>5} {s['n_closed_win']:>3} {s['n_closed_loss']:>3}")
        total_start += s["bankroll_start_usd"]
        total_real += s["realized_pnl_usd"]
        total_tied += s["cost_tied_up_usd"]
        total_bal += s["current_balance_usd"]
    print("-" * 112)
    print(f"{'':<6} {'PORTFOLIO':<32} {'':<10} "
          f"${total_start:>7,.0f} "
          f"${total_real:>+8,.2f} "
          f"${total_tied:>8,.2f} "
          f"${total_bal:>8,.2f}")
    return 0


def cmd_list_open(args) -> int:
    state = compute_state()
    any_shown = False
    for eid, s in state.items():
        if args.engine and eid != args.engine:
            continue
        if not s["positions_open"]:
            continue
        any_shown = True
        print(f"\n=== {eid} — {s['name']} — {s['n_open']} open positions ===")
        for p in s["positions_open"]:
            print(f"  {p['position_id']}  {p['ticker']:<40} {p['side']} "
                  f"{p['size']}@${p['price']:.2f} cost=${p['cost_usd']:.2f} "
                  f"opened={p['opened_ts'][:19]}")
            if p["reason"]:
                print(f"    reason: {p['reason'][:100]}")
    if not any_shown:
        print("(no open positions)")
    return 0


def cmd_ledger_tail(args) -> int:
    rows = _read_ledger()[-args.n:]
    for r in rows:
        print(json.dumps(r))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Shadow P&L ledger CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="Per-engine balances + portfolio total")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("list-open", help="List open positions")
    sp.add_argument("--engine", default=None, help="Filter to one engine (e.g. T2)")
    sp.set_defaults(func=cmd_list_open)

    sp = sub.add_parser("tail", help="Print last N ledger rows (raw JSON)")
    sp.add_argument("-n", type=int, default=20)
    sp.set_defaults(func=cmd_ledger_tail)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
