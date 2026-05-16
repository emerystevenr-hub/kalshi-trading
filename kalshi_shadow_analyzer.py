"""
Shadow data analyzer. Answers the right questions after v2 changes run:

  - Median holding time per category
  - % of positions that fully round-trip vs sit in inventory
  - Number and P&L impact of forced liquidations
  - Realized P&L decomposed: spread-capture vs liquidation exits
  - Open notional + unrealized + total P&L (mark-to-market)
  - Category-level ROI & turnover
  - Answers: "safer MM or reluctant directional trader?"

Data sources:
  kalshi_shadow_fills.csv    — all fills
  kalshi_shadow_quotes.csv   — quote snapshots for current mid
  kalshi_shadow_ws.log       — console output with LIQUIDATING markers

Usage:
  python3 kalshi_shadow_analyzer.py
"""

import csv
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from statistics import median
from typing import Dict, List, Tuple

HERE = os.path.dirname(__file__)
FILLS_CSV = os.path.join(HERE, "kalshi_shadow_fills.csv")
QUOTES_CSV = os.path.join(HERE, "kalshi_shadow_quotes.csv")
LOG_FILE = os.path.join(HERE, "kalshi_shadow_ws.log")

# Must match the CATEGORY_RULES in kalshi_shadow_ws.py
CATEGORY_RULES = [
    ("golf",             ["KXPGATOUR", "KXLPGATOUR", "KXKFTOUR"]),
    ("political_binary", ["KXNEXTAG", "KXTRUMPADMIN", "KXKASHOUT", "KXHEGSETHOUT"]),
    ("jump_risk",        ["KXUSAIRANAGREEMENT", "KXGOVTSHUTLENGTH", "KXTEAMSINNBAF"]),
    ("sports_other",     ["KXMARMAD", "KXPERUPRES"]),
]


def classify(ticker: str) -> str:
    for cat, prefixes in CATEGORY_RULES:
        for p in prefixes:
            if ticker.startswith(p):
                return cat
    return "default"


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_fills() -> List[dict]:
    if not os.path.exists(FILLS_CSV):
        return []
    return list(csv.DictReader(open(FILLS_CSV)))


def load_liquidation_windows() -> List[Tuple[str, datetime]]:
    """Parse log for 🟡 LIQUIDATING / JUMP-RISK LIQUIDATE events. Returns (ticker, ts)."""
    events = []
    if not os.path.exists(LOG_FILE):
        return events
    pattern = re.compile(r"(\d{2}:\d{2}:\d{2}).*?🟡.*?LIQUIDAT(?:ING|E)\s+(\S+)")
    # Log timestamps are HH:MM:SS — map by nearest fill timestamp instead.
    # Simpler: just record the ticker substring.
    simple_re = re.compile(r"🟡\s*(?:LIQUIDATING|JUMP-RISK LIQUIDATE)\s+(\S+)")
    with open(LOG_FILE) as f:
        for line in f:
            m = simple_re.search(line)
            if m:
                events.append(m.group(1))
    return events


def latest_mids() -> Dict[str, float]:
    """Scan quotes CSV for the most recent best_bid/best_ask per ticker → mid."""
    latest = {}
    if not os.path.exists(QUOTES_CSV):
        return latest
    for r in csv.DictReader(open(QUOTES_CSV)):
        t = r["ticker"]
        bb, ba = float(r["best_bid"]), float(r["best_ask"])
        if bb > 0 and ba > 0:
            latest[t] = (bb + ba) / 2.0
    return latest


def round_trip_analysis(fills: List[dict], liquidated_tickers: set):
    """
    FIFO-match buys with sells per ticker. Return per-category:
      - list of holding-time-minutes for completed round trips
      - list of current-open-age-minutes for still-open buys
      - separate tracking for liquidation-sourced sells
    """
    from collections import defaultdict, deque
    open_buys = defaultdict(deque)  # ticker → deque of (ts, size, px)
    rt_by_cat = defaultdict(list)    # category → [hold_minutes, ...]
    rt_pnl_by_cat_and_type = defaultdict(lambda: {"spread": 0.0, "liquidate": 0.0})
    still_open_by_cat = defaultdict(list)  # category → [open_age_minutes, ...]
    spread_fills = defaultdict(int)   # category → count
    liquidate_fills = defaultdict(int)

    if not fills:
        return {}

    latest_ts = parse_iso(fills[-1]["timestamp"])

    for r in fills:
        t = r["ticker"]
        cat = classify(t)
        ts = parse_iso(r["timestamp"])
        size = float(r["size"])
        px = float(r["fill_px"])

        if r["side"] == "BUY":
            open_buys[t].append((ts, size, px))
        else:
            # Determine if this sell was a liquidation (heuristic: ticker appears in liq events AND
            # sell_px <= best_bid at that time OR ticker category is jump_risk with residual)
            is_liq = t in liquidated_tickers
            remaining = size
            sell_pnl = 0.0
            while remaining > 0 and open_buys[t]:
                buy_ts, buy_size, buy_px = open_buys[t][0]
                matched = min(remaining, buy_size)
                hold_min = (ts - buy_ts).total_seconds() / 60.0
                rt_by_cat[cat].append(hold_min)
                pnl_piece = (px - buy_px) * matched
                sell_pnl += pnl_piece
                remaining -= matched
                buy_size -= matched
                if buy_size <= 0:
                    open_buys[t].popleft()
                else:
                    open_buys[t][0] = (buy_ts, buy_size, buy_px)

            if is_liq:
                liquidate_fills[cat] += 1
                rt_pnl_by_cat_and_type[cat]["liquidate"] += sell_pnl
            else:
                spread_fills[cat] += 1
                rt_pnl_by_cat_and_type[cat]["spread"] += sell_pnl

    # Age of still-open buys
    for t, q in open_buys.items():
        cat = classify(t)
        for buy_ts, buy_size, buy_px in q:
            age = (latest_ts - buy_ts).total_seconds() / 60.0
            still_open_by_cat[cat].append((age, buy_size, buy_px, t))

    return {
        "rt_by_cat": rt_by_cat,
        "rt_pnl_by_cat_and_type": rt_pnl_by_cat_and_type,
        "still_open_by_cat": still_open_by_cat,
        "spread_fills": spread_fills,
        "liquidate_fills": liquidate_fills,
    }


def main():
    fills = load_fills()
    if not fills:
        print("No fills yet.")
        return

    liquidated = set(load_liquidation_windows())
    mids = latest_mids()

    print("=" * 100)
    print(f"SHADOW ANALYZER — {len(fills)} fills")
    print("=" * 100)

    # Overall stats
    buys = sum(1 for r in fills if r["side"] == "BUY")
    sells = sum(1 for r in fills if r["side"] == "SELL")
    realized_final = float(fills[-1]["realized_pnl"])

    print(f"\nTotal fills: {len(fills)}  (buys: {buys}  sells: {sells})")
    print(f"Realized P&L (running): ${realized_final:+.2f}")
    print(f"Liquidation events detected: {len(liquidated)} unique tickers")
    print(f"  tickers: {sorted(liquidated)}" if liquidated else "  (none — nothing auto-liquidated)")

    # Mark-to-market open inventory
    open_inv_per_market = defaultdict(lambda: {"shares": 0.0, "cost_notional": 0.0})
    for r in fills:
        t = r["ticker"]
        size = float(r["size"])
        px = float(r["fill_px"])
        if r["side"] == "BUY":
            open_inv_per_market[t]["shares"] += size
            open_inv_per_market[t]["cost_notional"] += size * px
        else:
            # Reduce proportionally (FIFO-ish approximation for mark-to-market)
            d = open_inv_per_market[t]
            if d["shares"] > 0:
                avg_cost = d["cost_notional"] / d["shares"]
                reduced_shares = min(size, d["shares"])
                d["shares"] -= reduced_shares
                d["cost_notional"] -= reduced_shares * avg_cost

    total_mkt_val = 0
    total_unrealized = 0
    total_tail_risk = 0
    print(f"\n{'market':<42s} {'cat':<16s} {'inv':>6s} {'avg_cost':>9s} {'mid_now':>9s} {'mkt_val':>9s} {'unreal':>9s}")
    print("-" * 110)
    for t, d in sorted(open_inv_per_market.items(), key=lambda x: -x[1]["shares"]):
        if d["shares"] <= 0.01:
            continue
        avg_cost = d["cost_notional"] / d["shares"]
        mid = mids.get(t, 0)
        mkt_val = d["shares"] * mid
        unreal = (mid - avg_cost) * d["shares"] if mid > 0 else 0
        tail = d["cost_notional"]
        total_mkt_val += mkt_val
        total_unrealized += unreal
        total_tail_risk += tail
        print(f"{t[:42]:<42s} {classify(t):<16s} {d['shares']:>6.0f} {avg_cost:>9.3f} {mid:>9.4f} ${mkt_val:>+7.2f} ${unreal:>+7.2f}")

    total_pnl = realized_final + total_unrealized
    print()
    print(f"Realized P&L:         ${realized_final:+.2f}")
    print(f"Unrealized P&L:       ${total_unrealized:+.2f}")
    print(f"TOTAL P&L (MTM):      ${total_pnl:+.2f}")
    print(f"Open notional:        ${total_mkt_val:+.2f}")
    print(f"Tail risk (all NO):   -${total_tail_risk:.2f}")
    print(f"Ratio tail/total:     {total_tail_risk / max(abs(total_pnl), 0.01):.1f}x")

    # Round-trip analysis
    analysis = round_trip_analysis(fills, liquidated)
    if not analysis:
        return

    print("\n" + "=" * 100)
    print("ROUND-TRIP BEHAVIOR BY CATEGORY")
    print("=" * 100)
    print(f"{'category':<18s} {'trips':>6s} {'median_min':>11s} {'spread_fills':>13s} {'liq_fills':>10s} {'spread_pnl':>11s} {'liq_pnl':>10s} {'open':>5s}")
    print("-" * 110)
    for cat in ["golf", "political_binary", "jump_risk", "sports_other", "default"]:
        trips = analysis["rt_by_cat"].get(cat, [])
        n = len(trips)
        if n == 0 and len(analysis["still_open_by_cat"].get(cat, [])) == 0:
            continue
        med = median(trips) if trips else 0
        pnl = analysis["rt_pnl_by_cat_and_type"].get(cat, {"spread": 0, "liquidate": 0})
        spread_fills_n = analysis["spread_fills"].get(cat, 0)
        liq_fills_n = analysis["liquidate_fills"].get(cat, 0)
        open_n = len(analysis["still_open_by_cat"].get(cat, []))
        print(f"{cat:<18s} {n:>6d} {med:>11.1f} {spread_fills_n:>13d} {liq_fills_n:>10d} ${pnl['spread']:>+9.2f} ${pnl['liquidate']:>+8.2f} {open_n:>5d}")

    # Stale inventory report
    stale = []
    for cat, opens in analysis["still_open_by_cat"].items():
        for age, size, px, t in opens:
            if age > 60:
                stale.append((age, t, cat, size, px))
    if stale:
        print("\n" + "=" * 100)
        print(f"STALE INVENTORY (open >60 min, not turning over): {len(stale)} positions")
        print("=" * 100)
        for age, t, cat, size, px in sorted(stale, key=lambda x: -x[0])[:15]:
            print(f"  {age:>6.0f}min  {t[:38]:<40s} [{cat}]  {size:.0f} shares @ ${px:.3f}")
    else:
        print("\n✓ No stale inventory (everything turned over within 60 min).")

    # Verdict
    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    total_liq_pnl = sum(d.get("liquidate", 0) for d in analysis["rt_pnl_by_cat_and_type"].values())
    total_spread_pnl = sum(d.get("spread", 0) for d in analysis["rt_pnl_by_cat_and_type"].values())
    print(f"Spread-capture P&L:   ${total_spread_pnl:+.2f}")
    print(f"Liquidation P&L:      ${total_liq_pnl:+.2f}")
    print(f"Realized discrepancy: ${realized_final - (total_spread_pnl + total_liq_pnl):+.2f} (FIFO vs bot's rolling-avg)")

    if total_pnl > 0 and total_tail_risk < 150 and total_liq_pnl > -5:
        verdict = "✓ Safer MM. Bounded tail risk, positive total P&L, controlled liquidations."
    elif total_pnl > 0 and total_liq_pnl < -10:
        verdict = "⚠ Profitable on net but forced exits hurt. Possibly over-constrained."
    elif total_pnl < 0 and total_liq_pnl < -5:
        verdict = "✗ Reluctant directional trader with forced exits. Constraints too tight or strategy doesn't work here."
    else:
        verdict = "Marginal. Need more data to call."
    print(f"\n{verdict}")


if __name__ == "__main__":
    main()
