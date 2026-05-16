"""Terminal 3c — Initial Claims Settlement Reconciler.

Closes T3c shadow positions when KXJOBLESSCLAIMS markets resolve. Trusts
Kalshi as ground truth: when a market's `result` is yes/no AND `status`
is `settled` or `finalized`, the position closes.

Same pattern as T2 reconciler (the bug-fixed one that handles BOTH terminal
statuses). Per-event realized P&L logged to event_realized.jsonl for
retrospective edge analysis.

Usage:
    python3 ~/Documents/terminal3c_settlement_reconciler.py --once --dry-run
    python3 ~/Documents/terminal3c_settlement_reconciler.py --once
    nohup caffeinate -is python3 ~/Documents/terminal3c_settlement_reconciler.py \\
        --interval-sec 1800 > ~/Documents/terminal3c_data/reconciler.out 2>&1 &
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
DATA_DIR = Path.home() / "Documents" / "terminal3c_data"
LOG_PATH = DATA_DIR / "settlement_reconciler.log"
EVENT_LEDGER_PATH = DATA_DIR / "event_realized.jsonl"
ENGINE = "T3c"
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


def find_open_t3c_positions() -> List[dict]:
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
    """Return 'yes' / 'no' / None. Both 'settled' AND 'finalized' are
    terminal (T2 lesson). `result` is the source of truth."""
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


def write_event_realized(event_ticker: str, n_legs: int, total_cost: float,
                         total_realized: float, kalshi_actual_value: Optional[int],
                         legs: List[dict]) -> None:
    record = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "event_ticker": event_ticker,
        "n_legs": n_legs,
        "kalshi_actual_value": kalshi_actual_value,
        "total_cost_usd": round(total_cost, 4),
        "total_realized_pnl_usd": round(total_realized, 4),
        "roi_pct": round((total_realized / total_cost) * 100, 2) if total_cost else 0.0,
        "legs": legs,
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVENT_LEDGER_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        log(f"  [warn] event_realized write failed: {e}")


def reconcile_once(dry_run: bool) -> dict:
    positions = find_open_t3c_positions()
    log(f"open T3c positions: {len(positions)}")
    if not positions:
        return {"closed": 0, "pending": 0, "errors": 0, "events_finalized": 0}

    sl = ShadowLedger() if not dry_run else None
    closed = 0
    pending = 0
    errors = 0

    by_event: Dict[str, list] = defaultdict(list)
    closes_this_pass: Dict[str, list] = defaultdict(list)
    event_actual: Dict[str, Optional[int]] = {}

    for pos in positions:
        ticker = pos.get("ticker")
        if not ticker:
            errors += 1
            continue
        md = pos.get("signal_metadata") or {}
        event_ticker = md.get("event_ticker", "")
        by_event[event_ticker].append(pos)

        market = fetch_market(ticker)
        if market is None:
            errors += 1
            continue

        # Capture the actual claims value if Kalshi populated it
        ev = market.get("expiration_value")
        if ev is not None and event_ticker not in event_actual:
            try:
                event_actual[event_ticker] = int(float(ev))
            except (TypeError, ValueError):
                event_actual[event_ticker] = None

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

        log(f"  [close] {pos['position_id']}  {ticker:<42} {side} @${pos.get('price', 0):.4f}  "
            f"strike={int(md.get('strike', 0)):,}  kalshi_result={resolution.upper()}  "
            f"outcome={outcome}  realized=${realized:+.2f}  "
            f"{'DRY-RUN' if dry_run else 'closing...'}")

        if not dry_run:
            # Idempotency fence: if a prior reconciler pass already wrote a
            # close for this pid (Cowork scheduled task fires twice, operator
            # manual --once, daemon overlap), ShadowLedger.close() raises
            # ValueError. Catch it, log, skip the audit-note + rollup append
            # for this position (state is already correct), continue with the
            # rest of the pass. Without this, one already-closed position
            # aborts reconcile_once for ALL remaining positions and the
            # per-event rollup never writes. Fixed 2026-05-09 (audit C-T3c-1).
            # TODO post-May-14: introduce shadow_pnl_core.AlreadyClosedError
            # so reconcilers can match exception type instead of string.
            try:
                sl.close(
                    position_id=pos["position_id"],
                    settle_price=settle_price,
                    outcome=outcome,
                    fee_usd=0.0,
                )
            except ValueError as e:
                if "already closed" in str(e).lower():
                    log(f"  [skip-already-closed] {pos['position_id']}  "
                        f"{ticker} — prior pass already booked, continuing")
                    continue
                raise
            try:
                from shadow_pnl_core import LEDGER_PATH
                note = {
                    "type": "reconcile_note",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "position_id": pos["position_id"],
                    "ticker": ticker,
                    "engine": ENGINE,
                    "phase": "close",
                    "tag": "t3c_settlement_reconciler",
                    "kalshi_status": market.get("status"),
                    "kalshi_result": market.get("result"),
                    "kalshi_expiration_value": market.get("expiration_value"),
                    "kalshi_last_price_dollars": market.get("last_price_dollars"),
                }
                with open(LEDGER_PATH, "a") as f:
                    f.write(json.dumps(note) + "\n")
            except Exception as e:
                log(f"  [warn] audit note failed: {e}")

        closed += 1

        closes_this_pass[event_ticker].append({
            "position_id": pos["position_id"],
            "ticker": ticker,
            "side": side,
            "strike": md.get("strike"),
            "our_p": md.get("our_p"),
            "market_p": md.get("market_p"),
            "edge": md.get("edge"),
            "cost_usd": cost,
            "fee_usd": pos.get("fee_usd", 0),
            "realized_pnl_usd": realized,
            "kalshi_result": resolution,
        })

    # Per-event rollup — only if all legs of an event closed in this pass
    events_finalized = 0
    for event_ticker, closed_legs in closes_this_pass.items():
        all_legs = by_event.get(event_ticker, [])
        if len(closed_legs) != len(all_legs):
            continue
        total_realized = sum(c["realized_pnl_usd"] for c in closed_legs)
        total_cost = sum(c["cost_usd"] + c.get("fee_usd", 0) for c in closed_legs)
        actual = event_actual.get(event_ticker)
        log(f"  [event] {event_ticker}  legs={len(closed_legs)}  "
            f"actual={actual if actual is not None else '?'}  "
            f"cost=${total_cost:.2f}  realized=${total_realized:+.2f}  "
            f"ROI={(total_realized/total_cost)*100:+.1f}%")
        if not dry_run:
            write_event_realized(event_ticker, len(closed_legs), total_cost,
                                 total_realized, actual, closed_legs)
        events_finalized += 1

    return {"closed": closed, "pending": pending, "errors": errors,
            "events_finalized": events_finalized}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=1800,
                    help="daemon poll interval (default 30 min)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    log(f"T3c Settlement Reconciler starting. dry_run={args.dry_run} "
        f"once={args.once} interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            r = reconcile_once(args.dry_run)
            log(f"loop #{loops}: closed={r['closed']} pending={r['pending']} "
                f"errors={r['errors']} events_finalized={r['events_finalized']}")
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
