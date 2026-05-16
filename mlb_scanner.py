"""
DEPRECATED — 2026-04-15
-----------------------
This file implements naive N=2 ask-side Dutching on MLB moneylines, which is
the same dead strategy proven unworkable by the main sniper on 2026-04-15.
The 'edge' it flags is market-maker vig, not arbitrage. Do not run.

Replacement: mlb_edge_tracker.py  (model-vs-market Elo disagreement scan).
"""

import sys
sys.exit(
    "mlb_scanner.py is deprecated. "
    "Use mlb_edge_tracker.py instead."
)

# --- original code retained below for reference ---

import requests
import json

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"


def get_prices(token_ids):
    headers = {"Content-Type": "application/json"}
    payload = [{"token_id": tid, "side": "buy"} for tid in token_ids]
    resp = requests.post(f"{CLOB_BASE}/prices", json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def scan_mlb():
    print("Scanning Polymarket for MLB markets...\n")

    all_events = []
    for offset in range(0, 1000, 100):
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

    # Find MLB or baseball-tagged events with "vs" in title
    mlb_events = []
    for event in all_events:
        title = event.get("title", "").lower()
        tags = [t.get("label", "").lower() for t in event.get("tags", [])]

        is_baseball = "baseball" in tags or "mlb" in tags or "mlb" in title
        has_vs = " vs " in title or " vs. " in title

        if has_vs:
            mlb_events.append(event)  # grab all vs events since tags may not be baseball-specific

    if not mlb_events:
        # Show what sports ARE available
        print("No 'vs' events found. Checking what tags exist...\n")
        tag_counts = {}
        for event in all_events:
            for t in event.get("tags", []):
                label = t.get("label", "")
                tag_counts[label] = tag_counts.get(label, 0) + 1
        for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1])[:20]:
            print(f"  {tag}: {count}")
        return

    print(f"Found {len(mlb_events)} 'vs' events\n")

    opportunities = []

    for event in mlb_events:
        title = event.get("title", "")
        markets = event.get("markets", [])
        tags = [t.get("label", "") for t in event.get("tags", [])]

        for m in markets:
            clob_tokens = m.get("clobTokenIds", [])
            if isinstance(clob_tokens, str):
                clob_tokens = json.loads(clob_tokens)

            outcomes = m.get("outcomes", [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            prices_raw = m.get("outcomePrices", [])
            if isinstance(prices_raw, str):
                prices_raw = json.loads(prices_raw)

            if len(clob_tokens) < 2 or len(outcomes) < 2 or len(prices_raw) < 2:
                continue

            price_a = float(prices_raw[0])
            price_b = float(prices_raw[1])
            prob_sum = price_a + price_b

            edge = (1.0 - prob_sum) * 100 if prob_sum < 1.0 else 0
            overround = (prob_sum - 1.0) * 100 if prob_sum >= 1.0 else 0

            result = {
                "title": title,
                "tags": tags,
                "team_a": outcomes[0],
                "team_b": outcomes[1],
                "price_a": price_a,
                "price_b": price_b,
                "prob_sum": prob_sum,
                "edge": edge,
                "overround": overround,
                "token_a": clob_tokens[0],
                "token_b": clob_tokens[1],
            }

            if edge > 0:
                opportunities.append(result)

            print(f"{title}")
            print(f"  {outcomes[0]}: {price_a:.3f}  |  {outcomes[1]}: {price_b:.3f}  |  Sum: {prob_sum:.4f}", end="")
            if edge > 0:
                print(f"  >>> EDGE: {edge:.2f}%")
            else:
                print(f"  (overround: {overround:.2f}%)")

    print(f"\n--- RESULTS ---")
    print(f"Total matchups scanned: {len(mlb_events)}")
    print(f"Dutch opportunities (sum < 1.0): {len(opportunities)}")

    if opportunities:
        print(f"\nBest opportunities:")
        for opp in sorted(opportunities, key=lambda x: -x["edge"]):
            print(f"\n  {opp['title']}")
            print(f"    {opp['team_a']}: {opp['price_a']:.3f}")
            print(f"    {opp['team_b']}: {opp['price_b']:.3f}")
            print(f"    Edge: {opp['edge']:.2f}%")
            print(f"    Token A: {opp['token_a']}")
            print(f"    Token B: {opp['token_b']}")


if __name__ == "__main__":
    scan_mlb()
