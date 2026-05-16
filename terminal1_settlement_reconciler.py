"""Terminal 1 — Settlement Reconciler.

Closes open T1 shadow positions when NWS actuals arrive for their
target_date. For each open (engine=T1) position in the shadow ledger:
  1. Parse the Kalshi weather ticker → (station, target_date, strike)
  2. Look up nws_actuals_{station}.jsonl for that date
  3. Determine YES outcome based on actual vs strike
  4. Close position at settle_price = 1.00 (YES won) or 0.00 (YES lost)

Usage:
    # Once, dry-run (show what would close, change nothing):
    python3 ~/Documents/terminal1_settlement_reconciler.py --once --dry-run

    # Live single pass:
    python3 ~/Documents/terminal1_settlement_reconciler.py --once

    # Daemon mode (poll every hour):
    nohup caffeinate -is python3 ~/Documents/terminal1_settlement_reconciler.py \\
        --interval-sec 3600 > /dev/null 2>&1 &
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger, _read_ledger  # noqa: E402
from terminal1_phase2_paper_trader import (  # noqa: E402
    parse_weather_ticker,
    _event_to_target_date,
)


DATA_DIR = Path.home() / "Documents" / "terminal1_data"
LOG_PATH = Path.home() / "Documents" / "terminal1_settlement_reconciler.log"
ENGINE = "T1"

_STOP = False


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _handle_sigint(sig, frame):
    global _STOP
    _STOP = True
    log("SIGINT — exiting after current iteration.")


def load_actuals_by_station() -> Dict[str, Dict[str, dict]]:
    """Return {station: {date_local: actual_row}}."""
    out: Dict[str, Dict[str, dict]] = {}
    for path in DATA_DIR.glob("nws_actuals_*.jsonl"):
        station = path.stem.replace("nws_actuals_", "")
        out[station] = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d = r.get("date_local")
                if d:
                    out[station][d] = r
    return out


def find_open_t1_positions() -> List[dict]:
    """Replay the ledger and return currently-open T1 positions."""
    open_map: Dict[str, dict] = {}
    for r in _read_ledger():
        if r.get("type") == "open" and r.get("engine") == ENGINE:
            open_map[r["position_id"]] = r
        elif r.get("type") == "close":
            open_map.pop(r.get("position_id", ""), None)
    return list(open_map.values())


def parse_open_position(pos: dict) -> Optional[dict]:
    """Return {station, target_date, which_metric, strike_type, strike_lo, strike_hi}
    or None if the ticker can't be parsed.
    """
    ticker = pos.get("ticker", "")
    # Title isn't in ledger; reconstruct enough from ticker alone.
    # Fallback: use signal_metadata if present (from phase2 paper trader).
    meta = pos.get("signal_metadata", {}) or {}
    station = meta.get("station")
    target_date = meta.get("target_date")
    strike_disp = meta.get("strike_display") or ""

    # Re-parse — signal_metadata may be absent for manually-entered T1 trades
    # Extract station + target_date from ticker alone if needed
    if not station or not target_date:
        # event_ticker style: KX{HIGH|LOW}(T?)<STATION>-<YYMMMDD>
        # Strip "-<STRIKE>" suffix first
        if "-" in ticker:
            ev = "-".join(ticker.split("-")[:-1])
            target_date = target_date or _event_to_target_date(ev)
        # station parse: best-effort
        if not station:
            for code in ("NYC", "LAX", "CHI", "ORD", "DEN", "ATL", "MIA", "PHX", "DFW"):
                if code in ticker:
                    station = code.replace("CHI", "ORD")  # normalize
                    break

    if not station or not target_date:
        return None

    # Re-parse ticker to get strike. Need a title — we don't have one in the
    # ledger. Rebuild from strike_display if present, else from ticker suffix.
    if strike_disp:
        # Build a synthetic title that parser will recognize
        if "-" in strike_disp:
            title = f"range {strike_disp}"
        elif "<" in strike_disp:
            title = f"{strike_disp}"
        elif ">" in strike_disp:
            title = f"{strike_disp}"
        else:
            title = strike_disp
    else:
        # Parse strike from ticker suffix
        suffix = ticker.rsplit("-", 1)[-1] if "-" in ticker else ""
        if suffix.startswith("B") and "." in suffix:
            # B42.5 → "42-43°"
            try:
                center = float(suffix[1:])
                lo = int(center - 0.5)
                hi = int(center + 0.5)
                title = f"{lo}-{hi}°"
            except ValueError:
                return None
        elif suffix.startswith("T"):
            # T42 is ambiguous: could be threshold_above ("> 42°") OR
            # threshold_below ("< 42°"). Fail closed instead of guessing
            # — a wrong guess flips win→loss on settle. signal_metadata
            # carries strike_display for new entries (the if-branch above
            # at line 127), so this branch only fires for legacy/manual
            # entries that pre-date the metadata convention. Those need
            # manual reconciliation. Fixed 2026-05-09 (audit H-T1-4).
            return None
        else:
            return None

    parsed = parse_weather_ticker(ticker, title)
    if parsed is None:
        return None

    return {
        "station": station,
        "target_date": target_date,
        **parsed,
    }


def determine_yes_outcome(parsed_pos: dict, actual: dict) -> Optional[bool]:
    """Given parsed position + actual NWS row, return True if YES resolved.

    actual row has {high_f, low_f}.
    """
    which = parsed_pos["which_metric"]  # "high" or "low"
    val = actual.get(f"{which}_f")
    if val is None:
        return None
    lo = parsed_pos["strike_lo"]
    hi = parsed_pos["strike_hi"]

    # Kalshi weather resolution:
    # - Settles on the NWS ASOS station's official daily high/low as
    #   reported in the METAR/TAF daily summary.
    # - NWS reports integer °F (1-degree resolution) for the official max/min
    #   after end of day. Hourly obs are 0.1°F precision; the daily summary
    #   rounds via NWS convention (nearest integer, half-to-even).
    # - Iowa State Mesonet mirrors the NWS values — typically integer.
    #
    # Ticker grammar:
    # - T<N> "<N°" (threshold below): YES iff reported_val < N
    # - T<N> ">N°" (threshold above): YES iff reported_val > N
    # - B<N.5> "<X>-<X+1>°" (bucket): YES iff reported_val ∈ {X, X+1}
    #   Our parser writes strike_lo=X, strike_hi=X+2 (half-open [X, X+2)).
    #
    # If the Mesonet returns a float (e.g. 72.0 or 72.4), we defensively round
    # to the nearest integer BEFORE bucket containment. That matches how
    # Kalshi resolves against the NWS integer value.

    if lo == float("-inf"):
        # Threshold below: YES iff temp < strike (strict, per Kalshi title "<N°")
        return val < hi
    if hi == float("inf"):
        # Threshold above: YES iff temp > strike (strict, per Kalshi title ">N°")
        return val > lo

    # Bucket: normalize val to integer temp as NWS reports it
    val_rounded = int(round(val))
    # Bucket membership is {lo, lo+1, ..., hi-1}. Integer range check.
    return int(lo) <= val_rounded < int(hi)


def reconcile_once(dry_run: bool) -> dict:
    positions = find_open_t1_positions()
    log(f"open T1 positions: {len(positions)}")
    if not positions:
        return {"closed": 0, "unresolvable": 0, "pending": 0}

    actuals_by_station = load_actuals_by_station()

    # Iowa State Mesonet returns running daily snapshots — a row may exist
    # for today's date even though the day isn't done. To prevent premature
    # closures we wait until the target_date's LATEST US time zone (PDT) has
    # ended plus a 1-hour NWS-publish buffer:
    #
    #   Target date "2026-04-27" weather day in:
    #     PDT/MST: ends 2026-04-28T07:00 UTC
    #     MDT:     ends 2026-04-28T06:00 UTC
    #     CDT:     ends 2026-04-28T05:00 UTC
    #     EDT:     ends 2026-04-28T04:00 UTC
    #
    #   We use 2026-04-28T08:00 UTC as the universal close gate (PDT end +
    #   1h buffer for NWS daily summary to publish). For non-US stations
    #   this is conservative; for any US station it's safe.
    #
    # Bug history that motivates this gate (do NOT loosen without re-reading):
    #   2026-04-25: 4 NYC positions closed at 06:30 UTC, partial-day Mesonet
    #               data, required annul_close.
    #   2026-04-27: 3 positions (MIA/PHX/LAX HIGH) closed at 00:17 UTC against
    #               14:37 UTC partial-day actuals; afternoon highs climbed
    #               5–15°F; required terminal1_fix_apr27_partial.py to recover.
    #   The old gate (target_date < today_utc) was insufficient — UTC ticks
    #   over to next day at 00:00 UTC, which is hours BEFORE PDT end-of-day.
    LOCAL_END_BUFFER_HOURS = 8   # PDT end-of-day in UTC + 1h NWS buffer
    now_utc = datetime.now(timezone.utc)

    sl = ShadowLedger() if not dry_run else None
    closed = 0
    unresolvable = 0
    pending = 0

    for pos in positions:
        parsed = parse_open_position(pos)
        if parsed is None:
            log(f"  [unresolvable] {pos['position_id']} {pos.get('ticker')} — couldn't parse")
            unresolvable += 1
            continue

        station = parsed["station"]
        target_date = parsed["target_date"]

        # Block closing until the target_date has fully ended in PDT
        # (latest US zone) plus a 1-hour NWS-publish buffer. See the
        # comment at the top of reconcile_once for the full bug history
        # and rationale.
        from datetime import timedelta as _td  # local import — avoids reorder
        try:
            target_dt = datetime.fromisoformat(f"{target_date}T00:00:00+00:00")
            earliest_close_utc = target_dt + _td(days=1, hours=LOCAL_END_BUFFER_HOURS)
        except ValueError:
            pending += 1
            continue
        if now_utc < earliest_close_utc:
            pending += 1
            continue

        actual = actuals_by_station.get(station, {}).get(target_date)
        if actual is None:
            # Actual not yet published for this target_date
            pending += 1
            continue

        yes_won = determine_yes_outcome(parsed, actual)
        if yes_won is None:
            log(f"  [unresolvable] {pos['position_id']} {pos.get('ticker')} — no {parsed['which_metric']}_f in actual")
            unresolvable += 1
            continue

        # Settle price: YES=1, NO=0 based on whether YES won
        settle_price = 1.0 if yes_won else 0.0
        position_side = pos["side"]
        # Outcome from POSITION PERSPECTIVE:
        #   YES position wins if yes_won; NO position wins if not yes_won
        if position_side == "YES":
            outcome_label = "win" if yes_won else "loss"
        else:
            outcome_label = "win" if not yes_won else "loss"

        metric_key = f"{parsed['which_metric']}_f"
        metric_val = actual.get(metric_key)
        mode_label = "DRY-RUN" if dry_run else "closing..."
        log(f"  [close] {pos['position_id']} {pos.get('ticker'):<30} "
            f"{position_side} @ {pos['price']:.2f}  "
            f"actual {metric_key}={metric_val}  "
            f"yes_won={yes_won}  outcome={outcome_label}  "
            f"{mode_label}")

        if not dry_run:
            sl.close(
                position_id=pos["position_id"],
                settle_price=settle_price,
                outcome=outcome_label,
                fee_usd=0.0,   # no additional close fee in shadow mode
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

    log(f"T1 Settlement Reconciler starting. "
        f"dry_run={args.dry_run}  once={args.once}  interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            result = reconcile_once(args.dry_run)
            log(f"loop #{loops}: closed={result['closed']} "
                f"unresolvable={result['unresolvable']} "
                f"pending={result['pending']}")
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
