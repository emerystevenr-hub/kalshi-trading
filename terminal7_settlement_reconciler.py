"""Terminal 7 — NBA/NHL Settlement Reconciler.

Closes open T7 positions whose Kalshi markets have finalized. Mirrors
terminal6_mlb_settlement_reconciler.py with one defense-in-depth delta:

  Δ — H13 idempotency pattern: ShadowLedger.close() raises ValueError on
       already-closed positions; wrap in try/except so a stuck/duplicate
       position can't take down the rest of the reconcile pass. Logs
       [skip-already-closed] on hit, re-raises non-"already closed"
       ValueErrors (fail-loud on unknown errors, fail-quiet only on the
       expected idempotency case).

T6's reconciler doesn't have this pattern (it runs as a long-running
daemon, not as --once via Cowork scheduler, so double-fire risk is low).
T7 inherits the safer pattern from T3c — no cost, defense-in-depth.

Kalshi market lifecycle (mirrors T6):
  status = "active"     → game in progress / pre-game; no settlement
  status = "settled"    → terminal; result is "yes" or "no"
  status = "finalized"  → terminal; same as settled
  status = "closed"     → market closed for trading but not yet settled

Usage:
    python3 ~/Documents/terminal7_settlement_reconciler.py --once
    nohup caffeinate -is python3 ~/Documents/terminal7_settlement_reconciler.py \\
        --interval-sec 3600 \\
        > ~/Documents/terminal7_data/settlement_reconciler.out 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path.home() / "Documents"))

from shadow_pnl_core import ShadowLedger, _read_ledger, LEDGER_PATH  # noqa: E402

import requests

ENGINE = "T7"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path.home() / "Documents" / "terminal7_data"
LOG_PATH = DATA_DIR / "settlement_reconciler.log"

DEFAULT_INTERVAL_SEC = 3600  # hourly

_STOP_REQUESTED = False


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


def _handle_sigint(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    log("[signal] stop requested")


def fetch_market(ticker: str) -> Optional[dict]:
    try:
        r = requests.get(f"{KALSHI_BASE}/markets/{ticker}", timeout=30)
    except requests.RequestException as e:
        log(f"  [error] /markets/{ticker} fetch failed: {e}")
        return None
    if r.status_code != 200:
        log(f"  [error] /markets/{ticker} returned {r.status_code}")
        return None
    return r.json().get("market")


def kalshi_resolution(market: dict) -> Optional[str]:
    if not market:
        return None
    status = (market.get("status") or "").lower()
    if status not in ("settled", "finalized"):
        return None
    result = (market.get("result") or "").lower()
    if result in ("yes", "no"):
        return result
    return None


def determine_outcome(side: str, resolution: str) -> str:
    side = side.upper()
    if (side == "YES" and resolution == "yes") or (side == "NO" and resolution == "no"):
        return "win"
    return "loss"


def find_open_t7_positions() -> List[dict]:
    opens_by_pid: Dict[str, dict] = {}
    closed_pids = set()
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        if r.get("type") == "open":
            opens_by_pid[r["position_id"]] = r
        elif r.get("type") == "close":
            closed_pids.add(r["position_id"])
    return [
        o for pid, o in opens_by_pid.items() if pid not in closed_pids
    ]


def reconcile_once(dry_run: bool) -> dict:
    positions = find_open_t7_positions()
    log(f"open T7 positions: {len(positions)}")
    if not positions:
        return {"closed": 0, "pending": 0, "errors": 0, "skipped_already_closed": 0}

    sl = ShadowLedger() if not dry_run else None
    closed = 0
    pending = 0
    errors = 0
    skipped_already_closed = 0
    total_realized = 0.0

    for pos in positions:
        ticker = pos.get("ticker")
        if not ticker:
            errors += 1
            continue

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
        total_realized += realized

        md = pos.get("signal_metadata") or {}
        log(f"  [close] {pos['position_id']}  {ticker:<42} {side} "
            f"@${pos.get('price', 0):.4f}  "
            f"sport={md.get('sport', '?')} "
            f"team={md.get('team_name', '?')}  "
            f"G{md.get('game_num', '?')} "
            f"delta={md.get('delta', 0):+.3f}  "
            f"kalshi_result={resolution.upper()}  outcome={outcome}  "
            f"realized=${realized:+.2f}  "
            f"{'DRY-RUN' if dry_run else 'closing...'}")

        if not dry_run:
            # H13 idempotency: ShadowLedger.close() raises ValueError on
            # double-close. The expected case (already-closed position)
            # logs and continues; unknown ValueErrors re-raise so we don't
            # silently swallow real bugs. T7 inherits this pattern from T3c
            # even though the daemon isn't Cowork-scheduled — defense in
            # depth costs nothing and forecloses a foot-gun if the
            # reconciler ever gets re-wrapped under launchd or similar.
            try:
                sl.close(
                    position_id=pos["position_id"],
                    settle_price=settle_price,
                    outcome=outcome,
                    fee_usd=0.0,
                )
            except ValueError as e:
                if "already closed" in str(e).lower():
                    log(f"  [skip-already-closed] {pos['position_id']}: {e}")
                    skipped_already_closed += 1
                    continue
                # Unknown ValueError — fail loud
                raise
            try:
                note = {
                    "type": "reconcile_note",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "position_id": pos["position_id"],
                    "ticker": ticker,
                    "engine": ENGINE,
                    "phase": "close",
                    "tag": "t7_settlement_reconciler",
                    "kalshi_status": market.get("status"),
                    "kalshi_result": market.get("result"),
                    "kalshi_last_price_dollars": market.get("last_price_dollars"),
                    "sport": md.get("sport"),
                    "game_num": md.get("game_num"),
                    "series_ticker": md.get("series_ticker"),  # Kalshi parent name (audit)
                    "series_id": md.get("series_id"),          # synthetic per-matchup ID (Filter 15)
                }
                with open(LEDGER_PATH, "a") as f:
                    f.write(json.dumps(note) + "\n")
            except Exception as e:
                log(f"  [warn] audit note failed: {e}")

        closed += 1

    log(f"loop summary: closed={closed} pending={pending} errors={errors} "
        f"skipped_already_closed={skipped_already_closed} "
        f"total_realized=${total_realized:+.2f}")
    return {"closed": closed, "pending": pending, "errors": errors,
            "skipped_already_closed": skipped_already_closed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Single pass, then exit.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write closes to ledger.")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC,
                    help="Loop cadence in seconds (default 3600 = hourly).")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log(f"T7 Settlement Reconciler starting; once={args.once} "
        f"dry_run={args.dry_run} interval={args.interval_sec}s")

    if args.once:
        reconcile_once(args.dry_run)
        return 0

    while not _STOP_REQUESTED:
        try:
            reconcile_once(args.dry_run)
        except Exception as e:
            log(f"[error] loop raised: {e}")
        slept = 0
        while slept < args.interval_sec and not _STOP_REQUESTED:
            time.sleep(1)
            slept += 1

    log("T7 Settlement Reconciler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
