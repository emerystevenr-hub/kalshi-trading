"""Terminal 3a — FOMC Observation Mode (data-only, no trading).

Tight-cadence snapshotting of specific KXFEDDECISION events during a FOMC
meeting window. Designed to answer ONE question:

    "If a real Dutch arb appeared, would we have had time to execute it?"

Observation strategy:
  - Snap target event(s) every INTERVAL_SEC (default 15s — 4× normal cadence)
  - Record Σask, Σbid every snap
  - Compute Δsum_ask over 1 min and 5 min trailing windows
  - Detect "dislocation events" when |Σask - rolling_5min_mean| ≥ THRESHOLD
  - Track dislocation lifetime (consecutive snaps with Σ deviated)
  - Write per-snap rows + per-dislocation summaries to JSONL

DOES NOT TRADE. Pure observation. Run in parallel with the existing
terminal3a_fed_scanner.

Outputs:
  ~/Documents/terminal3a_data/fomc_obs_{event_ticker}_{YYYY-MM-DD}.jsonl
  ~/Documents/terminal3a_data/fomc_dislocations.jsonl  (cross-day events log)

Usage:
  # Run for the April 29-30 FOMC, 15-sec cadence:
  python3 ~/Documents/terminal3a_fomc_observer.py \\
      --events KXFEDDECISION-26APR \\
      --interval-sec 15

  # Daemonize:
  nohup caffeinate -is python3 ~/Documents/terminal3a_fomc_observer.py \\
      --events KXFEDDECISION-26APR --interval-sec 15 \\
      > /dev/null 2>&1 &

After FOMC, analyze with:
  cat ~/Documents/terminal3a_data/fomc_dislocations.jsonl | jq .

Or write a one-shot analyzer over the per-snap log to compute:
  - mean dislocation lifetime (sec)
  - max dislocation magnitude (¢ from baseline)
  - any actual arb crossings (Σask < 100)
  - distribution of Δsum_ask per minute
"""

import argparse
import json
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Deque, Dict, List, Optional, Tuple

import requests


BASE = "https://api.elections.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT = 10
PAGE_LIMIT = 50

DATA_DIR = Path.home() / "Documents" / "terminal3a_data"
DISLOC_PATH = DATA_DIR / "fomc_dislocations.jsonl"
LOG_PATH = DATA_DIR / "fomc_observer.log"

# Detection thresholds
DEFAULT_DISLOCATION_THRESHOLD_CENTS = 3   # |Σask - rolling_mean| ≥ this
DEFAULT_INTERVAL_SEC = 15                  # 4× normal scanner cadence

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
    log("SIGINT — exiting after current snap.")


def fetch_markets(event_ticker: str) -> List[dict]:
    try:
        r = requests.get(
            f"{BASE}/markets",
            params={"event_ticker": event_ticker, "limit": PAGE_LIMIT},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return r.json().get("markets", []) or []


def fetch_orderbook(ticker: str) -> Optional[dict]:
    try:
        r = requests.get(
            f"{BASE}/markets/{ticker}/orderbook",
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    body = r.json()
    return body.get("orderbook_fp") or body.get("orderbook")


def _levels_to_cents(levels) -> List[Tuple[int, int]]:
    out = []
    for lvl in levels or []:
        try:
            p = float(lvl[0])
            s = int(float(lvl[1]))
            pc = int(round(p * 100)) if p < 2 else int(round(p))
            if 0 < pc < 100:
                out.append((pc, s))
        except (ValueError, TypeError, IndexError):
            continue
    return out


def snap_event(event_ticker: str) -> Optional[dict]:
    """One snapshot of an event. Returns sum_ask, sum_bid, n_legs, and
    per-leg detail. None if the event has no markets or all are missing."""
    markets = fetch_markets(event_ticker)
    if not markets:
        return None
    legs = []
    for m in markets:
        ob = fetch_orderbook(m["ticker"])
        yes_levels = _levels_to_cents(
            ob.get("yes_dollars", []) if ob else []
        ) if ob else []
        no_levels = _levels_to_cents(
            ob.get("no_dollars", []) if ob else []
        ) if ob else []
        yes_top = max(yes_levels, key=lambda x: x[0]) if yes_levels else (None, 0)
        no_top = max(no_levels, key=lambda x: x[0]) if no_levels else (None, 0)
        yes_ask = (100 - no_top[0]) if no_top[0] is not None else None
        legs.append({
            "ticker": m["ticker"],
            "yes_bid": yes_top[0],
            "yes_bid_size": yes_top[1],
            "no_bid": no_top[0],
            "no_bid_size": no_top[1],
            "yes_ask": yes_ask,
            "yes_ask_size": no_top[1],
        })

    # Match main scanner's fallback semantics: missing yes_ask → 100 (no one
    # will sell at any reasonable price); missing yes_bid → 0 (no one will buy).
    if not legs:
        sum_ask = None
        sum_bid = None
    else:
        sum_ask = sum(
            leg["yes_ask"] if leg["yes_ask"] is not None else 100
            for leg in legs
        )
        sum_bid = sum(
            leg["yes_bid"] if leg["yes_bid"] is not None else 0
            for leg in legs
        )
    # Track how many legs had real quotes (vs. fallback) for diagnostic clarity
    n_legs_with_ask = sum(1 for leg in legs if leg["yes_ask"] is not None)
    n_legs_with_bid = sum(1 for leg in legs if leg["yes_bid"] is not None)

    return {
        "n_legs": len(legs),
        "n_legs_with_ask": n_legs_with_ask,
        "n_legs_with_bid": n_legs_with_bid,
        "sum_ask": sum_ask,
        "sum_bid": sum_bid,
        "min_yes_ask_size": (
            min(leg["yes_ask_size"] for leg in legs) if legs else 0
        ),
        "legs": legs,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True,
                    help="Comma-separated event tickers (e.g. KXFEDDECISION-26APR)")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC,
                    help=f"snap interval (default {DEFAULT_INTERVAL_SEC})")
    ap.add_argument("--dislocation-threshold", type=int,
                    default=DEFAULT_DISLOCATION_THRESHOLD_CENTS,
                    help="|Σask - 5min_mean| ≥ this triggers dislocation event")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    target_events = [e.strip() for e in args.events.split(",") if e.strip()]
    log(f"FOMC Observer starting. events={target_events}  "
        f"interval={args.interval_sec}s  "
        f"dislocation_threshold={args.dislocation_threshold}¢")

    # Per-event rolling history for delta + dislocation detection
    # window size = 5 min in snaps
    window_size = max(2, 300 // args.interval_sec)
    history: Dict[str, Deque[dict]] = {ev: deque(maxlen=window_size) for ev in target_events}
    in_dislocation: Dict[str, Optional[dict]] = {ev: None for ev in target_events}

    snap_count = 0
    while True:
        if _STOP:
            break
        t0 = time.time()
        ts_utc = datetime.now(timezone.utc)

        for ev in target_events:
            try:
                snap = snap_event(ev)
            except Exception as e:
                log(f"  [error] snap {ev}: {type(e).__name__}: {e}")
                continue
            if snap is None:
                continue

            # Compute deltas vs trailing windows
            hist = history[ev]
            delta_1min = None
            delta_5min = None
            rolling_mean_5min = None
            if hist and snap["sum_ask"] is not None:
                # 1 min back: snaps[-(60/interval):]
                lookback_1min = max(1, 60 // args.interval_sec)
                lookback_5min = max(1, 300 // args.interval_sec)
                recent_1min = [
                    h["sum_ask"] for h in list(hist)[-lookback_1min:]
                    if h.get("sum_ask") is not None
                ]
                recent_5min = [
                    h["sum_ask"] for h in list(hist)
                    if h.get("sum_ask") is not None
                ]
                if recent_1min:
                    delta_1min = snap["sum_ask"] - recent_1min[0]
                if recent_5min:
                    delta_5min = snap["sum_ask"] - recent_5min[0]
                    rolling_mean_5min = round(mean(recent_5min), 2)

            row = {
                "ts_utc": ts_utc.isoformat(),
                "event_ticker": ev,
                "n_legs": snap["n_legs"],
                "n_legs_with_ask": snap["n_legs_with_ask"],
                "n_legs_with_bid": snap["n_legs_with_bid"],
                "sum_ask": snap["sum_ask"],
                "sum_bid": snap["sum_bid"],
                "min_yes_ask_size": snap["min_yes_ask_size"],
                "delta_sum_ask_1min": delta_1min,
                "delta_sum_ask_5min": delta_5min,
                "rolling_mean_5min": rolling_mean_5min,
                "interval_sec": args.interval_sec,
            }

            # Per-event per-day file
            date_str = ts_utc.strftime("%Y-%m-%d")
            ev_path = DATA_DIR / f"fomc_obs_{ev}_{date_str}.jsonl"
            with open(ev_path, "a") as f:
                f.write(json.dumps(row) + "\n")

            # Dislocation detection
            if (rolling_mean_5min is not None and snap["sum_ask"] is not None
                    and abs(snap["sum_ask"] - rolling_mean_5min) >= args.dislocation_threshold):
                # In a dislocation. Start tracking if not already.
                if in_dislocation[ev] is None:
                    in_dislocation[ev] = {
                        "event_ticker": ev,
                        "start_ts": ts_utc.isoformat(),
                        "start_sum_ask": snap["sum_ask"],
                        "baseline_5min": rolling_mean_5min,
                        "min_sum_ask": snap["sum_ask"],
                        "max_sum_ask": snap["sum_ask"],
                        "snaps_in_dislocation": 1,
                        "any_arb_crossed": snap["sum_ask"] < 100,
                    }
                    log(f"  [dislocation START] {ev}  Σask={snap['sum_ask']}  "
                        f"baseline={rolling_mean_5min:.1f}  "
                        f"deviation={snap['sum_ask']-rolling_mean_5min:+.1f}¢")
                else:
                    d = in_dislocation[ev]
                    d["min_sum_ask"] = min(d["min_sum_ask"], snap["sum_ask"])
                    d["max_sum_ask"] = max(d["max_sum_ask"], snap["sum_ask"])
                    d["snaps_in_dislocation"] += 1
                    if snap["sum_ask"] < 100:
                        d["any_arb_crossed"] = True
            else:
                # Reverted to normal. Close the dislocation event.
                if in_dislocation[ev] is not None:
                    d = in_dislocation[ev]
                    d["end_ts"] = ts_utc.isoformat()
                    d["end_sum_ask"] = snap["sum_ask"]
                    d["lifetime_sec"] = d["snaps_in_dislocation"] * args.interval_sec
                    log(f"  [dislocation END] {ev}  duration~{d['lifetime_sec']}s  "
                        f"min_Σask={d['min_sum_ask']}  arb_crossed={d['any_arb_crossed']}")
                    with open(DISLOC_PATH, "a") as f:
                        f.write(json.dumps(d) + "\n")
                    in_dislocation[ev] = None

            hist.append(row)

        snap_count += 1
        dt = time.time() - t0
        if snap_count % 20 == 0:
            log(f"  snap #{snap_count}  ({dt:.1f}s)")

        if args.once or _STOP:
            break
        end = time.time() + args.interval_sec
        while time.time() < end and not _STOP:
            time.sleep(min(0.5, end - time.time()))

    log(f"Stopped. Total snaps: {snap_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
