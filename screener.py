"""
TIER 1: INSTITUTIONAL SCREENER (V2)
Filters:
  1. MECE-enforced (negRisk = True)
  2. 3+ legs grouped via groupItemTitle
  3. 24h volume > $10k (kills stale prices)
  4. Resolves in < 60 days (kills time-value mirages)
  5. Cached prob sum in [0.85, 1.00] (MECE-complete window)

Output: hot_list.json + hot_tokens.json — feeds the Tier 2 WebSocket sniper.
"""

import requests
import json
import time
from datetime import datetime, timezone

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Filter knobs
MIN_LEGS = 3
MIN_VOLUME_24H = 10_000
MAX_DAYS_TO_RESOLUTION = 60
MIN_CACHED_SUM = 0.85
MAX_CACHED_SUM = 1.00
PAGES_TO_SCAN = 30  # 30 * 100 = 3000 events


def fetch_all_events():
    all_events = []
    for offset in range(0, PAGES_TO_SCAN * 100, 100):
        params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}
        try:
            resp = requests.get(GAMMA_EVENTS_URL, params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_events.extend(batch)
        except Exception as e:
            print(f"  [WARN] Fetch error at offset {offset}: {e}")
            break
    return all_events


def parse_json_field(val):
    """Polymarket Gamma returns clobTokenIds and outcomePrices as JSON strings."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return []
    return val or []


def screen():
    print("=" * 72)
    print("TIER 1: INSTITUTIONAL SCREENER V2")
    print(f"Started: {datetime.now().isoformat()}")
    print("=" * 72)

    t0 = time.time()
    events = fetch_all_events()
    print(f"Fetched {len(events)} events in {time.time()-t0:.1f}s\n")

    # Funnel counters — see exactly which filter eliminates how many
    funnel = {
        "total": len(events),
        "min_legs_fail": 0,
        "not_mece": 0,
        "low_volume": 0,
        "no_end_date": 0,
        "expired_or_far": 0,
        "parse_fail": 0,
        "out_of_sum_range": 0,
        "passed": 0,
    }

    now = datetime.now(timezone.utc)
    hot_list = []

    for event in events:
        markets = event.get("markets", [])

        # 1. Min legs
        if len(markets) < MIN_LEGS:
            funnel["min_legs_fail"] += 1
            continue

        # 2. MECE filter (negRisk on event or any market)
        event_neg = event.get("negRisk") is True or event.get("neg_risk") is True
        market_neg = any(
            m.get("negRisk") is True or m.get("neg_risk") is True
            for m in markets
        )
        if not (event_neg or market_neg):
            funnel["not_mece"] += 1
            continue

        # 3. Liquidity — sum 24h volume across all legs
        event_vol_24h = sum(
            float(m.get("volume24hr") or 0) for m in markets
        )
        if event_vol_24h < MIN_VOLUME_24H:
            funnel["low_volume"] += 1
            continue

        # 4. Time to resolution
        end_date_str = event.get("endDate")
        if not end_date_str:
            funnel["no_end_date"] += 1
            continue
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            days_left = (end_date - now).days
        except (ValueError, AttributeError):
            funnel["no_end_date"] += 1
            continue
        if days_left < 0 or days_left > MAX_DAYS_TO_RESOLUTION:
            funnel["expired_or_far"] += 1
            continue

        # Parse legs
        outcomes = []
        cached_sum = 0.0
        parse_ok = True
        for m in markets:
            git = (m.get("groupItemTitle") or "").strip()
            if not git:
                continue

            tokens = parse_json_field(m.get("clobTokenIds"))
            prices = parse_json_field(m.get("outcomePrices"))
            if len(tokens) < 2 or len(prices) < 1:
                continue
            try:
                yes_price = float(prices[0])
            except (ValueError, TypeError):
                continue

            outcomes.append({
                "name": git,
                "yes_token": tokens[0],
                "no_token": tokens[1],
                "cached_yes_price": yes_price,
                "volume_24h": float(m.get("volume24hr") or 0),
            })
            cached_sum += yes_price

        if len(outcomes) < MIN_LEGS:
            funnel["parse_fail"] += 1
            continue

        # 5. MECE-completeness window
        if not (MIN_CACHED_SUM <= cached_sum <= MAX_CACHED_SUM):
            funnel["out_of_sum_range"] += 1
            continue

        funnel["passed"] += 1
        hot_list.append({
            "event_id": event.get("id"),
            "title": event.get("title", ""),
            "tags": [t.get("label", "") for t in event.get("tags", [])],
            "num_outcomes": len(outcomes),
            "cached_prob_sum": round(cached_sum, 4),
            "implied_edge_pct": round((1.0 - cached_sum) * 100, 2),
            "days_to_resolution": days_left,
            "volume_24h": round(event_vol_24h, 2),
            "outcomes": outcomes,
        })

    # Rank: lowest sum, then highest volume, then nearest resolution
    hot_list.sort(key=lambda x: (
        x["cached_prob_sum"],
        -x["volume_24h"],
        x["days_to_resolution"],
    ))

    elapsed = time.time() - t0

    print("=" * 72)
    print("FUNNEL")
    print("=" * 72)
    print(f"  Total events scanned:        {funnel['total']}")
    print(f"  Eliminated — <3 legs:        {funnel['min_legs_fail']}")
    print(f"  Eliminated — not MECE:       {funnel['not_mece']}")
    print(f"  Eliminated — vol24h < $10k:  {funnel['low_volume']}")
    print(f"  Eliminated — no end date:    {funnel['no_end_date']}")
    print(f"  Eliminated — >60d or stale:  {funnel['expired_or_far']}")
    print(f"  Eliminated — parse failed:   {funnel['parse_fail']}")
    print(f"  Eliminated — sum out of range: {funnel['out_of_sum_range']}")
    print(f"  PASSED:                      {funnel['passed']}")
    print(f"  Elapsed: {elapsed:.1f}s")

    if hot_list:
        print(f"\n{'='*72}\nINSTITUTIONAL HOT LIST\n{'='*72}")
        for i, m in enumerate(hot_list, 1):
            print(f"\n#{i}  [sum={m['cached_prob_sum']:.4f}] edge={m['implied_edge_pct']:+.2f}%  "
                  f"vol24h=${m['volume_24h']:,.0f}  resolves in {m['days_to_resolution']}d")
            print(f"    {m['title']}")
            preview = ", ".join(o["name"] for o in m["outcomes"][:5])
            tail = "..." if m["num_outcomes"] > 5 else ""
            print(f"    Legs ({m['num_outcomes']}): {preview}{tail}")
    else:
        print("\nNo markets passed all filters. Loosen MIN_VOLUME_24H or MAX_DAYS_TO_RESOLUTION.")

    # Outputs
    with open("hot_list.json", "w") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "filters": {
                "min_legs": MIN_LEGS,
                "min_vol_24h": MIN_VOLUME_24H,
                "max_days": MAX_DAYS_TO_RESOLUTION,
                "sum_range": [MIN_CACHED_SUM, MAX_CACHED_SUM],
            },
            "funnel": funnel,
            "hot_list": hot_list,
        }, f, indent=2)

    flat_tokens = [o["yes_token"] for m in hot_list for o in m["outcomes"]]
    with open("hot_tokens.json", "w") as f:
        json.dump(flat_tokens, f)

    print(f"\nWrote hot_list.json ({len(hot_list)} markets)")
    print(f"Wrote hot_tokens.json ({len(flat_tokens)} tokens for WebSocket)")


if __name__ == "__main__":
    screen()
