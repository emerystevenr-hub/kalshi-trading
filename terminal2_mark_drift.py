"""Terminal 2 — Mark-Drift Tracker.

Solves the long-settlement validation problem: T2 positions take 1–12+
months to settle, so realized-N calibration is too slow to be useful.
Mark-drift converts time-to-evidence from settlement-bound to scan-bound.

Methodology:
  For each open T2 position, fetch current Kalshi mark and compute:

    drift_score = (entry_price - current_mark) / (entry_price - target_price)

  Domain:
    drift_score > 0   → position moving TOWARD our target (thesis playing out)
    drift_score = 1   → target reached
    drift_score < 0   → position moving AWAY from target (thesis weakening)
    drift_score < -1  → position past stop_price territory

  Daily/weekly tracking gives ~120+ observations per quarter on a book
  of 8 positions, vs ~0–2 realized closes. Enough to flag thesis
  categories that systematically fail BEFORE settlement.

Output (append-only):
  ~/Documents/terminal2_data/mark_drift.jsonl
    one record per (position, snap_ts)

Usage:
  # One-shot — typical daily cron:
  python3 ~/Documents/terminal2_mark_drift.py

  # With per-category aggregate report:
  python3 ~/Documents/terminal2_mark_drift.py --report
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional

import requests


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path.home() / "Documents" / "terminal2_data"
DRIFT_PATH = DATA_DIR / "mark_drift.jsonl"
LOG_PATH = DATA_DIR / "mark_drift.log"
LEDGER = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"
ENGINE = "T2"
SCHEMA_VERSION = "v1"


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


# --------------------------------------------------------------------------
# Open-position discovery
# --------------------------------------------------------------------------

def load_thesis_target_lookup() -> Dict[str, dict]:
    """Build {ticker: {target_price, stop_price, sub_engine, our_probability}}
    from all historical thesis_candidates_*.json files. Used as fallback for
    positions whose signal_metadata didn't preserve T5's target/stop."""
    lookup: Dict[str, dict] = {}
    docs_dir = Path.home() / "Documents"
    for path in sorted(docs_dir.glob("thesis_candidates_*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for c in data.get("thesis_candidates", []) or []:
            for tk in c.get("target_ticker_prefixes", []) or []:
                lookup[tk] = {
                    "target_price": c.get("target_price"),
                    "stop_price": c.get("stop_price"),
                    "sub_engine": c.get("sub_engine"),
                    "our_probability": c.get("our_probability"),
                    "correlation_group": c.get("correlation_group"),
                }
    return lookup


def find_open_t2_positions() -> List[dict]:
    """Replay ledger and return open (engine=T2) positions."""
    if not LEDGER.exists():
        return []
    opens: Dict[str, dict] = {}
    annulled: Dict[str, set] = defaultdict(set)
    closed: set = set()
    with open(LEDGER) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("engine") != ENGINE:
                continue
            t = r.get("type")
            pid = r.get("position_id")
            if t == "open":
                opens[pid] = r
            elif t == "close":
                if r["ts"] not in annulled.get(pid, set()):
                    closed.add(pid)
            elif t == "annul_close":
                annulled[pid].add(r.get("annulled_close_ts"))
                closed.discard(pid)
    return [r for pid, r in opens.items() if pid not in closed]


# --------------------------------------------------------------------------
# Kalshi mark fetcher
# --------------------------------------------------------------------------

def fetch_market(ticker: str) -> Optional[dict]:
    try:
        r = requests.get(
            f"{KALSHI_BASE}/markets/{ticker}",
            timeout=20,
            proxies={"http": None, "https": None},
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log(f"  [error] fetch {ticker}: {e}")
        return None
    return r.json().get("market")


def current_value_for_side(market: dict, side: str) -> Optional[float]:
    """Mark-to-market value of OUR side. For a NO position, value = no_bid
    (what we could sell for now). For YES, value = yes_bid."""
    yb = market.get("yes_bid_dollars")
    nb = market.get("no_bid_dollars")
    try:
        yb = float(yb) if yb else None
        nb = float(nb) if nb else None
    except (TypeError, ValueError):
        return None
    if side == "YES":
        return yb
    if side == "NO":
        return nb
    return None


# --------------------------------------------------------------------------
# Drift computation
# --------------------------------------------------------------------------

def compute_drift_record(pos: dict, market: dict,
                         thesis_lookup: Dict[str, dict]) -> Optional[dict]:
    """Build one drift snapshot for a position."""
    ticker = pos.get("ticker")
    side = (pos.get("side") or "").upper()
    entry = pos.get("price")
    if entry is None:
        return None
    md = pos.get("signal_metadata") or {}
    # T5-generated thesis fields, if present in signal_metadata:
    target = md.get("target_price")
    stop = md.get("stop_price")
    sub_engine = md.get("sub_engine") or md.get("source") or "manual"
    correlation_group = md.get("correlation_group")
    # Fallback: look up from historical thesis_candidates JSON archive.
    fallback = thesis_lookup.get(ticker, {}) if ticker else {}
    if target is None:
        target = fallback.get("target_price")
    if stop is None:
        stop = fallback.get("stop_price")
    if not correlation_group:
        correlation_group = fallback.get("correlation_group")
    if sub_engine in (None, "?", "manual") and fallback.get("sub_engine"):
        sub_engine = fallback["sub_engine"]

    current = current_value_for_side(market, side)
    if current is None:
        return None

    # drift_score: (entry - current) / (entry - target). Conditioned on side.
    # For NO positions, target > entry (we want to sell higher), and
    #   "moving toward target" = current > entry, so (entry - current) < 0,
    #   denom (entry - target) < 0, score positive. ✓
    # For YES positions, target > entry as well, same algebra.
    score = None
    if target is not None and target != entry:
        try:
            score = round((entry - current) / (entry - float(target)), 3)
        except (TypeError, ZeroDivisionError):
            score = None

    # Unrealized P&L on size contracts at the current mark (per our side)
    size = pos.get("size", 0)
    unreal = round((current - entry) * size, 2) if size else None

    # Days held
    try:
        opened = datetime.fromisoformat(pos["ts"].replace("Z", "+00:00"))
        days_held = (datetime.now(timezone.utc) - opened).days
    except (KeyError, ValueError):
        days_held = None

    # Days to close (if Kalshi tells us)
    dtr = None
    close_time = market.get("close_time")
    if close_time:
        try:
            ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            dtr = (ct - datetime.now(timezone.utc)).days
        except ValueError:
            pass

    return {
        "_schema_version": SCHEMA_VERSION,
        "snap_ts_utc": datetime.now(timezone.utc).isoformat(),
        "position_id": pos.get("position_id"),
        "ticker": ticker,
        "side": side,
        "entry_price": entry,
        "target_price": target,
        "stop_price": stop,
        "current_mark": current,
        "drift_score": score,
        "unrealized_pnl_usd": unreal,
        "size_contracts": size,
        "days_held": days_held,
        "days_to_close": dtr,
        "sub_engine": sub_engine,
        "correlation_group": correlation_group,
    }


def append_records(records: List[dict]) -> int:
    if not records:
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DRIFT_PATH, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return len(records)


# --------------------------------------------------------------------------
# Aggregate report by sub_engine (calendar_fade vs tail_probability vs manual)
# --------------------------------------------------------------------------

def render_report(records: List[dict]) -> str:
    if not records:
        return "(no open T2 positions)\n"
    lines = []
    lines.append("=" * 88)
    lines.append("T2 MARK-DRIFT — OPEN POSITION SNAPSHOT")
    lines.append("=" * 88)
    lines.append(f"{'ticker':<40} {'side':<3} {'entry':>5} {'mark':>5} "
                 f"{'drift':>6} {'unreal':>7} {'days':>4}d/{'dtr':<4}d  cat")
    lines.append("-" * 88)
    for r in sorted(records, key=lambda x: -(x.get("drift_score") or -99)):
        ds = r.get("drift_score")
        ds_s = f"{ds:+.2f}" if ds is not None else "  —"
        unreal = r.get("unrealized_pnl_usd") or 0
        dh = r.get("days_held")
        dtr = r.get("days_to_close")
        cat = (r.get("sub_engine") or "?")[:11]
        lines.append(
            f"{r['ticker']:<40} {r['side']:<3} "
            f"${r['entry_price']:>4.2f} ${r['current_mark']:>4.2f} "
            f"{ds_s:>6} ${unreal:>+5.2f} "
            f"{dh if dh is not None else '?':>4}/{dtr if dtr is not None else '?':<4}  "
            f"{cat}"
        )
    lines.append("")
    # By-category aggregate
    by_cat: Dict[str, List[dict]] = defaultdict(list)
    for r in records:
        by_cat[r.get("sub_engine") or "manual"].append(r)
    lines.append("=" * 60)
    lines.append("BY THESIS CATEGORY (validation signal)")
    lines.append("=" * 60)
    lines.append(f"{'category':<18} {'n':>3} {'mean_drift':>10} {'median':>8} "
                 f"{'pct_progressing':>16}")
    for cat, rs in sorted(by_cat.items()):
        scores = [x["drift_score"] for x in rs if x.get("drift_score") is not None]
        if not scores:
            continue
        progressing = sum(1 for s in scores if s > 0)
        pct = progressing / len(scores) * 100
        lines.append(
            f"  {cat:<16} {len(scores):>3} "
            f"{mean(scores):>+9.2f}  {median(scores):>+7.2f}  "
            f"{pct:>13.0f}% (>{0:.0f})"
        )
    lines.append("")
    lines.append("Read: drift_score > 0 → moving toward target (thesis playing out).")
    lines.append("      drift_score = 1 → target hit.   drift_score < 0 → reversing.")
    lines.append("      A category with mean_drift > 0 and pct_progressing > 50% over ")
    lines.append("      a multi-week window is showing real edge BEFORE settlement.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                    help="print aggregate report to stdout after snap")
    ap.add_argument("--no-write", action="store_true",
                    help="dry run — don't append to JSONL")
    args = ap.parse_args()

    positions = find_open_t2_positions()
    log(f"open T2 positions: {len(positions)}")
    if not positions:
        return 0

    thesis_lookup = load_thesis_target_lookup()
    log(f"loaded {len(thesis_lookup)} historical thesis targets for lookup fallback")

    records: List[dict] = []
    for pos in positions:
        ticker = pos.get("ticker")
        market = fetch_market(ticker)
        if market is None:
            continue
        rec = compute_drift_record(pos, market, thesis_lookup)
        if rec:
            records.append(rec)

    if not args.no_write:
        n = append_records(records)
        log(f"appended {n} drift records")

    if args.report:
        print(render_report(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
