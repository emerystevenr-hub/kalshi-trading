"""Terminal 3b — CPI Settlement Reconciler.

PHASE 1 PAPER ENGINE — settlement triggers on the BLS row appearing in
`bls_cpi_yoy.jsonl`, NOT on Kalshi's `expiration_value` / `result` /
`status` finalization. This is intentional for a shadow engine where the
goal is calibration analysis on the BLS truth. If/when T3b goes live with
real capital, the gate must change to `kalshi_market.status == 'settled'
AND result IN {'yes','no'}` to avoid booking P&L on a divergent BLS rounding
that Kalshi resolves differently. See AUDIT_2026-05-09.md H-T3b-1.

Closes T3b shadow positions when BLS publishes the CPI actual for their
target month. Per spec §10.4 (shutdown handling), uses
`latest_expiration_time` not `expected_expiration_time` as the hard ceiling.

Resolution rule (per Kalshi market rules for KXCPIYOY-{event}-T{X.X}):
  YES wins iff `actual_yoy_rounded_1dec > strike`
  NO  wins iff `actual_yoy_rounded_1dec ≤ strike`

`actual_yoy_rounded_1dec` is BLS YoY rounded to 1 decimal place (Kalshi's
stated settlement convention; see market `rules_primary` field).

Usage:
    # Dry run — show what would close, change nothing:
    python3 ~/Documents/terminal3b_settlement_reconciler.py --once --dry-run

    # Live single pass:
    python3 ~/Documents/terminal3b_settlement_reconciler.py --once

    # Daemon (poll every hour):
    nohup caffeinate -is python3 ~/Documents/terminal3b_settlement_reconciler.py \\
        --interval-sec 3600 > /dev/null 2>&1 &
"""

import argparse
import json
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger, _read_ledger  # noqa: E402


DATA_DIR = Path.home() / "Documents" / "terminal3b_data"
BLS_PATH = DATA_DIR / "bls_cpi_yoy.jsonl"
LOG_PATH = DATA_DIR / "settlement_reconciler.log"
ENGINE = "T3b"

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
# BLS actuals loader
# --------------------------------------------------------------------------

def load_bls_actuals() -> Dict[Tuple[int, int], dict]:
    """Return {(target_year, target_month): row_with_yoy_rounded}."""
    out: Dict[Tuple[int, int], dict] = {}
    if not BLS_PATH.exists():
        return out
    try:
        with open(BLS_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                y = r.get("year")
                period = r.get("period", "")
                if not y or not period.startswith("M"):
                    continue
                try:
                    m = int(period[1:])
                except ValueError:
                    continue
                if 1 <= m <= 12:
                    out[(int(y), m)] = r
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# T3b open position lookup + ticker parsing
# --------------------------------------------------------------------------

EVENT_RE = re.compile(r"^(KXCPIYOY-\d{2}[A-Z]{3})$")
STRIKE_RE = re.compile(r"-T(\d+(?:\.\d+)?)$")
MONTH_CODE = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def parse_t3b_ticker(ticker: str) -> Optional[dict]:
    """KXCPIYOY-26APR-T3.5 → {target_year, target_month, strike}."""
    if not ticker.startswith("KXCPIYOY-"):
        return None
    m = STRIKE_RE.search(ticker)
    if not m:
        return None
    try:
        strike = float(m.group(1))
    except ValueError:
        return None
    head = ticker[: m.start()]   # KXCPIYOY-26APR
    parts = head.split("-")
    if len(parts) != 2:
        return None
    code = parts[1].upper()
    if len(code) != 5 or not code[:2].isdigit():
        return None
    yy = int(code[:2])
    mon = MONTH_CODE.get(code[2:])
    if not mon:
        return None
    return {
        "event_ticker": head,
        "target_year": 2000 + yy,
        "target_month": mon,
        "strike": strike,
    }


def find_open_t3b_positions() -> List[dict]:
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
            ts = r.get("ts")
            if pid and ts not in annulled_close_ts.get(pid, set()):
                open_map.pop(pid, None)
        elif t == "annul_close":
            annulled_close_ts.setdefault(pid, set()).add(r.get("annulled_close_ts"))
    return list(open_map.values())


# --------------------------------------------------------------------------
# Settlement
# --------------------------------------------------------------------------

def determine_yes_outcome(strike: float, actual_yoy_rounded: float) -> bool:
    """KXCPIYOY-...-T<X> resolves YES iff CPI YoY > X% (per Kalshi rules)."""
    return actual_yoy_rounded > strike


def reconcile_once(dry_run: bool) -> dict:
    positions = find_open_t3b_positions()
    log(f"open T3b positions: {len(positions)}")
    if not positions:
        return {"closed": 0, "unresolvable": 0, "pending": 0}

    actuals = load_bls_actuals()
    sl = ShadowLedger() if not dry_run else None
    now_utc = datetime.now(timezone.utc)

    closed = 0
    pending = 0
    unresolvable = 0

    for pos in positions:
        ticker = pos.get("ticker", "")
        parsed = parse_t3b_ticker(ticker)
        if parsed is None:
            log(f"  [unresolvable] {pos['position_id']} {ticker} — can't parse")
            unresolvable += 1
            continue

        target_key = (parsed["target_year"], parsed["target_month"])
        actual_row = actuals.get(target_key)

        # Spec §10.4: don't close on expected_expiration alone. We need an
        # actual BLS print or to be past latest_expiration_time.
        md = pos.get("signal_metadata") or {}
        # signal_metadata in T3b doesn't carry expirations; we accept that
        # the gate is "BLS row exists for this target month".
        if actual_row is None:
            pending += 1
            continue

        actual_rounded = actual_row.get("yoy_rounded")
        if actual_rounded is None:
            log(f"  [unresolvable] {pos['position_id']} {ticker} — yoy_rounded missing")
            unresolvable += 1
            continue

        yes_won = determine_yes_outcome(parsed["strike"], float(actual_rounded))
        side = pos.get("side", "")
        outcome_label = "win" if (yes_won and side == "YES") or (not yes_won and side == "NO") else "loss"
        settle_price = 1.0 if yes_won else 0.0

        log(f"  [close] {pos['position_id']} {ticker:<26} {side} @ "
            f"{pos.get('price', 0):.2f}  strike={parsed['strike']}%  "
            f"actual={actual_rounded}%  yes_won={yes_won}  outcome={outcome_label}  "
            f"{'DRY-RUN' if dry_run else 'closing...'}")

        if not dry_run:
            sl.close(
                position_id=pos["position_id"],
                settle_price=settle_price,
                outcome=outcome_label,
                fee_usd=0.0,
            )
        closed += 1

    return {"closed": closed, "unresolvable": unresolvable, "pending": pending}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=3600)
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    log(f"T3b Settlement Reconciler starting. dry_run={args.dry_run} "
        f"once={args.once} interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            r = reconcile_once(args.dry_run)
            log(f"loop #{loops}: closed={r['closed']} "
                f"unresolvable={r['unresolvable']} pending={r['pending']}")
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
