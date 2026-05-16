"""
Kalshi diagnostic — same filters we used on Polymarket.

Public API, no auth required for market data. Reports:
  - Universe size with $10k+/day volume, open, 7+ days to close
  - Spread distribution in cents
  - Tick size per market
  - Top-volume markets for comparison with Polymarket

If this shows a viable universe + active maker rewards, we pivot.
"""

import json
import time
from collections import Counter
from datetime import datetime, timezone
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Try both API hosts — Kalshi moved from trading-api to api.elections.kalshi.com
BASES = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
]

def get_working_base():
    for b in BASES:
        try:
            r = requests.get(f"{b}/markets", params={"limit": 1, "status": "open"}, timeout=10)
            if r.status_code == 200:
                print(f"using API base: {b}")
                return b
        except Exception as e:
            print(f"  {b} failed: {e}")
    raise RuntimeError("No working Kalshi API endpoint")


def fetch_all_markets(base):
    markets = []
    cursor = None
    while True:
        params = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{base}/markets", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        batch = data.get("markets", [])
        if not batch:
            break
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor:
            break
        print(f"  fetched {len(markets)}...")
    return markets


def fetch_orderbook(base, ticker):
    try:
        r = requests.get(f"{base}/markets/{ticker}/orderbook", timeout=10)
        r.raise_for_status()
        return r.json().get("orderbook", {})
    except Exception:
        return None


def main():
    print("=" * 90)
    print(f"KALSHI DIAGNOSTIC  ({datetime.now().isoformat(timespec='seconds')})")
    print("=" * 90)

    base = get_working_base()

    print("\nfetching all open markets...")
    markets = fetch_all_markets(base)
    print(f"total open markets: {len(markets)}")

    # Stage: volume filter + days to close
    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        # Kalshi uses 'volume_24h' or similar — check both
        vol = m.get("volume_24h") or m.get("dollar_volume_24h") or 0
        # Some APIs return 'liquidity'; don't rely on it alone
        close_ts = m.get("close_time") or m.get("expiration_time")
        dtr = None
        if close_ts:
            try:
                dt = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
                dtr = (dt - now).total_seconds() / 86400.0
            except Exception:
                pass
        if dtr is not None and dtr < 7.0:
            continue
        if float(vol) < 10000:
            continue
        candidates.append(m)

    print(f"\ncandidates (vol >= $10k/day, dtr >= 7d): {len(candidates)}")

    if not candidates:
        # Show what the top markets look like regardless, so we see what Kalshi has
        print("\nNo markets met the $10k filter. Showing top 30 by volume (any dtr):")
        all_by_vol = sorted(markets, key=lambda m: float(m.get("volume_24h") or m.get("dollar_volume_24h") or 0), reverse=True)[:30]
        for m in all_by_vol:
            vol = float(m.get("volume_24h") or m.get("dollar_volume_24h") or 0)
            print(f"  ${vol:>10,.0f}/d  {m.get('ticker', '?'):<25s}  {m.get('title', '')[:70]}")
        return

    print("\nfetching orderbooks (0.5s/req)...")
    tick_counts = Counter()
    spread_buckets = Counter()
    live_markets = []

    for i, m in enumerate(candidates[:100], 1):  # cap at 100 for diagnostic speed
        if i % 20 == 0:
            print(f"  {i}/{min(len(candidates), 100)}")
        ob = fetch_orderbook(base, m["ticker"])
        time.sleep(0.5)
        if not ob:
            continue
        yes_bids = ob.get("yes") or []
        yes_asks = ob.get("no") or []  # On Kalshi, 'no' side is the ask-side for YES; may vary
        # Actually Kalshi returns separate yes and no sides. The "ask" for YES is (100 - no_bid).
        # Let's use both representations and show what we see.
        if not yes_bids or not yes_asks:
            continue
        best_yes_bid = max(int(x[0]) for x in yes_bids) if yes_bids else 0
        best_no_bid = max(int(x[0]) for x in yes_asks) if yes_asks else 0
        # Kalshi prices in cents (0-100). YES best ask = 100 - best no bid
        best_yes_ask = 100 - best_no_bid if best_no_bid > 0 else 0
        if best_yes_bid <= 0 or best_yes_ask <= 0 or best_yes_ask <= best_yes_bid:
            continue
        spread_c = best_yes_ask - best_yes_bid
        mid_c = (best_yes_bid + best_yes_ask) / 2.0
        if mid_c < 5 or mid_c > 95:
            continue
        # Kalshi tick is typically 1 cent (integer prices)
        tick_c = 1
        tick_counts[tick_c] += 1
        # Bucket spreads
        if spread_c <= 1:
            spread_buckets["1c"] += 1
        elif spread_c == 2:
            spread_buckets["2c"] += 1
        elif spread_c == 3:
            spread_buckets["3c"] += 1
        elif spread_c <= 5:
            spread_buckets["4-5c"] += 1
        elif spread_c <= 10:
            spread_buckets["6-10c"] += 1
        else:
            spread_buckets[">10c"] += 1
        live_markets.append({
            "ticker": m["ticker"],
            "title": m.get("title", ""),
            "vol_24h": float(m.get("volume_24h") or m.get("dollar_volume_24h") or 0),
            "yes_bid": best_yes_bid,
            "yes_ask": best_yes_ask,
            "spread_c": spread_c,
        })

    print()
    print("=" * 90)
    print(f"sampled:           {min(len(candidates), 100)}")
    print(f"live 2-sided:      {len(live_markets)}")
    print()
    print("SPREAD DISTRIBUTION (in cents):")
    for k in ["1c", "2c", "3c", "4-5c", "6-10c", ">10c"]:
        n = spread_buckets.get(k, 0)
        pct = 100 * n / max(len(live_markets), 1)
        bar = "█" * int(pct / 2)
        print(f"  {k:>6s}  {n:>4}  ({pct:>5.1f}%)  {bar}")
    print()
    print("SURVIVORS AT EACH SPREAD THRESHOLD:")
    for thr in [1, 2, 3, 4, 5]:
        n = sum(1 for m in live_markets if m["spread_c"] >= thr)
        print(f"  spread >= {thr}c:   {n}")
    print()
    print("TOP 20 BY VOLUME:")
    for m in sorted(live_markets, key=lambda x: -x["vol_24h"])[:20]:
        print(f"  ${m['vol_24h']:>10,.0f}/d  bid={m['yes_bid']:>3d}c  ask={m['yes_ask']:>3d}c  sp={m['spread_c']:>2d}c  {m['title'][:55]}")
    print()
    print("TOP 10 WIDEST SPREADS (most MM-viable):")
    for m in sorted(live_markets, key=lambda x: -x["spread_c"])[:10]:
        print(f"  spread={m['spread_c']:>2d}c  vol=${m['vol_24h']:>9,.0f}/d  {m['title'][:60]}")

    # Save for analysis
    with open("kalshi_candidates.json", "w") as f:
        json.dump(live_markets, f, indent=2)
    print(f"\nwrote kalshi_candidates.json")


if __name__ == "__main__":
    main()
