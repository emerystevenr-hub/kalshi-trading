#!/usr/bin/env python3
"""Portfolio macro concentration check + hard cap enforcement.

Reads ~/Documents/shadow_pnl/engines.json, computes the share of deployed
capital that's in macro-correlated engines (T3a/T3b/T3c/T8/T9 etc), and
either prints a status table or evaluates whether a proposed engine
deployment would breach the 50% cap.

Born 2026-05-09 after the T2 thesis kill exposed that the portfolio had
no structural defense against accumulating correlated macro exposure.
The cap is the rule. The dashboard is how we know we're following it.

Usage:
    python3 portfolio_macro_concentration.py
        # Print current concentration table + verdict.

    python3 portfolio_macro_concentration.py --would-add NAME BANKROLL CATEGORY
        # Simulate adding an engine. Prints projected concentration and
        # exits 0 if under cap, 1 if at cap (50% exact), 2 if over cap.
        # CI/redeploy scripts can use the exit code as a hard gate.

    python3 portfolio_macro_concentration.py --json
        # Emit machine-readable JSON. For dashboard / status integration.

Categories that count as macro:
    macro       — Fed/inflation/labor/rate engines (T3a, T3b, T3c, T8)
    crypto      — inherits Fed-regime beta via risk-on correlation (T9)

Categories that do NOT count as macro:
    weather     — T1 (independent of macro regime)
    sports      — T6, T7, T11/NFL (independent)
    earnings    — T10 (partial — currently classified non-macro pending
                  build; revisit if structural correlation observed)
    arb         — convergence engines (independent of any thesis)
    infrastructure / archived — no deployed capital
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ENGINES_FILE = Path.home() / "Documents" / "shadow_pnl" / "engines.json"
MACRO_CAP = 0.50

# Categories that contribute to "macro" exposure.
MACRO_CATEGORIES = {"macro", "crypto"}

# Modes that count as deployed capital. Engines in planning, infrastructure,
# reserved, or archived modes don't count.
DEPLOYED_MODES = {"shadow", "shadow_subset", "live"}


def load_engines() -> Dict[str, dict]:
    if not ENGINES_FILE.exists():
        sys.stderr.write(f"ERROR: {ENGINES_FILE} not found\n")
        sys.exit(3)
    return json.loads(ENGINES_FILE.read_text())


def deployed(eng: dict) -> bool:
    return (
        eng.get("active", False)
        and eng.get("mode") in DEPLOYED_MODES
        and eng.get("bankroll_usd", 0) > 0
    )


def is_macro(eng: dict) -> bool:
    return eng.get("category") in MACRO_CATEGORIES


def compute(engines: Dict[str, dict]) -> dict:
    rows = []
    total = 0.0
    macro = 0.0
    for code, eng in engines.items():
        if not deployed(eng):
            continue
        bk = float(eng.get("bankroll_usd", 0))
        cat = eng.get("category", "uncategorized")
        rows.append({
            "code": code,
            "name": eng.get("name", code),
            "bankroll_usd": bk,
            "category": cat,
            "is_macro": cat in MACRO_CATEGORIES,
        })
        total += bk
        if cat in MACRO_CATEGORIES:
            macro += bk
    rows.sort(key=lambda r: -r["bankroll_usd"])
    pct = (macro / total) if total > 0 else 0.0
    headroom = total * MACRO_CAP - macro
    return {
        "rows": rows,
        "total_deployed_usd": total,
        "macro_deployed_usd": macro,
        "macro_pct": pct,
        "macro_cap_pct": MACRO_CAP,
        "macro_headroom_usd": headroom,
        "over_cap": pct > MACRO_CAP,
        "at_cap": abs(pct - MACRO_CAP) < 1e-9,
    }


def print_table(state: dict) -> None:
    print("=" * 70)
    print(" PORTFOLIO MACRO CONCENTRATION")
    print("=" * 70)
    print(f"  {'engine':<8} {'name':<36} {'bankroll':>10}  {'cat':<8} {'M':<3}")
    print(f"  {'-'*8} {'-'*36} {'-'*10}  {'-'*8} {'-'*3}")
    for r in state["rows"]:
        flag = "*" if r["is_macro"] else ""
        print(f"  {r['code']:<8} {r['name'][:36]:<36} ${r['bankroll_usd']:>8,.0f}  "
              f"{r['category']:<8} {flag:<3}")
    print(f"  {'-'*8} {'-'*36} {'-'*10}  {'-'*8} {'-'*3}")
    print(f"  {'TOTAL':<8} {'deployed capital':<36} ${state['total_deployed_usd']:>8,.0f}")
    print(f"  {'MACRO':<8} {'(* rows above)':<36} ${state['macro_deployed_usd']:>8,.0f}  "
          f"{state['macro_pct']*100:>5.1f}%")
    print(f"  {'CAP':<8} {'hard limit':<36} {'':<10}   {state['macro_cap_pct']*100:>5.0f}%")
    if state["over_cap"]:
        verdict = "OVER CAP — block new macro deployments and rebalance"
    elif state["at_cap"]:
        verdict = "AT CAP — block any new macro deployment"
    else:
        verdict = f"UNDER CAP — ${state['macro_headroom_usd']:,.0f} headroom for new macro"
    print(f"  {'STATUS':<8} {verdict}")
    print("=" * 70)


def project_with(state: dict, name: str, bankroll: float, category: str) -> dict:
    new_total = state["total_deployed_usd"] + bankroll
    new_macro = state["macro_deployed_usd"] + (bankroll if category in MACRO_CATEGORIES else 0.0)
    new_pct = new_macro / new_total if new_total > 0 else 0.0
    return {
        "would_add_name": name,
        "would_add_bankroll": bankroll,
        "would_add_category": category,
        "projected_total_usd": new_total,
        "projected_macro_usd": new_macro,
        "projected_macro_pct": new_pct,
        "would_breach": new_pct > MACRO_CAP + 1e-9,
        "would_be_at_cap": abs(new_pct - MACRO_CAP) < 1e-9,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--would-add", nargs=3, metavar=("NAME", "BANKROLL", "CATEGORY"),
                    help="Simulate adding an engine. BANKROLL in dollars; CATEGORY "
                         "is one of: macro, sports, weather, crypto, arb, earnings.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = ap.parse_args()

    engines = load_engines()
    state = compute(engines)

    if args.would_add:
        name, bk_str, cat = args.would_add
        try:
            bk = float(bk_str)
        except ValueError:
            sys.stderr.write(f"ERROR: bankroll must be numeric, got {bk_str!r}\n")
            return 3
        proj = project_with(state, name, bk, cat)
        if args.json:
            print(json.dumps({"current": state, "projection": proj}, indent=2))
        else:
            print_table(state)
            print()
            print(f"PROJECTED with {name} (${bk:,.0f}, {cat}):")
            print(f"  total: ${proj['projected_total_usd']:,.0f}")
            print(f"  macro: ${proj['projected_macro_usd']:,.0f} "
                  f"({proj['projected_macro_pct']*100:.1f}%)")
            if proj["would_breach"]:
                print(f"  VERDICT: WOULD BREACH CAP — REFUSE DEPLOYMENT")
            elif proj["would_be_at_cap"]:
                print(f"  VERDICT: WOULD HIT CAP EXACTLY — refuse further macro after this")
            else:
                print(f"  VERDICT: would fit under cap — deployment allowed")
        if proj["would_breach"]:
            return 2
        if proj["would_be_at_cap"]:
            return 1
        return 0

    if args.json:
        print(json.dumps(state, indent=2))
    else:
        print_table(state)

    if state["over_cap"]:
        return 2
    if state["at_cap"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
