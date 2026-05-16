"""Kalshi 3-way mutex event survey.

Read-only exploration of Kalshi's public `/markets` endpoint to answer:
  1. How many open events group 3 child markets (potential 3-way mutex)?
  2. Of those, how many look like soccer matches (or similar N-way)?
  3. What's the distribution of sum_yes_ask across them?
  4. How many clear fee-adjusted net-edge thresholds for a $100 arb?

No credentials required. No capital at risk. Pure signal survey to
decide whether to refactor Engine 1 into a Kalshi 3-way scout/executor.

Output: structured report to stdout + CSV to ~/Documents/ for later
analysis. Run time: 2-5 minutes depending on Kalshi API response speed.
"""

import csv
import json
import math
import sys
import time
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Tuple

import requests


BASE = "https://api.elections.kalshi.com/trade-api/v2"
PAGE_LIMIT = 1000
REQUEST_TIMEOUT = 30

# Exclude parlays and multi-leg combos — same filter Engine 1 uses.
PARLAY_PREFIXES = ("KXMVE", "KXMB", "KXMVECROSSCATEGORY", "KXMVESPORTS")

# Heuristic: soccer match events have "GAME" in the event_ticker prefix.
# (Observed: KXEFLCHAMPIONSHIPGAME-*, KXCOPPAITALIAGAME-*, etc.)
# We also accept anything with 3 children as a POTENTIAL 3-way; the GAME
# filter is additional confidence that it's soccer specifically.
SOCCER_GAME_MARKERS = ("GAME", "MATCH", "FIXTURE")

# Capital for the modeled arb.
ARB_CAPITAL_USD = 100.0

# Fee thresholds for "net edge > X" counts in the report.
NET_EDGE_BUCKETS_USD = [0.25, 0.50, 1.00, 2.00, 5.00]

CSV_OUT = "/Users/stevenemery/Documents/kalshi_3way_survey.csv"


def _fp_to_float(v) -> float:
    """Kalshi sends fixed-point numbers as strings like '0.4200'."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _kalshi_fee_per_contract(price: float) -> float:
    """Kalshi taker fee formula: 0.07 × p × (1-p), rounded up to nearest cent.
    Charged per contract, per fill. Same formula Engine 1 uses."""
    if price <= 0 or price >= 1:
        return 0.0
    raw = 0.07 * price * (1.0 - price)
    # Round UP to the nearest cent per Kalshi's published fee schedule.
    return math.ceil(raw * 100.0) / 100.0


def paginate_markets() -> List[dict]:
    """Pull all open markets via paginated REST. Filters out parlays."""
    cursor = None
    markets: List[dict] = []
    page = 0
    while True:
        params = {"limit": PAGE_LIMIT, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{BASE}/markets", params=params,
                             timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  [error] REST failed on page {page}: {e}", flush=True)
            break
        data = r.json()
        batch = data.get("markets", [])
        for m in batch:
            t = m.get("ticker", "")
            if any(t.startswith(p) for p in PARLAY_PREFIXES):
                continue
            markets.append(m)
        cursor = data.get("cursor")
        page += 1
        if page % 5 == 0:
            print(f"  paged {page * PAGE_LIMIT} markets "
                  f"(accumulated {len(markets)} after parlay filter)...",
                  flush=True)
        if not cursor or not batch:
            break
    return markets


def is_soccer_game_event(event_ticker: str) -> bool:
    """Heuristic: soccer event tickers contain GAME/MATCH/FIXTURE."""
    et = event_ticker.upper()
    return any(m in et for m in SOCCER_GAME_MARKERS)


def analyze_three_way_event(children: List[dict]) -> Optional[dict]:
    """Given the 3 child markets for one event, compute sum_ask,
    sum_bid, and fee-adjusted net edge per $100 of capital.

    Returns None if any child has missing pricing.
    """
    if len(children) != 3:
        return None

    asks: List[float] = []
    bids: List[float] = []
    vols: List[float] = []
    for c in children:
        ya = _fp_to_float(c.get("yes_ask_dollars"))
        yb = _fp_to_float(c.get("yes_bid_dollars"))
        v = _fp_to_float(c.get("volume_24h_fp"))
        if ya <= 0 or ya >= 1 or yb <= 0 or yb >= 1:
            return None
        asks.append(ya)
        bids.append(yb)
        vols.append(v)

    sum_ask = sum(asks)
    sum_bid = sum(bids)

    # Equal-payoff dutching economics with $ARB_CAPITAL_USD deployed:
    #   contracts per leg (uniform) = C / sum_ask
    #   payoff at resolution = C / sum_ask (one outcome wins, pays $1/contract)
    #   gross profit = C/sum_ask - C
    #   fees = contracts × sum_i(fee_per_contract(p_i))
    contracts = ARB_CAPITAL_USD / sum_ask if sum_ask > 0 else 0
    gross = (ARB_CAPITAL_USD / sum_ask - ARB_CAPITAL_USD) if sum_ask > 0 else 0
    total_fee = contracts * sum(_kalshi_fee_per_contract(p) for p in asks)
    net = gross - total_fee

    return {
        "sum_ask": sum_ask,
        "sum_bid": sum_bid,
        "asks": asks,
        "bids": bids,
        "min_vol_24h": min(vols),
        "max_vol_24h": max(vols),
        "contracts_per_leg": contracts,
        "gross_pnl_usd": gross,
        "total_fee_usd": total_fee,
        "net_edge_usd": net,
    }


def main():
    print("=" * 90)
    print("KALSHI 3-WAY MUTEX EVENT SURVEY")
    print("=" * 90)
    print(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Capital modeled: ${ARB_CAPITAL_USD:.0f}")
    print()

    print("Paginating /markets ...")
    markets = paginate_markets()
    print(f"  total (post-parlay-filter): {len(markets):,} markets")
    print()

    # Group by event_ticker.
    by_event: Dict[str, List[dict]] = defaultdict(list)
    for m in markets:
        et = m.get("event_ticker") or m.get("event") or ""
        if not et:
            continue
        by_event[et].append(m)

    print(f"Unique event_tickers: {len(by_event):,}")

    # Size distribution: how many events have 2, 3, 4, 5+ children?
    size_buckets = Counter()
    for et, kids in by_event.items():
        size_buckets[len(kids)] += 1
    print(f"Event size distribution:")
    for n in sorted(size_buckets.keys())[:10]:
        print(f"  {n:2d} children: {size_buckets[n]:,} events")
    if max(size_buckets.keys(), default=0) > 10:
        big = sum(v for k, v in size_buckets.items() if k > 10)
        print(f"  >10 children: {big:,} events (likely multi-outcome / non-mutex)")
    print()

    # Filter to events with exactly 3 children.
    three_way = {et: kids for et, kids in by_event.items() if len(kids) == 3}
    print(f"3-child events (potential 3-way mutex): {len(three_way):,}")

    # Of those, how many look like soccer GAME/MATCH events?
    soccer_candidates = {et: kids for et, kids in three_way.items()
                          if is_soccer_game_event(et)}
    print(f"  with soccer/game/match marker in ticker: {len(soccer_candidates):,}")
    print()

    # Analyze each candidate.
    analyzed: List[dict] = []
    invalid_pricing = 0
    for et, kids in soccer_candidates.items():
        metrics = analyze_three_way_event(kids)
        if metrics is None:
            invalid_pricing += 1
            continue
        analyzed.append({
            "event_ticker": et,
            "titles": [k.get("title", "")[:40] for k in kids],
            **metrics,
        })

    print(f"Analysis of {len(soccer_candidates)} soccer 3-way candidates:")
    print(f"  with complete 2-sided pricing on all 3 legs: {len(analyzed)}")
    print(f"  missing/incomplete pricing: {invalid_pricing}")
    print()

    if not analyzed:
        print("No analyzable 3-way soccer events with live pricing right now.")
        print("This could mean:")
        print("  - Kalshi has no active soccer matches in this window")
        print("  - Books are one-sided (no asks available)")
        print("  - Our heuristic (GAME/MATCH/FIXTURE) missed the real markers")
        print()
        print("Next step: inspect event_ticker patterns manually:")
        for et in list(three_way.keys())[:20]:
            print(f"  {et}")
        return

    # sum_ask distribution.
    sum_asks = [a["sum_ask"] for a in analyzed]
    print(f"sum_ask distribution (dutch arb when < 1.0):")
    print(f"  min:    {min(sum_asks):.4f}")
    print(f"  p25:    {sorted(sum_asks)[len(sum_asks)//4]:.4f}")
    print(f"  median: {sorted(sum_asks)[len(sum_asks)//2]:.4f}")
    print(f"  p75:    {sorted(sum_asks)[3*len(sum_asks)//4]:.4f}")
    print(f"  max:    {max(sum_asks):.4f}")
    print(f"  count < 1.00: {sum(1 for s in sum_asks if s < 1.0)}")
    print(f"  count < 0.95: {sum(1 for s in sum_asks if s < 0.95)}")
    print(f"  count < 0.90: {sum(1 for s in sum_asks if s < 0.90)}")
    print()

    # Net edge (post-fee) distribution.
    nets = [a["net_edge_usd"] for a in analyzed]
    print(f"Net edge after Kalshi fees (per ${ARB_CAPITAL_USD:.0f} arb):")
    print(f"  min:    ${min(nets):+.3f}")
    print(f"  p25:    ${sorted(nets)[len(nets)//4]:+.3f}")
    print(f"  median: ${sorted(nets)[len(nets)//2]:+.3f}")
    print(f"  p75:    ${sorted(nets)[3*len(nets)//4]:+.3f}")
    print(f"  max:    ${max(nets):+.3f}")
    for t in NET_EDGE_BUCKETS_USD:
        print(f"  count net > ${t:.2f}: "
              f"{sum(1 for n in nets if n > t)}")
    print()

    # Top 10 by net edge.
    print(f"Top 10 events by fee-adjusted net edge:")
    analyzed.sort(key=lambda x: -x["net_edge_usd"])
    for a in analyzed[:10]:
        print(f"  net=${a['net_edge_usd']:+6.2f}  "
              f"sum_ask={a['sum_ask']:.4f}  "
              f"fee=${a['total_fee_usd']:.2f}  "
              f"min_vol24h=${a['min_vol_24h']:,.0f}/d  "
              f"{a['event_ticker']}")
    print()

    # Write CSV for deeper analysis.
    try:
        with open(CSV_OUT, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["event_ticker", "title_0", "title_1", "title_2",
                        "ask_0", "ask_1", "ask_2",
                        "bid_0", "bid_1", "bid_2",
                        "sum_ask", "sum_bid",
                        "contracts_per_leg", "gross_pnl_usd",
                        "total_fee_usd", "net_edge_usd",
                        "min_vol_24h", "max_vol_24h"])
            for a in analyzed:
                w.writerow([
                    a["event_ticker"],
                    *(a["titles"] + [""] * 3)[:3],
                    *a["asks"], *a["bids"],
                    a["sum_ask"], a["sum_bid"],
                    a["contracts_per_leg"], a["gross_pnl_usd"],
                    a["total_fee_usd"], a["net_edge_usd"],
                    a["min_vol_24h"], a["max_vol_24h"],
                ])
        print(f"Detailed CSV: {CSV_OUT}")
    except OSError as e:
        print(f"  [warn] CSV write failed: {e}")

    print()
    print("=" * 90)
    print("Survey done.")
    print("=" * 90)


if __name__ == "__main__":
    main()
