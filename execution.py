"""
TIER 3: EXECUTION ENGINE
Three-layer institutional execution:
  Layer 1: Parallel FOK fills with size capped to top-of-book depth
  Layer 2: Reversal protocol when N-1 legs fill (orphan inventory cleanup)
  Layer 3: Pre-trigger size validation against live book

Plus position sizing module:
  - Walk-the-book VWAP optimal size
  - Quarter-Kelly capital cap
  - Per-trade hard cap (5%)
  - Cluster correlation cap
  - Adverse-selection size halving
"""

import asyncio
import time
from typing import Dict, List, Optional, Any
from statistics import median

from config import (
    TARGET_MARKETS,
    EDGE_THRESHOLD_NET,
    TOTAL_CAPITAL_USD,
    KELLY_FRACTION,
    PER_TRADE_CAP_PCT,
    CLUSTER_CAPS,
    ADVERSE_SELECTION_RATIO,
)

# In a real deployment, these come from py_clob_client. Stubbed for paper trading.
# from py_clob_client.client import ClobClient
# from py_clob_client.clob_types import OrderArgs, OrderType
# from py_clob_client.order_builder.constants import BUY, SELL


# ──────────────────────────────────────────────────────────────────────
# RUNTIME STATE
# ──────────────────────────────────────────────────────────────────────

# cluster -> capital currently deployed (refreshed on every fill/exit)
CURRENT_EXPOSURE: Dict[str, float] = {c: 0.0 for c in CLUSTER_CAPS}

# token_id -> rolling list of recent top-of-book depths (for adverse selection)
DEPTH_HISTORY: Dict[str, List[float]] = {}
DEPTH_HISTORY_MAX = 60  # ~1 minute @ 1Hz

# Master kill switch
PAPER_TRADING = True  # set False once you trust it with real money


# ──────────────────────────────────────────────────────────────────────
# SIZING — walk the book + Kelly + caps
# ──────────────────────────────────────────────────────────────────────

def sum_of_vwaps(legs_books: List[List[tuple]], test_size: float) -> float:
    """
    For a candidate share size, compute the VWAP fill cost on each leg by
    walking its ask ladder, then sum. Returns infinity if any leg can't fill.
    """
    total = 0.0
    for asks in legs_books:
        remaining = test_size
        cost = 0.0
        for price, depth in asks:  # ascending price order
            if remaining <= 0:
                break
            take = min(remaining, depth)
            cost += take * price
            remaining -= take
        if remaining > 0:
            return float("inf")
        total += cost / test_size  # this leg's VWAP
    return total


def compute_max_arb_size(legs_books: List[List[tuple]], threshold: float) -> float:
    """
    Binary search for the largest size where sum(VWAP_i) <= threshold.
    Returns size in shares.
    """
    if not legs_books or any(not asks for asks in legs_books):
        return 0.0

    max_supported = min(sum(s for _, s in asks) for asks in legs_books)
    if max_supported < 1:
        return 0.0

    # Binary search in 1-share increments (Polymarket trades in fractional but
    # share resolution is fine for sizing). For finer control switch to float
    # bisection with epsilon.
    lo, hi = 1, int(max_supported)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if sum_of_vwaps(legs_books, mid) <= threshold:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return float(best)


def kelly_capital(expected_edge: float, partial_fill_var: float = 0.0009) -> float:
    """
    Quarter-Kelly capital allocation.
    Default partial_fill_var ~ (3% reversal loss)^2 = 0.0009.
    """
    if partial_fill_var <= 0 or expected_edge <= 0:
        return 0.0
    full_kelly = expected_edge / partial_fill_var
    return TOTAL_CAPITAL_USD * KELLY_FRACTION * full_kelly


def adverse_selection_check(token_id: str, fillable_size: float) -> float:
    """
    If fillable size >> historical median depth, halve it. Defensive against
    bait quotes that vanish when crossed.
    """
    history = DEPTH_HISTORY.get(token_id, [])
    if len(history) < 5:
        return fillable_size  # not enough data → trust it
    med = median(history)
    if med > 0 and fillable_size > ADVERSE_SELECTION_RATIO * med:
        return fillable_size * 0.5
    return fillable_size


def compute_position_size(
    market_key: str,
    live_book: Dict[str, Dict[str, list]],
) -> Optional[Dict[str, Any]]:
    """
    Returns dict with sized legs ready for execution, or None if no viable trade.
    """
    market = TARGET_MARKETS[market_key]
    cluster = market["cluster"]
    tokens = market["tokens"]

    # Pull each leg's ask ladder from the live WS book
    legs_books = []
    leg_meta = []
    for leg_name, token_id in tokens.items():
        book = live_book.get(token_id, {})
        asks = book.get("asks", [])
        if not asks:
            return None
        legs_books.append(asks)
        leg_meta.append({
            "name": leg_name,
            "token_id": token_id,
            "best_ask": asks[0][0],
            "top_depth": asks[0][1],
        })

    # ── A. Walk-the-book max size that preserves edge ──
    book_max_shares = compute_max_arb_size(legs_books, EDGE_THRESHOLD_NET)
    if book_max_shares == 0:
        print(f"   ABORT: book walk shows no positive-EV size at threshold {EDGE_THRESHOLD_NET}")
        # Diagnostic: dump top 3 ask levels per leg so we can tell
        # "no size" vs "size exists but priced above EV cap"
        for meta in leg_meta:
            top3 = live_book.get(meta["token_id"], {}).get("asks", [])[:3]
            top3_fmt = [(round(p, 4), round(s, 1)) for p, s in top3]
            print(f"     {meta['name']:>12s}: top3 asks = {top3_fmt}")
        print(f"     sum(best_asks) = {sum(m['best_ask'] for m in leg_meta):.4f}")
        return None

    # Notional cost = sum(best_ask) × shares (each share costs ~$1 at resolution)
    sum_best_asks = sum(m["best_ask"] for m in leg_meta)
    book_max_capital = book_max_shares * sum_best_asks

    # ── B. Quarter-Kelly cap ──
    expected_edge = max(0.0, EDGE_THRESHOLD_NET - sum_best_asks)
    kelly_cap_capital = kelly_capital(expected_edge)

    # ── C. Per-trade hard cap (5%) ──
    per_trade_cap = TOTAL_CAPITAL_USD * PER_TRADE_CAP_PCT

    # ── D. Cluster correlation cap ──
    cluster_total = TOTAL_CAPITAL_USD * CLUSTER_CAPS.get(cluster, 0.05)
    cluster_remaining = max(0.0, cluster_total - CURRENT_EXPOSURE.get(cluster, 0.0))

    # Final capital = strictest constraint
    final_capital = min(book_max_capital, kelly_cap_capital, per_trade_cap, cluster_remaining)
    if final_capital < 1.0:
        print(f"   ABORT: capital cap collapsed to ${final_capital:.2f}")
        print(f"     book_max=${book_max_capital:.2f} kelly=${kelly_cap_capital:.2f} "
              f"per_trade=${per_trade_cap:.2f} cluster_remaining=${cluster_remaining:.2f}")
        return None

    final_shares = final_capital / sum_best_asks

    # ── E. Adverse-selection guard on each leg ──
    sized_legs = []
    for meta in leg_meta:
        adj_size = adverse_selection_check(meta["token_id"], final_shares)
        if adj_size < final_shares:
            print(f"   ⚠️  Adverse-selection guard halved {meta['name']}: "
                  f"{final_shares:.0f} → {adj_size:.0f}")
        sized_legs.append({
            "name": meta["name"],
            "token_id": meta["token_id"],
            "price": meta["best_ask"],
            "size": min(adj_size, final_shares),
        })

    # Use the smallest leg-adjusted size to keep the dutch balanced
    bound_size = min(leg["size"] for leg in sized_legs)
    for leg in sized_legs:
        leg["size"] = bound_size

    return {
        "market_key": market_key,
        "cluster": cluster,
        "legs": sized_legs,
        "capital_deployed": bound_size * sum_best_asks,
        "expected_edge_pct": (1.0 - sum_best_asks) * 100,
    }


# ──────────────────────────────────────────────────────────────────────
# LAYER 1: PARALLEL FOK
# ──────────────────────────────────────────────────────────────────────

async def fire_fok_limit(token_id: str, price: float, size: float) -> Dict[str, Any]:
    """
    Fires a Fill-Or-Kill limit order. In paper mode, simulates with random fill.
    """
    if PAPER_TRADING:
        # Simulate ~85% fill rate (realistic for FOK at top-of-book in panic windows)
        import random
        await asyncio.sleep(0.05 + random.random() * 0.10)  # network latency sim
        if random.random() < 0.85:
            return {"status": "filled", "token_id": token_id, "size": size, "price": price}
        else:
            return {"status": "killed", "token_id": token_id, "reason": "no_fill_at_limit"}

    # Real execution path
    # order = client.create_and_post_order(
    #     OrderArgs(token_id=token_id, price=price, size=size,
    #               side=BUY, order_type=OrderType.FOK)
    # )
    # return order
    raise NotImplementedError("Live trading disabled. Set PAPER_TRADING=False and wire py_clob_client.")


# ──────────────────────────────────────────────────────────────────────
# LAYER 2: REVERSAL PROTOCOL
# ──────────────────────────────────────────────────────────────────────

async def execute_reversal(filled_legs: List[Dict[str, Any]]):
    """
    Market-sell orphaned inventory. Crosses the bid to dump immediately.
    Expected loss: 1-3% of filled notional.
    """
    print(f"   🩸 REVERSAL: liquidating {len(filled_legs)} orphan legs")
    losses = 0.0
    for leg in filled_legs:
        # In real mode: aggressive IOC sell at best_bid - epsilon
        # Stubbed loss assumption: 2% of notional
        notional = leg["price"] * leg["size"]
        loss = notional * 0.02
        losses += loss
        print(f"     dumped {leg['size']:.0f} @ {leg['token_id'][:12]}... "
              f"(est loss ${loss:.2f})")
    print(f"   Total reversal cost: ${losses:.2f}")
    return losses


# ──────────────────────────────────────────────────────────────────────
# MASTER EXECUTION
# ──────────────────────────────────────────────────────────────────────

async def execute_dutch_arb(market_key: str, live_book: Dict[str, Dict[str, list]]):
    """Called by the WS sniper when sum(best_ask) < EXECUTION_THRESHOLD."""

    print(f"   ───────────────────────────────────────────")
    print(f"   EXECUTION: {market_key}")

    # ── LAYER 3: Pre-trigger size validation + sizing ──
    plan = compute_position_size(market_key, live_book)
    if not plan:
        return False

    print(f"   ✅ Layer 3 passed. Sized: ${plan['capital_deployed']:.2f} "
          f"(edge {plan['expected_edge_pct']:+.2f}%)")

    # Reserve cluster exposure BEFORE firing (rolled back on full failure)
    CURRENT_EXPOSURE[plan["cluster"]] += plan["capital_deployed"]

    # ── LAYER 1: Parallel FOK ──
    print(f"   🔫 Firing {len(plan['legs'])} FOK orders in parallel...")
    tasks = [
        fire_fok_limit(leg["token_id"], leg["price"], leg["size"])
        for leg in plan["legs"]
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    filled = []
    failed = 0
    for leg, res in zip(plan["legs"], results):
        if isinstance(res, Exception) or res.get("status") != "filled":
            failed += 1
            print(f"   ❌ {leg['name']}: {res}")
        else:
            filled.append(leg)
            print(f"   ✅ {leg['name']}: filled {leg['size']:.0f} @ {leg['price']:.4f}")

    # ── LAYER 2: Reversal if partial ──
    if failed > 0:
        if filled:
            reversal_loss = await execute_reversal(filled)
            # Roll back exposure (kept the loss on the books mentally)
            CURRENT_EXPOSURE[plan["cluster"]] -= plan["capital_deployed"]
            print(f"   ❌ ARB FAILED. Net loss ~${reversal_loss:.2f}")
        else:
            CURRENT_EXPOSURE[plan["cluster"]] -= plan["capital_deployed"]
            print(f"   🟡 All legs killed cleanly. No inventory exposure.")
        return False

    print(f"   🎯 ARB SECURED. ${plan['capital_deployed']:.2f} deployed across "
          f"{len(filled)} legs. Expected payout at resolution: "
          f"${plan['legs'][0]['size']:.0f} on the winner.")
    return True


# ──────────────────────────────────────────────────────────────────────
# DEPTH HISTORY UPDATE (called externally by sniper on each book event)
# ──────────────────────────────────────────────────────────────────────

def record_depth(token_id: str, top_depth: float):
    history = DEPTH_HISTORY.setdefault(token_id, [])
    history.append(top_depth)
    if len(history) > DEPTH_HISTORY_MAX:
        history.pop(0)


if __name__ == "__main__":
    print("This is a module. Run sniper.py to start the engine.")
    print(f"Paper trading: {PAPER_TRADING}")
    print(f"Total capital: ${TOTAL_CAPITAL_USD:,.2f}")
    print(f"Net edge threshold: {EDGE_THRESHOLD_NET}")
    print(f"Markets whitelisted: {len(TARGET_MARKETS)}")
