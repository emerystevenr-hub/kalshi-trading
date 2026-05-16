"""Terminal 2 — Catalyst Settlement Reconciler.

Closes open T2 shadow positions when Kalshi marks them as settled.
Unlike T1 (settles against NWS actuals) and T3b (settles against BLS
actuals), T2 has heterogeneous resolution criteria across markets —
political departures, FISA bills, FDA approvals, etc — each with their
own ground truth source.

Solution: trust Kalshi as the consolidated ground truth. When
`status == "settled"` and `result ∈ {yes, no}`, the market has been
officially resolved. Reconciler reads the result, settles the position
at $1.00 (YES) or $0.00 (NO), and computes outcome from the position's
side.

This unblocks T2 strategy validation: without auto-settlement, every
T2 position would lag indefinitely past its Kalshi settle, breaking
the realized-P&L feedback loop the engine depends on.

Usage:
    # One-shot, dry-run:
    python3 ~/Documents/terminal2_settlement_reconciler.py --once --dry-run

    # Live single pass:
    python3 ~/Documents/terminal2_settlement_reconciler.py --once

    # Daemon mode (poll every 30 min — T2 settles span days/months,
    # rapid polling not required):
    nohup caffeinate -is python3 ~/Documents/terminal2_settlement_reconciler.py \\
        --interval-sec 1800 > ~/Documents/terminal2_data/reconciler.out 2>&1 &
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger, _read_ledger  # noqa: E402


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path.home() / "Documents" / "terminal2_data"
LOG_PATH = DATA_DIR / "settlement_reconciler.log"
ENGINE = "T2"
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


# --------------------------------------------------------------------------
# Kalshi market fetch (with backoff on 429)
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Open T2 position discovery (annul-aware)
# --------------------------------------------------------------------------

def find_open_t2_positions() -> List[dict]:
    """Replay ledger, return list of open (engine=T2) position open events."""
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


# --------------------------------------------------------------------------
# Settlement decoding
# --------------------------------------------------------------------------

def kalshi_resolution(market: dict) -> Optional[str]:
    """Return 'yes' / 'no' / None. None means market is not yet resolved.

    2026-05-02 fix: Kalshi uses BOTH 'settled' and 'finalized' as terminal
    statuses depending on the market type. Daily polling-based markets
    (KXVOTEHUBTRUMPUPDOWN) and event-based markets (KXFISAEXTEND) settle
    to 'finalized' rather than 'settled'. The original gate of `status ==
    'settled'` skipped these — KXVOTEHUBTRUMPUPDOWN and KXFISAEXTEND-26APR
    sat in shadow_pnl as "open" for 27+ hours despite being resolved on
    Kalshi. Treat both statuses as resolved, and additionally trust the
    `result` field directly when populated (source of truth).
    """
    status = (market.get("status") or "").lower()
    result = (market.get("result") or "").lower()
    # Primary gate: result must be yes/no for us to settle
    if result not in ("yes", "no"):
        return None
    # Secondary check: status should be a terminal one. If neither
    # 'settled' nor 'finalized', that's unusual — log but don't act.
    if status not in ("settled", "finalized"):
        log(f"  [warn] {market.get('ticker')} has result={result!r} but "
            f"status={status!r} (expected 'settled' or 'finalized'); skipping")
        return None
    return result


def determine_outcome(side: str, kalshi_result: str) -> str:
    """Return 'win' or 'loss' from position perspective."""
    yes_won = (kalshi_result == "yes")
    if side == "YES":
        return "win" if yes_won else "loss"
    return "win" if not yes_won else "loss"


# --------------------------------------------------------------------------
# Main reconcile pass
# --------------------------------------------------------------------------

def reconcile_once(dry_run: bool) -> dict:
    positions = find_open_t2_positions()
    log(f"open T2 positions: {len(positions)}")
    if not positions:
        return {"closed": 0, "pending": 0, "errors": 0}

    sl = ShadowLedger() if not dry_run else None
    closed = 0
    pending = 0
    errors = 0

    for pos in positions:
        ticker = pos.get("ticker")
        if not ticker:
            log(f"  [skip] {pos.get('position_id')} — no ticker")
            errors += 1
            continue

        market = fetch_market(ticker)
        if market is None:
            log(f"  [error] {ticker} — could not fetch market")
            errors += 1
            continue

        resolution = kalshi_resolution(market)
        if resolution is None:
            pending += 1
            continue

        side = (pos.get("side") or "").upper()
        settle_price = 1.0 if resolution == "yes" else 0.0
        outcome = determine_outcome(side, resolution)

        # P&L preview (for log readability — ShadowLedger computes the same)
        size = pos.get("size", 0)
        entry = pos.get("price", 0)
        cost = pos.get("cost_usd", 0)
        if side == "YES":
            proceeds = settle_price * size
        else:
            proceeds = (1.0 - settle_price) * size
        realized = proceeds - cost - pos.get("fee_usd", 0)

        log(f"  [close] {pos['position_id']}  {ticker:<42} {side} @${entry:.2f}  "
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
            # Audit row alongside the close — includes Kalshi metadata
            try:
                from shadow_pnl_core import LEDGER_PATH
                note = {
                    "type": "reconcile_note",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "position_id": pos["position_id"],
                    "ticker": ticker,
                    "engine": ENGINE,
                    "phase": "close",
                    "tag": "t2_settlement_reconciler",
                    "kalshi_status": market.get("status"),
                    "kalshi_result": market.get("result"),
                    "kalshi_last_price_dollars": market.get("last_price_dollars"),
                    "kalshi_settled_close_time": market.get("close_time"),
                }
                with open(LEDGER_PATH, "a") as f:
                    f.write(json.dumps(note) + "\n")
            except Exception as e:
                log(f"  [warn] audit note failed: {e}")
        closed += 1

    return {"closed": closed, "pending": pending, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=1800,
                    help="daemon poll interval (default 30 min)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    log(f"T2 Settlement Reconciler starting. dry_run={args.dry_run} "
        f"once={args.once} interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            r = reconcile_once(args.dry_run)
            log(f"loop #{loops}: closed={r['closed']} "
                f"pending={r['pending']} errors={r['errors']}")
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
