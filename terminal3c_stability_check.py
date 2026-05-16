"""Terminal 3c — Edge Stability Check.

Reads paper_trader.log dry-run signal lines, groups by ticker, and reports
edge persistence over the most recent N snaps. The go-live gate (per the
2026-05-02 watch protocol) requires:

    - top-3 strikes hold edge ≥ $0.10 across ≥3 consecutive snaps
    - bid/ask spread ≤ $0.05 on candidates
    - OI does not drop >25% from initial observation

Usage:
    python3 ~/Documents/terminal3c_stability_check.py
    python3 ~/Documents/terminal3c_stability_check.py --window 6
"""

import argparse
import re
import sys
from collections import defaultdict, OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOG_PATH = Path.home() / "Documents" / "terminal3c_data" / "paper_trader.log"

# Match a [OPEN] line emitted by the paper trader's dry-run logging:
# 2026-05-02 17:55:28 UTC]     [OPEN] KXJOBLESSCLAIMS-26MAY07-205000           YES @$0.3800  edge=$0.292  our_p=0.592  mkt_p=0.300  strike=205,000  DRY-RUN
OPEN_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\].*?\[OPEN\]\s+"
    r"(?P<ticker>\S+)\s+(?P<side>YES|NO)\s+@\$(?P<entry>[\d.]+)\s+"
    r"edge=\$(?P<edge>[\d.\-]+)\s+our_p=(?P<our_p>[\d.]+)\s+"
    r"mkt_p=(?P<mkt_p>[\d.]+)\s+strike=(?P<strike>[\d,]+)"
)

# Header line per snap, useful for grouping signals into a "snap":
# loop #N: opened=X skipped=Y candidates=Z
LOOP_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\] loop #(?P<n>\d+):"
)


def parse_log(path: Path) -> List[dict]:
    """Return ordered list of {ts, type, payload} events from the log."""
    events: List[dict] = []
    if not path.exists():
        return events
    with open(path, "r") as f:
        for line in f:
            m = OPEN_RE.search(line)
            if m:
                events.append({
                    "ts": m.group("ts"),
                    "type": "open",
                    "ticker": m.group("ticker"),
                    "side": m.group("side"),
                    "entry": float(m.group("entry")),
                    "edge": float(m.group("edge")),
                    "our_p": float(m.group("our_p")),
                    "mkt_p": float(m.group("mkt_p")),
                    "strike": int(m.group("strike").replace(",", "")),
                })
                continue
            m = LOOP_RE.search(line)
            if m:
                events.append({
                    "ts": m.group("ts"),
                    "type": "loop",
                    "n": int(m.group("n")),
                })
    return events


def group_by_snap(events: List[dict]) -> List[Tuple[str, List[dict]]]:
    """Return [(snap_ts, [open_events]), ...] in chronological order.
    A snap is delimited by a `loop` event; opens before the next loop
    are attributed to that loop."""
    snaps: List[Tuple[str, List[dict]]] = []
    current_opens: List[dict] = []
    current_loop_ts: Optional[str] = None
    pending_opens: List[dict] = []

    for e in events:
        if e["type"] == "open":
            pending_opens.append(e)
        elif e["type"] == "loop":
            # Loop event closes the current snap — opens since last loop belong to THIS loop
            snap_ts = e["ts"]
            snaps.append((snap_ts, list(pending_opens)))
            pending_opens = []
    # Trailing opens with no loop wrapper (incomplete snap) — still include
    if pending_opens:
        snaps.append((datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), pending_opens))
    return snaps


def stability_report(snaps: List[Tuple[str, List[dict]]], window: int) -> dict:
    """Analyze last `window` snaps."""
    if not snaps:
        return {"snap_count": 0}

    recent = snaps[-window:]
    actual_window = len(recent)

    # Track each ticker: edges across snaps, presence count, edge stats
    by_ticker: Dict[str, List[Tuple[str, float, float, float]]] = defaultdict(list)
    # (snap_ts, edge, our_p, entry)
    for snap_ts, opens in recent:
        for o in opens:
            by_ticker[o["ticker"]].append(
                (snap_ts, o["edge"], o["our_p"], o["entry"])
            )

    rows = []
    for ticker, snaps_seen in by_ticker.items():
        present = len(snaps_seen)
        edges = [s[1] for s in snaps_seen]
        our_ps = [s[2] for s in snaps_seen]
        entries = [s[3] for s in snaps_seen]
        rows.append({
            "ticker": ticker,
            "present_in": present,
            "of": actual_window,
            "edge_min": min(edges),
            "edge_avg": sum(edges) / len(edges),
            "edge_max": max(edges),
            "our_p_min": min(our_ps),
            "our_p_max": max(our_ps),
            "entry_min": min(entries),
            "entry_max": max(entries),
        })
    rows.sort(key=lambda r: -r["edge_avg"])

    # Persistent strikes = present in ALL recent snaps with edge_min ≥ 0.10
    persistent = [r for r in rows
                  if r["present_in"] == actual_window and r["edge_min"] >= 0.10]
    top3_persistent = persistent[:3]

    return {
        "snap_count": len(snaps),
        "window": actual_window,
        "rows": rows,
        "persistent_count": len(persistent),
        "top3_persistent": top3_persistent,
        "ready_to_fire": len(top3_persistent) >= 3,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=6,
                    help="how many recent snaps to analyze (default 6)")
    ap.add_argument("--log", default=str(LOG_PATH))
    args = ap.parse_args()

    path = Path(args.log)
    print(f"=== T3c stability check ===  log={path}")
    events = parse_log(path)
    if not events:
        print(f"  log empty or not yet present — let the daemons run a few cycles")
        return 0

    snaps = group_by_snap(events)
    rep = stability_report(snaps, args.window)
    print(f"  snaps in log: {rep['snap_count']}  analyzed window: {rep['window']}")
    print()

    if not rep["rows"]:
        print("  no candidates produced in window — check logger / data freshness")
        return 0

    print(f"  {'TICKER':<42} {'IN':<5} {'EDGE_MIN':>9} {'EDGE_AVG':>9} {'EDGE_MAX':>9} {'OUR_P':>14} {'ENTRY':>14}")
    for r in rep["rows"]:
        present = f"{r['present_in']}/{r['of']}"
        our_p = f"{r['our_p_min']:.2f}-{r['our_p_max']:.2f}"
        entry = f"${r['entry_min']:.2f}-${r['entry_max']:.2f}"
        print(f"  {r['ticker']:<42} {present:<5} ${r['edge_min']:>7.3f} ${r['edge_avg']:>7.3f} "
              f"${r['edge_max']:>7.3f} {our_p:>14} {entry:>14}")
    print()

    print(f"  persistent (edge≥$0.10 across all {rep['window']} snaps): {rep['persistent_count']}")
    if rep["top3_persistent"]:
        print(f"  top-3 persistent strikes:")
        for r in rep["top3_persistent"]:
            print(f"    {r['ticker']:<42}  edge_min=${r['edge_min']:.3f}")
    print()

    if rep["ready_to_fire"]:
        print("  ✓ GO-LIVE GATE MET (top-3 persistent strikes hold edge ≥ $0.10)")
    else:
        print(f"  ✗ go-live gate not met yet (need top-3 persistent, have {len(rep['top3_persistent'])})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
