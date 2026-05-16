"""Portfolio Freshness Watchdog.

One job: catch silent data starvation across all engines before it costs another
12 days of stuck positions. Born 2026-05-08 after T3b's silent 12-day nowcast/BLS
starvation and T1's silent 7-day NWS actuals starvation — both went undetected
because "daemon is running" was being inferred from handoff docs, not from data.

Rule: log freshness is the only reliable signal of liveness.

How it works:
- For each watched data file, check mtime vs an `expected_max_age_h` threshold.
- If stale: print a loud row, append to `~/Documents/freshness_alarm.log`,
  and write `~/Documents/freshness_alarm.flag` so traders can refuse to open
  new positions while the data path is broken.
- Exit 0 if all fresh, 2 if any stale. Suitable for cron (`*/15 * * * *`).

Trader integration (one-liner at the top of each open() codepath):

    from pathlib import Path
    if (Path.home() / "Documents" / "freshness_alarm.flag").exists():
        print("[BLOCK] freshness_alarm.flag present — refusing new positions")
        return  # or sys.exit(2)

The flag clears itself the moment the watchdog runs and finds everything fresh.

Usage:
    python3 portfolio_freshness_watchdog.py            # run once, print + flag
    python3 portfolio_freshness_watchdog.py --quiet    # only print on stale
    python3 portfolio_freshness_watchdog.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

DOCS = Path.home() / "Documents"
FLAG = DOCS / "freshness_alarm.flag"
LOG = DOCS / "freshness_alarm.log"


@dataclass
class Check:
    engine: str
    label: str
    path: Path
    expected_max_age_h: float
    # If the file may legitimately not exist yet (e.g., first-run state file),
    # a missing file is NOT stale. Default: missing == stale.
    missing_ok: bool = False


# ---------------------------------------------------------------------------
# Watch list. Cycle thresholds match the operating handoff. Tighten if a
# specific puller starts silently failing more often than expected.
# Add new rows when a new daemon ships. Remove rows when a daemon retires.
# ---------------------------------------------------------------------------
CHECKS: List[Check] = [
    # T1 — weather ensemble
    Check("T1", "NWS actuals (any station)", DOCS / "terminal1_data" / "nws_actuals_NYC.jsonl", 12.0),
    Check("T1", "paper trader log",          DOCS / "terminal1_phase2_paper_trader.log",        0.5),
    Check("T1", "model pullers log",         DOCS / "terminal1_pullers.log",                    1.0),
    Check("T1", "kalshi logger",             DOCS / "terminal1_logger.log",                     0.5),
    Check("T1", "reconciler",                DOCS / "terminal1_settlement_reconciler.log",      2.0),

    # T2 — catalyst book
    Check("T2", "reconciler",                DOCS / "terminal2_data" / "settlement_reconciler.log", 1.0),
    Check("T2", "mark drift (daily)",        DOCS / "terminal2_data" / "mark_drift.log",         30.0),

    # T3b — CPI nowcast
    Check("T3b", "kalshi logger",            DOCS / "terminal3b_data" / "kalshi_logger.log",     0.5),
    Check("T3b", "paper trader",             DOCS / "terminal3b_data" / "paper_trader.log",      1.0),
    Check("T3b", "nowcast puller",           DOCS / "terminal3b_data" / "nowcast_puller.log",    8.0),
    Check("T3b", "BLS actuals",              DOCS / "terminal3b_data" / "bls_actuals.log",       30.0),
    Check("T3b", "reconciler (hourly)",      DOCS / "terminal3b_data" / "settlement_reconciler.log", 2.0),

    # T3c — initial jobless claims
    Check("T3c", "kalshi logger",            DOCS / "terminal3c_data" / "kalshi_logger.log",     0.5),
    Check("T3c", "paper trader",             DOCS / "terminal3c_data" / "paper_trader.log",      1.0),
    Check("T3c", "FRED claims data",         DOCS / "terminal3c_data" / "claims_data.log",       14.0),

    # T6 — MLB game markets (cut over to WebSocket 2026-05-08; REST logger
    # retired). The WS logger writes snapshots every 5 sec; threshold of 6 min
    # gives a generous buffer for transient reconnects.
    Check("T6", "kalshi logger (WS)",        DOCS / "terminal6_data" / "ws_logger.log",          0.1),
    Check("T6", "Vegas lines puller",        DOCS / "terminal6_data" / "lines_puller.log",       1.5,  missing_ok=True),
    Check("T6", "paper trader",              DOCS / "terminal6_data" / "paper_trader.log",       1.0,  missing_ok=True),
    Check("T6", "settlement reconciler",     DOCS / "terminal6_data" / "settlement_reconciler.log", 2.0, missing_ok=True),
]


def _age_hours(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    return (time.time() - mtime) / 3600.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def run(quiet: bool, as_json: bool) -> int:
    rows = []
    any_stale = False

    for c in CHECKS:
        age = _age_hours(c.path)
        if age is None:
            stale = not c.missing_ok
            status = "MISSING" if stale else "missing-ok"
            age_str = "—"
        else:
            stale = age > c.expected_max_age_h
            status = "STALE" if stale else "fresh"
            age_str = f"{age:.2f}h"
        if stale:
            any_stale = True
        rows.append({
            "engine": c.engine,
            "label": c.label,
            "path": str(c.path),
            "age_h": age,
            "max_age_h": c.expected_max_age_h,
            "status": status,
            "stale": stale,
        })

    if as_json:
        print(json.dumps({"now": _now(), "any_stale": any_stale, "rows": rows}, indent=2))
    else:
        if any_stale or not quiet:
            print(f"[{_now()}] portfolio_freshness_watchdog")
            print(f"  {'engine':<5} {'label':<26} {'age':>10} {'max':>8}  status")
            for r in rows:
                age_disp = "—" if r["age_h"] is None else f"{r['age_h']:.2f}h"
                print(f"  {r['engine']:<5} {r['label']:<26} {age_disp:>10} {r['max_age_h']:>6.1f}h  {r['status']}")
            if any_stale:
                print(f"  STALE PRESENT — wrote flag {FLAG}")

    if any_stale:
        try:
            FLAG.parent.mkdir(parents=True, exist_ok=True)
            stale_summary = "; ".join(
                f"{r['engine']}/{r['label']}={r['age_h']:.1f}h"
                for r in rows if r["stale"] and r["age_h"] is not None
            )
            missing = ", ".join(f"{r['engine']}/{r['label']}" for r in rows if r["status"] == "MISSING")
            with open(FLAG, "w") as f:
                f.write(f"{_now()}\nstale: {stale_summary}\nmissing: {missing}\n")
            with open(LOG, "a") as f:
                f.write(f"[{_now()}] STALE: {stale_summary} | MISSING: {missing}\n")
        except OSError as e:
            print(f"  [warn] flag write failed: {e}", file=sys.stderr)
        return 2
    else:
        # All fresh — clear the flag if it exists. Keep the log as audit trail.
        if FLAG.exists():
            try:
                FLAG.unlink()
                with open(LOG, "a") as f:
                    f.write(f"[{_now()}] all fresh — cleared flag\n")
            except OSError:
                pass
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress output unless something is stale.")
    ap.add_argument("--json", action="store_true", help="Emit JSON.")
    args = ap.parse_args()
    return run(quiet=args.quiet, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
