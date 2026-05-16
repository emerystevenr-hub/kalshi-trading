"""Entropy Collapse Detector — informed-flow signal across T3a/T3b/T3c.

Premise: when a contract's implied probability moves sharply with above-normal
volume in a short window, that's evidence of informed trading. Two uses:

  1. DEFENSIVE — refuse new entries that would fade the collapse direction.
     Don't sell into informed flow.
  2. OFFENSIVE (Phase 2, after n>=15 calibration alerts) — conditional entry
     with the collapse direction when our directional model agrees.

This script runs as a scheduler job every 5 minutes. It reads recent Kalshi
snapshot files per engine, computes entropy + z-score against a within-event
rolling baseline (last 30 windows of the same contract), and writes alerts
to ~/Documents/entropy_alerts.jsonl.

Liquidity gate (per Steve 2026-05-08):
  Alert fires only when:
    |z_score| > Z_THRESHOLD
    AND 5-min volume > rolling median × VOLUME_GATE_MULTIPLIER
  Single retail orders on thin macro books otherwise produce false positives.

Within-event baseline (vs cross-event):
  T3b has 12 events/yr — too sparse for cross-event historical baselines.
  Within-event (rolling last 30 windows of THIS contract) handles sparsity.

Output schema (per alert):
  {
    "ts": ISO,
    "engine": "T3b",
    "event_ticker": "KXCPIYOY-26APR",
    "ticker": "KXCPIYOY-26APR-T3.6",
    "entropy_now_bits": 0.881,
    "entropy_baseline_mean": 0.999,
    "entropy_baseline_std": 0.005,
    "z_score": -23.6,
    "direction": "rising_yes",
    "yes_p_5min_start": 0.50,
    "yes_p_5min_end": 0.70,
    "volume_5min": 240,
    "volume_baseline_median": 12,
    "liquidity_pass": true,
    "alert_level": "watch",
    "alert_id": "T3b-KXCPIYOY-26APR-T3.6-2026-05-09T04:45Z"
  }

Run:
    python3 ~/Documents/entropy_collapse_detector.py
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DOCS = Path.home() / "Documents"
ALERTS_PATH = DOCS / "entropy_alerts.jsonl"
LOG_PATH = DOCS / "entropy_collapse_detector.log"

# Engines watched + their snapshot directories
ENGINES = {
    "T3a": DOCS / "terminal3a_data",
    "T3b": DOCS / "terminal3b_data",
    "T3c": DOCS / "terminal3c_data",
}

# Per-engine snapshot file naming
SNAPSHOT_GLOBS = {
    "T3a": "kalshi_*.jsonl",
    "T3b": "kalshi_*.jsonl",
    "T3c": "kalshi_*.jsonl",
}

# Detection parameters
Z_THRESHOLD = 3.0                       # entropy z-score floor
VOLUME_GATE_MULTIPLIER = 1.5            # volume must exceed median × this
WINDOW_SEC = 300                        # 5 minutes
BASELINE_WINDOW_COUNT = 30              # rolling last N windows for baseline
MIN_BASELINE_SAMPLES = 6                # need at least this many for valid stats


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def binary_entropy(p: float) -> float:
    """Shannon entropy in bits for a binary outcome with prob p."""
    if p <= 0 or p >= 1:
        return 0.0
    return -p * math.log2(p) - (1 - p) * math.log2(1 - p)


def parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def get_ts(row: dict) -> Optional[str]:
    """Snapshot timestamp — schemas vary across engines."""
    return row.get("snap_ts_utc") or row.get("ts_utc")


def yes_p_from_row(row: dict) -> Optional[float]:
    """Extract midpoint implied probability from a kalshi snapshot row.
    Handles both T6 WS schema (yes_top_price_cents) and T3b/T3c REST schema
    (yes_bid/yes_ask in dollars or cents)."""
    # T6 WS schema
    yes_top = row.get("yes_top_price_cents")
    no_top = row.get("no_top_price_cents")
    if yes_top is not None and no_top is not None:
        yes_ask_cents = 100 - no_top
        return ((yes_top + yes_ask_cents) / 2.0) / 100.0
    # T3b/T3c REST schema — yes_bid/yes_ask
    yb = row.get("yes_bid")
    ya = row.get("yes_ask")
    if yb is not None and ya is not None:
        try:
            yb_f = float(yb); ya_f = float(ya)
        except (TypeError, ValueError):
            return None
        # Detect cents vs dollars: if value > 1.5, assume cents
        if yb_f > 1.5 or ya_f > 1.5:
            yb_f /= 100.0
            ya_f /= 100.0
        if 0 < yb_f < 1 and 0 < ya_f < 1 and ya_f >= yb_f:
            return (yb_f + ya_f) / 2.0
    # Fallback: last_price if present
    lp = row.get("last_price")
    if lp is not None:
        try:
            lp_f = float(lp)
            if lp_f > 1.5:
                lp_f /= 100.0
            if 0 < lp_f < 1:
                return lp_f
        except (TypeError, ValueError):
            pass
    return None


def volume_from_row(row: dict) -> Optional[float]:
    """Cumulative volume — schemas: 'volume' (T6) or 'volume_total' (T3b/T3c)."""
    for key in ("volume", "volume_total"):
        v = row.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def load_snapshots_for_engine(engine: str, hours_back: float = 4.0) -> Dict[str, List[dict]]:
    """Return {ticker: [snapshots sorted ascending by ts]} for the given engine."""
    data_dir = ENGINES[engine]
    if not data_dir.exists():
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    pattern = SNAPSHOT_GLOBS[engine]
    by_ticker: Dict[str, List[dict]] = defaultdict(list)
    for path in data_dir.glob(pattern):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_iso(get_ts(r))
                    if ts is None or ts < cutoff:
                        continue
                    ticker = r.get("ticker")
                    if not ticker:
                        continue
                    by_ticker[ticker].append(r)
        except OSError:
            continue
    for ticker in by_ticker:
        by_ticker[ticker].sort(key=lambda x: get_ts(x) or "")
    return by_ticker


def bucket_into_5min_windows(rows: List[dict]) -> List[Tuple[datetime, List[dict]]]:
    """Group rows into 5-minute windows keyed by window_start (UTC)."""
    if not rows:
        return []
    buckets: Dict[datetime, List[dict]] = defaultdict(list)
    for r in rows:
        ts = parse_iso(get_ts(r))
        if ts is None:
            continue
        # Floor to 5-min boundary
        floor_min = (ts.minute // 5) * 5
        window_start = ts.replace(minute=floor_min, second=0, microsecond=0)
        buckets[window_start].append(r)
    return sorted(buckets.items(), key=lambda x: x[0])


def window_summary(window_rows: List[dict]) -> Optional[dict]:
    """Compute entropy + volume for one 5-min window."""
    if not window_rows:
        return None
    # Use last snapshot of the window (most recent state at window-end)
    last = window_rows[-1]
    first = window_rows[0]
    yes_p_end = yes_p_from_row(last)
    yes_p_start = yes_p_from_row(first)
    if yes_p_end is None:
        return None
    entropy = binary_entropy(yes_p_end)
    vol_end = volume_from_row(last)
    vol_start = volume_from_row(first)
    if vol_end is not None and vol_start is not None and vol_end >= vol_start:
        vol_5min = vol_end - vol_start
    else:
        vol_5min = None
    return {
        "yes_p_start": yes_p_start,
        "yes_p_end": yes_p_end,
        "entropy_bits": entropy,
        "volume_5min": vol_5min,
    }


def evaluate_ticker(engine: str, ticker: str, rows: List[dict]) -> Optional[dict]:
    windows = bucket_into_5min_windows(rows)
    if len(windows) < MIN_BASELINE_SAMPLES + 1:
        return None
    summaries = [(ws, window_summary(wr)) for ws, wr in windows]
    summaries = [(ws, s) for ws, s in summaries if s is not None]
    if len(summaries) < MIN_BASELINE_SAMPLES + 1:
        return None
    latest_ws, latest = summaries[-1]
    baseline = summaries[-(BASELINE_WINDOW_COUNT + 1):-1]
    baseline = [s for _, s in baseline]
    if len(baseline) < MIN_BASELINE_SAMPLES:
        return None

    entropies = [s["entropy_bits"] for s in baseline]
    mean_e = statistics.fmean(entropies)
    try:
        std_e = statistics.stdev(entropies)
    except statistics.StatisticsError:
        std_e = 0.0
    if std_e <= 0:
        z = 0.0
    else:
        z = (latest["entropy_bits"] - mean_e) / std_e

    vols = [s["volume_5min"] for s in baseline if s["volume_5min"] is not None]
    vol_median = statistics.median(vols) if vols else None
    vol_now = latest["volume_5min"]
    if vol_median is not None and vol_now is not None:
        liquidity_pass = vol_now > vol_median * VOLUME_GATE_MULTIPLIER
    else:
        liquidity_pass = False

    direction = None
    yes_start = latest.get("yes_p_start")
    yes_end = latest.get("yes_p_end")
    if yes_start is not None and yes_end is not None:
        if yes_end > yes_start + 0.005:
            direction = "rising_yes"
        elif yes_end < yes_start - 0.005:
            direction = "rising_no"
        else:
            direction = "flat"

    alert_level = "noise"
    if abs(z) >= Z_THRESHOLD and liquidity_pass:
        alert_level = "watch"

    return {
        "ts": now_iso(),
        "window_start_utc": latest_ws.isoformat(),
        "engine": engine,
        "event_ticker": (rows[-1].get("event_ticker") if rows else None),
        "ticker": ticker,
        "entropy_now_bits": round(latest["entropy_bits"], 4),
        "entropy_baseline_mean": round(mean_e, 4),
        "entropy_baseline_std": round(std_e, 4),
        "z_score": round(z, 2),
        "direction": direction,
        "yes_p_5min_start": (round(yes_start, 4) if yes_start is not None else None),
        "yes_p_5min_end": (round(yes_end, 4) if yes_end is not None else None),
        "volume_5min": vol_now,
        "volume_baseline_median": vol_median,
        "liquidity_pass": liquidity_pass,
        "alert_level": alert_level,
        "alert_id": f"{engine}-{ticker}-{latest_ws.strftime('%Y-%m-%dT%H:%MZ')}",
    }


def write_alerts(alerts: List[dict]) -> None:
    if not alerts:
        return
    with open(ALERTS_PATH, "a") as f:
        for a in alerts:
            f.write(json.dumps(a) + "\n")


def run() -> int:
    log("entropy_collapse_detector tick")
    all_alerts: List[dict] = []
    watch_count = 0
    noise_count = 0
    for engine in ENGINES.keys():
        try:
            by_ticker = load_snapshots_for_engine(engine, hours_back=4.0)
        except Exception as e:
            log(f"  [error] loading {engine} snapshots: {e}")
            continue
        if not by_ticker:
            continue
        engine_watch = 0
        for ticker, rows in by_ticker.items():
            try:
                alert = evaluate_ticker(engine, ticker, rows)
            except Exception as e:
                log(f"  [error] eval {engine}/{ticker}: {e}")
                continue
            if alert is None:
                continue
            all_alerts.append(alert)
            if alert["alert_level"] == "watch":
                watch_count += 1
                engine_watch += 1
                log(f"  [WATCH] {engine}/{ticker}  z={alert['z_score']:+.1f}  "
                    f"vol={alert['volume_5min']}/{alert['volume_baseline_median']} "
                    f"dir={alert['direction']}  p={alert['yes_p_5min_start']}->"
                    f"{alert['yes_p_5min_end']}")
            else:
                noise_count += 1
        if engine_watch:
            log(f"  {engine}: {engine_watch} watch alerts")
    write_alerts(all_alerts)
    log(f"tick done — total tickers eval'd: {len(all_alerts)}  "
        f"watch={watch_count}  noise={noise_count}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    return run()


if __name__ == "__main__":
    sys.exit(main())
