"""Portfolio Freshness Watchdog — engines.json-driven (session 7 v6).

One job: catch silent data starvation across all ACTIVE engines before it
costs another 12 days of stuck positions.

SOURCE OF TRUTH: ~/Documents/shadow_pnl/engines.json
Each active engine declares its own `monitoring.checks` list. This watchdog
reads that file every run and builds the check list dynamically — no
hardcoded engine references. When an engine is set to `active: false` in
engines.json, it disappears from monitoring automatically. ARCHIVE = SILENT
by construction. No more T1-style ghost alarms forcing T6 into dry-run.

The engines.json schema for each active engine:

    "T6": {
      "active": true,
      "monitoring": {
        "show_in_dashboard": true,
        "ws_logger_path": "terminal6_data/ws_logger.log",
        "checks": [
          {"label": "kalshi logger (WS)", "path": "terminal6_data/ws_logger.log",
           "max_age_h": 0.1, "missing_ok": false},
          ...
        ]
      }
    }

How it works:
- For each watched data file, check mtime vs expected_max_age_h threshold.
- If stale: print a loud row, append to ~/Documents/freshness_alarm.log,
  and write ~/Documents/freshness_alarm.flag so traders refuse new positions.
- Exit 0 if all fresh, 2 if any stale. Suitable for cron (`*/15 * * * *`).

Trader integration (one-liner at the top of each open() codepath):

    from pathlib import Path
    if (Path.home() / "Documents" / "freshness_alarm.flag").exists():
        return  # block new positions

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
ENGINES_JSON = DOCS / "shadow_pnl" / "engines.json"
FLAG = DOCS / "freshness_alarm.flag"
LOG = DOCS / "freshness_alarm.log"


@dataclass
class Check:
    engine: str
    label: str
    path: Path
    expected_max_age_h: float
    missing_ok: bool = False


def _load_engines() -> dict:
    try:
        return json.loads(ENGINES_JSON.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"[fatal] cannot read engines.json: {e}", file=sys.stderr)
        return {}


def load_checks() -> List[Check]:
    """Build the check list from engines.json — only ACTIVE engines contribute.
    Set `active: false` on an engine to silence all its monitoring."""
    engines = _load_engines()
    checks: List[Check] = []
    for eid, meta in engines.items():
        # Skip the _doc string and any non-dict entries
        if not isinstance(meta, dict):
            continue
        # ARCHIVED ENGINES NEVER GET CHECKED. This is the whole point.
        if not meta.get("active"):
            continue
        mon = meta.get("monitoring") or {}
        for c in mon.get("checks") or []:
            try:
                checks.append(Check(
                    engine=eid,
                    label=c["label"],
                    path=DOCS / c["path"],
                    expected_max_age_h=float(c["max_age_h"]),
                    missing_ok=bool(c.get("missing_ok", False)),
                ))
            except (KeyError, TypeError, ValueError) as e:
                print(f"[warn] {eid} check skipped (bad config): {c!r} ({e})",
                      file=sys.stderr)
    return checks


def _age_hours(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    return (time.time() - mtime) / 3600.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def run(quiet: bool, as_json: bool) -> int:
    checks = load_checks()
    rows = []
    any_stale = False

    for c in checks:
        age = _age_hours(c.path)
        if age is None:
            stale = not c.missing_ok
            status = "MISSING" if stale else "missing-ok"
        else:
            stale = age > c.expected_max_age_h
            status = "STALE" if stale else "fresh"
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
        print(json.dumps(
            {"now": _now(), "any_stale": any_stale,
             "engines_checked": sorted({r["engine"] for r in rows}),
             "rows": rows},
            indent=2,
        ))
    else:
        if any_stale or not quiet:
            print(f"[{_now()}] portfolio_freshness_watchdog "
                  f"(engines: {sorted({r['engine'] for r in rows})})")
            print(f"  {'engine':<5} {'label':<28} {'age':>10} {'max':>8}  status")
            for r in rows:
                age_disp = "—" if r["age_h"] is None else f"{r['age_h']:.2f}h"
                print(f"  {r['engine']:<5} {r['label']:<28} {age_disp:>10} "
                      f"{r['max_age_h']:>6.1f}h  {r['status']}")
            if any_stale:
                print(f"  STALE PRESENT — wrote flag {FLAG}")

    if any_stale:
        try:
            FLAG.parent.mkdir(parents=True, exist_ok=True)
            stale_summary = "; ".join(
                f"{r['engine']}/{r['label']}={r['age_h']:.1f}h"
                for r in rows if r["stale"] and r["age_h"] is not None
            )
            missing = ", ".join(
                f"{r['engine']}/{r['label']}" for r in rows if r["status"] == "MISSING"
            )
            with open(FLAG, "w") as f:
                f.write(f"{_now()}\nstale: {stale_summary}\nmissing: {missing}\n")
            with open(LOG, "a") as f:
                f.write(f"[{_now()}] STALE: {stale_summary} | MISSING: {missing}\n")
        except OSError as e:
            print(f"  [warn] flag write failed: {e}", file=sys.stderr)
        return 2
    else:
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
