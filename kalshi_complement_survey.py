"""Kalshi complement-arb survey.

Alternative to the 3-way survey. For every individual binary Kalshi
market, check whether `yes_ask + no_ask < 1.00` (the complement arb
condition). If yes, buying both sides guarantees $1 payoff at
resolution for total cost < $1 — risk-free after fees.

Unlike the 3-way survey, this works on ANY binary Kalshi market:
politics, weather, sports, commodities, props, totals, etc. Much
larger candidate universe (thousands of markets vs. hundreds of
3-way events).

Same usage pattern as kalshi_3way_survey.py — read-only, no creds,
2-5 minute runtime, CSV output.

Decision rule:
  • If any meaningful count of markets clears net > $1 after fees,
    complement arb on Kalshi is a viable strategy.
  • If zero across the universe, Kalshi is fully efficient on the
    complement math AND retail-prediction-market arb is closed for
    this venue.
"""

import csv
import math
import sys
import time
from collections import defaultdict, Counter
from typing import Dict, List, Optional

import requests


BASE = "https://api.elections.kalshi.com/trade-api/v2"
PAGE_LIMIT = 1000
REQUEST_TIMEOUT = 30
PARLAY_PREFIXES = ("KXMVE", "KXMB", "KXMVECROSSCATEGORY", "KXMVESPORTS")

# Model capital per complement arb. Per-arb economics — same unit we
# use in Engine 3 ($100 CAPITAL_PER_ARB).
ARB_CAPITAL_USD = 100.0

# Net-edge buckets for the report.
NET_EDGE_BUCKETS_USD = [0.10, 0.25, 0.50, 1.00, 2.00, 5.00]

# Volume floor — a market with zero trading volume isn't executable
# even if it shows an apparent arb. Filter to some minimum daily.
MIN_VOLUME_USD = 100.0

CSV_OUT = "/Users/stevenemery/Documents/kalshi_complement_survey.csv"


def _fp_to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _kalshi_fee_per_contract(price: float) -> float:
    """Kalshi taker fee: 0.07 × p × (1-p), ceil to cent."""
    if price <= 0 or price >= 1:
        return 0.0
    raw = 0.07 * price * (1.0 - price)
    return math.ceil(raw * 100.0) / 100.0


def paginate_markets() -> List[dict]:
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


def analyze_complement(m: dict) -> Optional[dict]:
    """For one binary market, compute complement arb economics per
    $100 capital. Returns None if pricing is incomplete or volume
    too thin."""
    yes_ask = _fp_to_float(m.get("yes_ask_dollars"))
    no_ask = _fp_to_float(m.get("no_ask_dollars"))
    yes_bid = _fp_to_float(m.get("yes_bid_dollars"))
    vol = _fp_to_float(m.get("volume_24h_fp"))

    # If no_ask isn't populated directly (shouldn't happen but defensive),
    # derive from 1 - yes_bid.
    if no_ask <= 0 and yes_bid > 0:
        no_ask = round(1.0 - yes_bid, 4)

    if yes_ask <= 0 or yes_ask >= 1:
        return None
    if no_ask <= 0 or no_ask >= 1:
        return None
    if vol < MIN_VOLUME_USD:
        return None

    total_cost = yes_ask + no_ask  # dollar cost per $1 of guaranteed payoff

    # Equal-payoff dutching at $ARB_CAPITAL_USD of capital:
    #   contracts per leg (uniform) = C / total_cost
    #   payoff at resolution = C / total_cost (one side wins, pays $1 each)
    #   gross = C/total_cost - C
    contracts = ARB_CAPITAL_USD / total_cost if total_cost > 0 else 0
    gross = (ARB_CAPITAL_USD / total_cost - ARB_CAPITAL_USD) if total_cost > 0 else 0
    fee_yes = contracts * _kalshi_fee_per_contract(yes_ask)
    fee_no = contracts * _kalshi_fee_per_contract(no_ask)
    total_fee = fee_yes + fee_no
    net = gross - total_fee

    return {
        "ticker": m.get("ticker", ""),
        "title": (m.get("title") or "")[:60],
        "event_ticker": m.get("event_ticker") or m.get("event") or "",
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "total_cost": total_cost,
        "contracts_per_leg": contracts,
        "gross_pnl_usd": gross,
        "total_fee_usd": total_fee,
        "net_edge_usd": net,
        "volume_24h_usd": vol,
    }


def family_from_ticker(ticker: str) -> str:
    """Classify a Kalshi ticker into a market family (coarse)."""
    t = ticker.upper()
    if t.startswith("KXHIGH") or t.startswith("KXLOW"):
        return "weather"
    if any(s in t for s in ("NBA", "NFL", "MLB", "NHL", "NCAA",
                             "ITF", "EPL", "LIGUE", "LALIGA", "EFL",
                             "COPPA", "SERIEA", "BUNDESLIGA", "WC",
                             "MLSGAME", "UFC", "FIFA")):
        return "sports"
    if any(s in t for s in ("TRUMP", "BIDEN", "VOTE", "ELECT", "CONGRESS",
                             "SHUTDOWN", "IRAN", "ISRAEL", "UKRAINE",
                             "TARIFF", "KASHOUT", "PRIMARY", "SCOTUS")):
        return "politics"
    if any(s in t for s in ("WTI", "CRYPTO", "BTC", "ETH", "AAAGAS",
                             "OIL", "GDP", "CPI", "FED", "CRUDE")):
        return "commodities"
    if any(s in t for s in ("RT-", "OSCAR", "EMMY", "GRAMMY", "BOX")):
        return "entertainment"
    if any(s in t for s in ("MOV", "REDISTRICT", "MAP")):
        return "politics"  # redistricting / movement
    return "other"


def main():
    print("=" * 90)
    print("KALSHI COMPLEMENT-ARB SURVEY")
    print("=" * 90)
    print(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Capital per arb: ${ARB_CAPITAL_USD:.0f}")
    print(f"Volume floor: ${MIN_VOLUME_USD:.0f}/day (excludes dead markets)")
    print()

    print("Paginating /markets ...")
    markets = paginate_markets()
    print(f"  total (post-parlay-filter): {len(markets):,} markets")
    print()

    analyzed: List[dict] = []
    skipped_no_pricing = 0
    skipped_low_vol = 0
    for m in markets:
        yes_ask = _fp_to_float(m.get("yes_ask_dollars"))
        no_ask = _fp_to_float(m.get("no_ask_dollars"))
        vol = _fp_to_float(m.get("volume_24h_fp"))
        if yes_ask <= 0 or yes_ask >= 1 or no_ask <= 0 or no_ask >= 1:
            skipped_no_pricing += 1
            continue
        if vol < MIN_VOLUME_USD:
            skipped_low_vol += 1
            continue
        result = analyze_complement(m)
        if result is not None:
            analyzed.append(result)

    print(f"Filtering results:")
    print(f"  analyzable (2-sided pricing + vol ≥ ${MIN_VOLUME_USD:.0f}/d): "
          f"{len(analyzed):,}")
    print(f"  skipped — missing/one-sided pricing: {skipped_no_pricing:,}")
    print(f"  skipped — below volume floor: {skipped_low_vol:,}")
    print()

    if not analyzed:
        print("Nothing to analyze — universe is too thin right now.")
        return

    # total_cost distribution (complement arb when < 1.0).
    costs = [a["total_cost"] for a in analyzed]
    costs.sort()
    print(f"total_cost (= yes_ask + no_ask) distribution:")
    print(f"  min:    {min(costs):.4f}")
    print(f"  p25:    {costs[len(costs)//4]:.4f}")
    print(f"  median: {costs[len(costs)//2]:.4f}")
    print(f"  p75:    {costs[3*len(costs)//4]:.4f}")
    print(f"  max:    {max(costs):.4f}")
    print(f"  count < 1.000: {sum(1 for c in costs if c < 1.000)}")
    print(f"  count < 0.995: {sum(1 for c in costs if c < 0.995)}")
    print(f"  count < 0.99:  {sum(1 for c in costs if c < 0.99)}")
    print(f"  count < 0.95:  {sum(1 for c in costs if c < 0.95)}")
    print(f"  count < 0.90:  {sum(1 for c in costs if c < 0.90)}")
    print()

    nets = [a["net_edge_usd"] for a in analyzed]
    nets.sort()
    print(f"Net edge after Kalshi fees (per ${ARB_CAPITAL_USD:.0f} arb):")
    print(f"  min:    ${min(nets):+.3f}")
    print(f"  p25:    ${nets[len(nets)//4]:+.3f}")
    print(f"  median: ${nets[len(nets)//2]:+.3f}")
    print(f"  p75:    ${nets[3*len(nets)//4]:+.3f}")
    print(f"  max:    ${max(nets):+.3f}")
    for t in NET_EDGE_BUCKETS_USD:
        print(f"  count net > ${t:.2f}: "
              f"{sum(1 for n in nets if n > t)}")
    print()

    # Top 20 by net edge, with family breakdown.
    analyzed.sort(key=lambda x: -x["net_edge_usd"])
    print("Top 20 complement arbs by net edge:")
    for a in analyzed[:20]:
        fam = family_from_ticker(a["ticker"])
        print(f"  net=${a['net_edge_usd']:+7.3f}  "
              f"total={a['total_cost']:.4f}  "
              f"fee=${a['total_fee_usd']:.2f}  "
              f"vol=${a['volume_24h_usd']:,.0f}/d  "
              f"[{fam:<12s}] {a['ticker'][:40]}")
    print()

    # Family distribution of positive-net-edge candidates.
    positive = [a for a in analyzed if a["net_edge_usd"] > 0]
    if positive:
        by_fam = Counter(family_from_ticker(a["ticker"]) for a in positive)
        print(f"Families with positive-net-edge candidates "
              f"({len(positive)} total positive):")
        for fam, n in by_fam.most_common():
            print(f"  {fam:<14s}  {n:,}")
        print()

    # CSV output.
    try:
        with open(CSV_OUT, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticker", "title", "event_ticker",
                        "yes_ask", "no_ask", "total_cost",
                        "contracts_per_leg", "gross_pnl_usd",
                        "total_fee_usd", "net_edge_usd",
                        "volume_24h_usd", "family"])
            for a in analyzed:
                w.writerow([
                    a["ticker"], a["title"], a["event_ticker"],
                    a["yes_ask"], a["no_ask"], a["total_cost"],
                    a["contracts_per_leg"], a["gross_pnl_usd"],
                    a["total_fee_usd"], a["net_edge_usd"],
                    a["volume_24h_usd"], family_from_ticker(a["ticker"]),
                ])
        print(f"Detailed CSV: {CSV_OUT}")
    except OSError as e:
        print(f"  [warn] CSV write failed: {e}")

    print()
    print("=" * 90)
    print("Complement survey done.")
    print("=" * 90)


if __name__ == "__main__":
    main()
