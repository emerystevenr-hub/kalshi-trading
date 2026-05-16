"""Terminal 3a — Fed Decision Settlement Reconciler.

Closes T3a shadow positions when their Kalshi KXFEDDECISION events resolve.
Each Fed decision results in EXACTLY ONE YES winner across the event's legs
— so for a Dutch arb opened by the executor (multiple legs sharing an
arb_group_id), this reconciler closes all legs simultaneously when Kalshi
finalizes the event.

Mirrors terminal2_settlement_reconciler.py with two differences:
  1. engine="T3a" filter
  2. Logs per-arb_group realized P&L for retrospective edge analysis

Kalshi status semantics (lesson from T2 reconciler 2026-05-02):
  Both 'settled' AND 'finalized' are terminal. The `result` field is the
  source of truth — yes / no.

Usage:
    python3 ~/Documents/terminal3a_settlement_reconciler.py --once --dry-run
    python3 ~/Documents/terminal3a_settlement_reconciler.py --once
    nohup caffeinate -is python3 ~/Documents/terminal3a_settlement_reconciler.py \\
        --interval-sec 1800 > ~/Documents/terminal3a_data/reconciler.out 2>&1 &
"""

import argparse
import json
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger, _read_ledger  # noqa: E402


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path.home() / "Documents" / "terminal3a_data"
LOG_PATH = DATA_DIR / "settlement_reconciler.log"
ARB_LEDGER_PATH = DATA_DIR / "arb_group_realized.jsonl"
ENGINE = "T3a"
REQUEST_TIMEOUT = 20

_STOP = False


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _handle_sigint(sig, frame):
    global _STOP
    _STOP = True
    log("SIGINT — exiting after current iteration.")


def fetch_market(ticker: str) -> Optional[dict]:
    backoff = 1.0
    for attempt in range(4):
        try:
            r = requests.get(
                f"{KALSHI_BASE}/markets/{ticker}",
                timeout=REQUEST_TIMEOUT,
                proxies={"http": None, "https": None},
            )
            if r.status_code == 429:
                log(f"  [rate-limit] {ticker} backoff {backoff:.1f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 16.0)
                continue
            r.raise_for_status()
            return r.json().get("market")
        except requests.RequestException as e:
            log(f"  [error] fetch {ticker}: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 16.0)
    return None


def find_open_t3a_positions() -> List[dict]:
    open_map: Dict[str, dict] = {}
    annulled_close_ts: Dict[str, set] = {}
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        t = r.get("type")
        pid = r.get("position_id")
        if t == "open":
            open_map[pid] = r
        elif t == "close":
            if r.get("ts") not in annulled_close_ts.get(pid, set()):
                open_map.pop(pid, None)
        elif t == "annul_close":
            annulled_close_ts.setdefault(pid, set()).add(r.get("annulled_close_ts"))
    return list(open_map.values())


def kalshi_resolution(market: dict) -> Optional[str]:
    """Return 'yes' / 'no' / None. Treats both 'settled' and 'finalized'
    as terminal statuses (T2 lesson)."""
    result = (market.get("result") or "").lower()
    status = (market.get("status") or "").lower()
    if result not in ("yes", "no"):
        return None
    if status not in ("settled", "finalized"):
        log(f"  [warn] {market.get('ticker')} has result={result!r} but "
            f"status={status!r}; skipping")
        return None
    return result


def determine_outcome(side: str, kalshi_result: str) -> str:
    yes_won = (kalshi_result == "yes")
    if side == "YES":
        return "win" if yes_won else "loss"
    return "win" if not yes_won else "loss"


def write_arb_group_realized(arb_group_id: str, event_ticker: str,
                             flag: str, total_realized: float,
                             total_cost: float, n_legs: int) -> None:
    """Append a per-arb-group realization record for retrospective analysis."""
    record = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "arb_group_id": arb_group_id,
        "event_ticker": event_ticker,
        "flag": flag,
        "n_legs": n_legs,
        "total_cost_usd": round(total_cost, 4),
        "total_realized_pnl_usd": round(total_realized, 4),
        "roi_pct": round((total_realized / total_cost) * 100, 2) if total_cost else 0.0,
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(ARB_LEDGER_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        log(f"  [warn] arb_group_realized write failed: {e}")


def reconcile_once(dry_run: bool) -> dict:
    positions = find_open_t3a_positions()
    log(f"open T3a positions: {len(positions)}")
    if not positions:
        return {"closed": 0, "pending": 0, "errors": 0, "groups_finalized": 0}

    sl = ShadowLedger() if not dry_run else None
    closed = 0
    pending = 0
    errors = 0

    # Group positions by arb_group_id for retrospective P&L summary
    groups: Dict[str, list] = defaultdict(list)
    closes_this_pass: Dict[str, list] = defaultdict(list)

    for pos in positions:
        ticker = pos.get("ticker")
        if not ticker:
            errors += 1
            continue
        md = pos.get("signal_metadata") or {}
        arb_group_id = md.get("arb_group_id", "")
        groups[arb_group_id].append(pos)

        market = fetch_market(ticker)
        if market is None:
            errors += 1
            continue

        resolution = kalshi_resolution(market)
        if resolution is None:
            pending += 1
            continue

        side = (pos.get("side") or "").upper()
        settle_price = 1.0 if resolution == "yes" else 0.0
        outcome = determine_outcome(side, resolution)

        size = pos.get("size", 0)
        cost = pos.get("cost_usd", 0)
        if side == "YES":
            proceeds = settle_price * size
        else:
            proceeds = (1.0 - settle_price) * size
        realized = proceeds - cost - pos.get("fee_usd", 0)

        log(f"  [close] {pos['position_id']}  {ticker:<40} {side} @${pos.get('price', 0):.4f}  "
            f"kalshi_result={resolution.upper()}  outcome={outcome}  "
            f"realized=${realized:+.2f}  "
            f"{'DRY-RUN' if dry_run else 'closing...'}")

        if not dry_run:
            sl.close(
                position_id=pos["position_id"],
                settle_price=settle_price,
                outcome=outcome,
                fee_usd=0.0,
            )
        closed += 1

        if arb_group_id:
            closes_this_pass[arb_group_id].append({
                "position_id": pos["position_id"],
                "ticker": ticker,
                "side": side,
                "cost_usd": cost,
                "fee_usd": pos.get("fee_usd", 0),
                "realized_pnl_usd": realized,
                "kalshi_result": resolution,
                "flag": md.get("flag", ""),
                "event_ticker": md.get("event_ticker", ""),
            })

    # Per-arb-group rollup — only if ALL legs of the group closed in this pass
    groups_finalized = 0
    for arb_group_id, closed_legs in closes_this_pass.items():
        all_legs = groups.get(arb_group_id, [])
        if len(closed_legs) != len(all_legs):
            continue
        total_realized = sum(c["realized_pnl_usd"] for c in closed_legs)
        total_cost = sum(c["cost_usd"] + c.get("fee_usd", 0) for c in closed_legs)
        flag = closed_legs[0]["flag"]
        event_ticker = closed_legs[0]["event_ticker"]
        log(f"  [arb_group] {arb_group_id}  event={event_ticker}  flag={flag}  "
            f"legs={len(closed_legs)}  cost=${total_cost:.2f}  "
            f"realized=${total_realized:+.2f}  "
            f"ROI={(total_realized/total_cost)*100:+.1f}%")
        if not dry_run and arb_group_id:
            write_arb_group_realized(arb_group_id, event_ticker, flag,
                                     total_realized, total_cost, len(closed_legs))
        groups_finalized += 1

    return {"closed": closed, "pending": pending, "errors": errors,
            "groups_finalized": groups_finalized}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=1800,
                    help="daemon poll interval (default 30 min)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    log(f"T3a Settlement Reconciler starting. dry_run={args.dry_run} "
        f"once={args.once} interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            r = reconcile_once(args.dry_run)
            log(f"loop #{loops}: closed={r['closed']} "
                f"pending={r['pending']} errors={r['errors']} "
                f"arb_groups_finalized={r['groups_finalized']}")
        except Exception as e:
            log(f"[error] reconcile_once: {type(e).__name__}: {e}")
        if args.once or _STOP:
            break
        end = time.time() + args.interval_sec
        while time.time() < end and not _STOP:
            time.sleep(min(1.0, end - time.time()))

    log(f"Reconciler stopped. Loops: {loops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
