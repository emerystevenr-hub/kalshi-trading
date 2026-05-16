"""T1 Edge Analysis — does T1 have detectable edge after fees?

Reads the closed T1 positions from shadow_pnl/ledger.jsonl, computes
overall and bucketed statistics. Reports 95% CI on per-trade P&L
(both shadow numbers and after-fee estimates), then breaks the
universe into buckets to surface any subset with positive edge.

Goal: answer "is T1 ready for real money, in whole or in part?"

Usage:
    python3 t1_edge_analysis.py
    python3 t1_edge_analysis.py --fee 0.04   # alternate fee/contract assumption
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LEDGER = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"


def parse_ticker(ticker: str) -> dict:
    # KXHIGH/KXLOW market ticker conventions:
    #   KXHIGHTNYC-26MAY02-T72       (YES top-cap above 72)
    #   KXHIGHTNYC-26MAY02-B73.5     (in-range YES around 73.5)
    #   KXHIGHTNYC-26MAY02-B69.5     (also in-range)
    parts = ticker.split("-")
    if len(parts) < 3:
        return {}
    head = parts[0]  # KXHIGHTNYC, KXLOWTPHX, KXHIGHNYC, KXLOWNYC
    market = parts[1]
    strike_part = parts[2]
    # Determine metric and station
    metric = None
    station = None
    if head.startswith("KXHIGHT"):
        metric = "high"
        station = head[7:]
    elif head.startswith("KXHIGH"):
        metric = "high"
        station = head[6:]
    elif head.startswith("KXLOWT"):
        metric = "low"
        station = head[6:]
    elif head.startswith("KXLOW"):
        metric = "low"
        station = head[5:]
    strike_kind = "top" if strike_part.startswith("T") else (
        "bottom" if strike_part.startswith("B") else "?")
    return {"metric": metric, "station": station, "strike_kind": strike_kind,
            "market": market}


def load_t1_pairs() -> List[dict]:
    """Return list of dicts, one per CLOSED T1 position, joining the open + close."""
    opens: Dict[str, dict] = {}
    closes: Dict[str, dict] = {}
    for line in open(LEDGER):
        r = json.loads(line)
        if r.get("engine") != "T1" and r.get("type") != "close":
            continue
        if r.get("type") == "open" and r.get("engine") == "T1":
            opens[r["position_id"]] = r
        elif r.get("type") == "close":
            closes[r["position_id"]] = r
    pairs = []
    for pid, o in opens.items():
        c = closes.get(pid)
        if not c:
            continue
        pairs.append({
            "pid": pid,
            "ticker": o["ticker"],
            "side": o["side"],
            "entry_price": o["price"],
            "size": o["size"],
            "cost_usd": o["cost_usd"],
            "open_ts": o["ts"],
            "close_ts": c["ts"],
            "settle_price": c.get("settle_price"),
            "proceeds_usd": c.get("proceeds_usd", 0),
            "realized_pnl_usd": c.get("realized_pnl_usd", 0),
            "outcome": c.get("outcome"),
            **parse_ticker(o["ticker"]),
        })
    return pairs


def mean_ci(values: List[float]) -> Tuple[float, float, float, float]:
    """Return (mean, std, lower_95, upper_95) using normal approximation."""
    n = len(values)
    if n == 0:
        return 0, 0, 0, 0
    m = statistics.fmean(values)
    if n == 1:
        return m, 0, m, m
    s = statistics.stdev(values)
    se = s / math.sqrt(n)
    return m, s, m - 1.96 * se, m + 1.96 * se


def report(pairs: List[dict], fee_per_contract: float) -> None:
    print(f"T1 edge analysis  —  closed positions: {len(pairs)}")
    print(f"Assumed fee per contract round-trip: ${fee_per_contract:.3f}")
    print()

    # ---- Overall stats (shadow vs after fees) ----
    pnls_shadow = [p["realized_pnl_usd"] for p in pairs]
    pnls_aftfee = [p["realized_pnl_usd"] - fee_per_contract * p["size"] for p in pairs]
    wins = sum(1 for p in pairs if p["realized_pnl_usd"] > 0)
    losses = sum(1 for p in pairs if p["realized_pnl_usd"] < 0)
    flats = len(pairs) - wins - losses

    m_s, sd_s, lo_s, hi_s = mean_ci(pnls_shadow)
    m_f, sd_f, lo_f, hi_f = mean_ci(pnls_aftfee)
    total_shadow = sum(pnls_shadow)
    total_aftfee = sum(pnls_aftfee)

    print("=== OVERALL ===")
    print(f"win/loss/flat: {wins}/{losses}/{flats}    win rate {wins/len(pairs):.1%}")
    print(f"avg cost basis: ${statistics.fmean([p['cost_usd'] for p in pairs]):.2f}")
    print(f"avg entry price: ${statistics.fmean([p['entry_price'] for p in pairs]):.3f}")
    print()
    print("                       mean$/close       std       95% CI on mean$/close       cumulative")
    print(f"shadow (no fees):      ${m_s:+.4f}       ${sd_s:.3f}    [${lo_s:+.4f}, ${hi_s:+.4f}]      ${total_shadow:+.2f}")
    print(f"after-fee est:         ${m_f:+.4f}       ${sd_f:.3f}    [${lo_f:+.4f}, ${hi_f:+.4f}]      ${total_aftfee:+.2f}")
    print()
    print(f"Sharpe (shadow): {m_s/sd_s*math.sqrt(len(pairs)):.2f}")
    print(f"Sharpe (after-fee): {m_f/sd_f*math.sqrt(len(pairs)):.2f}")
    print()

    # ---- Per-bucket analyses ----
    def bucket_report(name: str, key_fn, min_n=20):
        groups: Dict = defaultdict(list)
        for p in pairs:
            try:
                k = key_fn(p)
            except Exception:
                continue
            if k is None:
                continue
            groups[k].append(p)
        if not groups:
            return
        print(f"=== BY {name.upper()} (min n={min_n} for CI flag) ===")
        print(f"  {'bucket':<22} {'n':>4} {'win%':>6} {'mean$':>10} {'CI low':>10} {'CI high':>10} {'flag'}")
        keys = sorted(groups.keys(), key=lambda k: -len(groups[k]))
        for k in keys:
            grp = groups[k]
            n = len(grp)
            if n < 5:
                continue
            pnls = [p["realized_pnl_usd"] - fee_per_contract * p["size"] for p in grp]
            wins_ = sum(1 for x in pnls if x > 0)
            m, sd, lo, hi = mean_ci(pnls)
            flag = "EDGE" if (n >= min_n and lo > 0) else (
                "LOSS" if (n >= min_n and hi < 0) else "")
            print(f"  {str(k):<22} {n:>4} {wins_/n:>5.1%} ${m:>+9.4f} ${lo:>+9.4f} ${hi:>+9.4f}  {flag}")
        print()

    bucket_report("side", lambda p: p["side"])
    bucket_report("station", lambda p: p["station"])
    bucket_report("metric", lambda p: p["metric"])
    bucket_report("strike_kind", lambda p: p["strike_kind"])
    bucket_report("entry_price_decile",
                  lambda p: f"{int(p['entry_price']*10)*10:02d}-{int(p['entry_price']*10)*10+10:02d}c")
    bucket_report("cost_decile",
                  lambda p: f"${int(p['cost_usd']/2)*2}-{int(p['cost_usd']/2)*2+2}")
    bucket_report("side_x_metric", lambda p: f"{p['side']}/{p['metric']}")
    bucket_report("side_x_strike", lambda p: f"{p['side']}/{p['strike_kind']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee", type=float, default=0.04,
                    help="Fee per contract round-trip in $ (default 0.04)")
    args = ap.parse_args()
    pairs = load_t1_pairs()
    report(pairs, args.fee)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
