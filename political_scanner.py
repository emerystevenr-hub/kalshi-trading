import requests
import json
import time

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"


def get_live_prices(token_ids):
    """Fetch live buy prices from CLOB API for accuracy over cached Gamma prices."""
    headers = {"Content-Type": "application/json"}
    payload = [{"token_id": tid, "side": "buy"} for tid in token_ids]
    resp = requests.post(f"{CLOB_BASE}/prices", json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def scan_nway_markets():
    print("Scanning Polymarket for N-way markets (3+ outcomes)...\n")

    all_events = []
    for offset in range(0, 2000, 100):
        params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}
        try:
            resp = requests.get(GAMMA_EVENTS_URL, params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_events.extend(batch)
        except Exception as e:
            print(f"  Fetch error at offset {offset}: {e}")
            break

    print(f"Total events fetched: {len(all_events)}\n")

    opportunities = []
    all_nway = []

    for event in all_events:
        title = event.get("title", "")
        markets = event.get("markets", [])
        tags = [t.get("label", "") for t in event.get("tags", [])]

        # N-way detection: multiple markets with groupItemTitle = separate outcomes
        grouped = {}
        for m in markets:
            git = m.get("groupItemTitle", "").strip()
            if not git:
                continue

            clob_tokens = m.get("clobTokenIds", [])
            if isinstance(clob_tokens, str):
                clob_tokens = json.loads(clob_tokens)

            prices_raw = m.get("outcomePrices", [])
            if isinstance(prices_raw, str):
                prices_raw = json.loads(prices_raw)

            if not clob_tokens or len(clob_tokens) < 1:
                continue

            # YES token is index 0, YES price is index 0
            yes_token = clob_tokens[0]
            yes_price = float(prices_raw[0]) if prices_raw else 0.0

            grouped[git] = {
                "name": git,
                "yes_token": yes_token,
                "yes_price": yes_price,
            }

        # Only care about 3+ outcomes
        if len(grouped) < 3:
            continue

        # Calculate implied probability sum from Gamma cached prices
        gamma_sum = sum(o["yes_price"] for o in grouped.values())

        market_data = {
            "title": title,
            "event_id": event.get("id"),
            "tags": tags,
            "num_outcomes": len(grouped),
            "outcomes": grouped,
            "gamma_prob_sum": gamma_sum,
        }
        all_nway.append(market_data)

    print(f"N-way markets found (3+ outcomes): {len(all_nway)}\n")

    if not all_nway:
        print("No multi-outcome markets found.")
        return

    # Sort by gamma_prob_sum ascending (lowest = most likely edge)
    all_nway.sort(key=lambda x: x["gamma_prob_sum"])

    # Now fetch LIVE prices from CLOB for the top candidates
    print("=" * 70)
    print("SCANNING FOR DUTCH OPPORTUNITIES (live CLOB prices)")
    print("=" * 70)

    for market in all_nway:
        title = market["title"]
        outcomes = market["outcomes"]
        n = market["num_outcomes"]
        gamma_sum = market["gamma_prob_sum"]

        # Fetch live prices
        token_ids = [o["yes_token"] for o in outcomes.values()]
        try:
            live_prices = get_live_prices(token_ids)
            time.sleep(0.1)  # rate limit courtesy
        except Exception as e:
            print(f"\n{title}")
            print(f"  CLOB error: {e}")
            continue

        # Map live prices back to outcomes
        live_sum = 0.0
        outcome_details = []
        for name, data in outcomes.items():
            tid = data["yes_token"]
            raw = live_prices.get(tid)
            # CLOB returns nested: {token_id: {"buy": "0.42", "sell": "0.43"}}
            if isinstance(raw, dict):
                live_p = float(raw.get("buy") or raw.get("BUY") or data["yes_price"])
            elif raw is not None:
                live_p = float(raw)
            else:
                live_p = float(data["yes_price"])
            live_sum += live_p
            outcome_details.append({
                "name": name,
                "price": live_p,
                "token": data["yes_token"],
            })

        edge = (1.0 - live_sum) * 100 if live_sum < 1.0 else 0
        overround = (live_sum - 1.0) * 100 if live_sum >= 1.0 else 0

        print(f"\n{'='*60}")
        print(f"{title}")
        print(f"  Tags: {', '.join(market['tags'])}")
        print(f"  Outcomes: {n}")
        print(f"  Gamma sum: {gamma_sum:.4f}  |  LIVE sum: {live_sum:.4f}")

        if edge > 0:
            print(f"  >>> DUTCH EDGE: {edge:.2f}% <<<")

            # Show optimal Dutch allocation
            print(f"\n  Optimal Dutch allocation (equal profit):")
            total_inverse = sum(1.0 / o["price"] for o in outcome_details if o["price"] > 0)
            for o in sorted(outcome_details, key=lambda x: -x["price"]):
                if o["price"] > 0:
                    weight = (1.0 / o["price"]) / total_inverse * 100
                    implied = o["price"] * 100
                    print(f"    {o['name']}: ${o['price']:.3f} (implied {implied:.1f}%) — allocate {weight:.1f}%")
                    print(f"      Token: {o['token']}")

            opportunities.append({
                "title": title,
                "edge": edge,
                "live_sum": live_sum,
                "outcomes": outcome_details,
            })
        else:
            print(f"  Overround: {overround:.2f}%")
            # Still show top 3 prices for context
            sorted_outcomes = sorted(outcome_details, key=lambda x: -x["price"])[:3]
            for o in sorted_outcomes:
                print(f"    {o['name']}: {o['price']:.3f}")

    # Final summary
    print(f"\n{'='*70}")
    print(f"SUMMARY")
    print(f"{'='*70}")
    print(f"Total events scanned: {len(all_events)}")
    print(f"N-way markets (3+ outcomes): {len(all_nway)}")
    print(f"Dutch opportunities (live sum < 1.0): {len(opportunities)}")

    if opportunities:
        print(f"\nRANKED OPPORTUNITIES:")
        for i, opp in enumerate(sorted(opportunities, key=lambda x: -x["edge"]), 1):
            print(f"\n  #{i}: {opp['title']}")
            print(f"      Edge: {opp['edge']:.2f}%  |  Prob sum: {opp['live_sum']:.4f}")
    else:
        print(f"\nNo live Dutch edges found at this moment.")
        print(f"Closest markets to edge (lowest overround):")
        closest = sorted(all_nway, key=lambda x: x["gamma_prob_sum"])[:5]
        for m in closest:
            print(f"  {m['title']}: sum={m['gamma_prob_sum']:.4f} ({m['num_outcomes']} outcomes)")


if __name__ == "__main__":
    scan_nway_markets()
