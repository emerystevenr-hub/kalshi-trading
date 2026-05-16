"""
Kalshi diagnostic v2 — uses correct API shape discovered from inspect script.

Strategy:
  1. Paginate /markets filtering out parlay tickers (KXMVE, KXMB prefixes)
  2. Use volume_24h_fp as real volume field
  3. Track price_level_structure + tick_size for precision distribution
  4. Report: universe, spread histogram, tick histogram, top markets
"""

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# v3b discovery filters — 2026-04-21 (aligned with shadow v5.5b).
# v3 ($25k) was the upstream bottleneck: shadow v5.5b lowered its runtime
# floor to $15k but the input candidate file filtered anything sub-$25k
# out first, capping eligible universe at 18 markets. Lowering here lets
# v5.5b's shadow floor actually do work.
#
# v3b admission criteria (all must hold):
#   MIN_VOL_24H        = $15k    (down from $25k; was $10k in v2)
#   MIN_SPREAD_DOLLARS = $0.025  (unchanged — break-even math requires it)
#   MIN_DTR_DAYS       = 0.1     (unchanged — intraday sports still admitted;
#                                 jump-risk handled by shadow's real-time gates)
MIN_VOL_24H = 15_000.0
MIN_SPREAD_DOLLARS = 0.025
MIN_DTR_DAYS = 0.1

PARLAY_PREFIXES = ("KXMVE", "KXMB", "KXMVECROSSCATEGORY", "KXMVESPORTS")


def is_parlay(ticker: str) -> bool:
    return any(ticker.startswith(p) for p in PARLAY_PREFIXES)


def paginate_markets():
    cursor = None
    page = 0
    while True:
        params = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{BASE}/markets", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        batch = data.get("markets", [])
        yield from batch
        cursor = data.get("cursor")
        page += 1
        if page % 5 == 0:
            print(f"  paged {page * 1000} markets...")
        if not cursor or not batch:
            return


def fp_to_float(v) -> float:
    """Kalshi returns fixed-point strings like '1234.56'. Convert safely."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def main():
    print("=" * 90)
    print(f"KALSHI DIAGNOSTIC v2  ({datetime.now().isoformat(timespec='seconds')})")
    print("=" * 90)

    now = datetime.now(timezone.utc)

    # Stats we want
    total_seen = 0
    total_parlay = 0
    total_primary = 0
    vol_seen = Counter()  # bucketed: 0, 1-100, 100-1k, 1k-10k, 10k+
    vol10k_primary = []   # list of primary markets with vol >= $10k
    tick_structures = Counter()

    print("\nPaginating /markets (filtering out parlays)...")
    for m in paginate_markets():
        total_seen += 1
        ticker = m.get("ticker", "")
        if is_parlay(ticker):
            total_parlay += 1
            continue
        total_primary += 1

        vol = fp_to_float(m.get("volume_24h_fp"))

        # Bucket
        if vol < 100:
            vol_seen["<$100"] += 1
        elif vol < 1000:
            vol_seen["$100-1k"] += 1
        elif vol < 10000:
            vol_seen["$1k-10k"] += 1
        else:
            vol_seen[">=$10k"] += 1

        pls = m.get("price_level_structure", "?")
        tick_structures[pls] += 1

        if vol < MIN_VOL_24H:
            continue

        # Check days-to-close
        close_ts = m.get("close_time")
        dtr = None
        if close_ts:
            try:
                dt = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
                dtr = (dt - now).total_seconds() / 86400.0
            except Exception:
                pass
        if dtr is not None and dtr < MIN_DTR_DAYS:
            continue

        yes_bid = fp_to_float(m.get("yes_bid_dollars"))
        yes_ask = fp_to_float(m.get("yes_ask_dollars"))
        spread = yes_ask - yes_bid if (yes_bid > 0 and yes_ask > 0) else 0
        mid = (yes_bid + yes_ask) / 2.0 if (yes_bid > 0 and yes_ask > 0) else 0
        tick_step = 0.01
        pr = m.get("price_ranges") or []
        if pr and isinstance(pr, list) and pr[0].get("step"):
            tick_step = fp_to_float(pr[0]["step"])
        spread_ticks = spread / tick_step if tick_step > 0 else 0

        # v3 absolute-spread filter: reject markets whose current spread
        # is below MIN_SPREAD_DOLLARS. Tick-agnostic — works on both
        # $0.01-tick and $0.001-tapered markets. Catches the Kalshi
        # failure mode where tapered markets with 3 ticks of spread
        # have $0.003 absolute — below break-even math.
        if spread > 0 and spread < MIN_SPREAD_DOLLARS:
            continue

        vol10k_primary.append({
            "ticker": ticker,
            "title": m.get("title", ""),
            "event_ticker": m.get("event_ticker", ""),
            "vol_24h": vol,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "spread": spread,
            "spread_ticks": spread_ticks,
            "mid": mid,
            "tick_step": tick_step,
            "price_structure": pls,
            "dtr": round(dtr, 1) if dtr else None,
        })

    print()
    print("=" * 90)
    print(f"Total markets seen:       {total_seen:,}")
    print(f"  parlay (filtered out):  {total_parlay:,}")
    print(f"  primary:                {total_primary:,}")
    print()
    print("VOLUME DISTRIBUTION (primary markets only, 24h):")
    for k in ["<$100", "$100-1k", "$1k-10k", ">=$10k"]:
        n = vol_seen.get(k, 0)
        pct = 100 * n / max(total_primary, 1)
        bar = "█" * int(pct / 2)
        print(f"  {k:>10s}  {n:>6,}  ({pct:>5.1f}%)  {bar}")
    print()
    print("PRICE STRUCTURE (primary markets):")
    for k, v in tick_structures.most_common():
        print(f"  {k:<20s}  {v:,}")
    print()
    print(f"CANDIDATES (vol >= ${MIN_VOL_24H/1000:.0f}k/day AND "
          f"spread >= ${MIN_SPREAD_DOLLARS:.3f} AND "
          f"dtr >= {MIN_DTR_DAYS}d):  {len(vol10k_primary)}")
    print()

    if not vol10k_primary:
        print("No primary markets meet the filter. Try relaxing MIN_VOL.")
        return

    # Spread distribution on candidates with live 2-sided books
    live = [m for m in vol10k_primary if m["yes_bid"] > 0 and m["yes_ask"] > 0 and m["yes_ask"] > m["yes_bid"]]
    print(f"2-SIDED BOOKS (out of {len(vol10k_primary)}):  {len(live)}")
    print()

    if live:
        spread_ticks_buckets = Counter()
        for m in live:
            t = m["spread_ticks"]
            if t < 1: spread_ticks_buckets["<1t"] += 1
            elif t < 2: spread_ticks_buckets["1t"] += 1
            elif t < 3: spread_ticks_buckets["2t"] += 1
            elif t < 5: spread_ticks_buckets["3-4t"] += 1
            elif t < 10: spread_ticks_buckets["5-9t"] += 1
            else: spread_ticks_buckets[">=10t"] += 1

        print("SPREAD DISTRIBUTION (in ticks):")
        for k in ["<1t", "1t", "2t", "3-4t", "5-9t", ">=10t"]:
            n = spread_ticks_buckets.get(k, 0)
            pct = 100 * n / max(len(live), 1)
            bar = "█" * int(pct / 2)
            print(f"  {k:>6s}  {n:>4}  ({pct:>5.1f}%)  {bar}")
        print()

        print("SURVIVORS AT EACH TICK THRESHOLD:")
        for thr in [1, 2, 3, 5, 10]:
            n = sum(1 for m in live if m["spread_ticks"] >= thr - 1e-9)
            print(f"  spread >= {thr}t:   {n}")
        print()

        print("TOP 25 CANDIDATES BY VOLUME (2-sided):")
        for m in sorted(live, key=lambda x: -x["vol_24h"])[:25]:
            print(f"  ${m['vol_24h']:>10,.0f}/d  bid={m['yes_bid']:.4f}  ask={m['yes_ask']:.4f}  "
                  f"sp={m['spread_ticks']:>4.1f}t  tick={m['tick_step']:.4f}  "
                  f"dtr={m['dtr'] or '?':>5}  {m['title'][:45]}")
        print()

        print("TOP 15 WIDEST SPREADS (best MM edge, 2-sided, >= $10k/d):")
        for m in sorted(live, key=lambda x: -x["spread_ticks"])[:15]:
            print(f"  {m['spread_ticks']:>4.1f}t (${m['spread']:.4f})  vol=${m['vol_24h']:>9,.0f}/d  "
                  f"tick={m['tick_step']:.4f}  {m['title'][:55]}")

    # Save
    with open("kalshi_candidates_v2.json", "w") as f:
        json.dump(vol10k_primary, f, indent=2)
    print(f"\nwrote kalshi_candidates_v2.json ({len(vol10k_primary)} markets)")


if __name__ == "__main__":
    main()
