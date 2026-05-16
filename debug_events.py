import requests
import json

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

all_events = []
for offset in [0, 100, 200, 300, 400]:
    params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}
    resp = requests.get(GAMMA_EVENTS_URL, params=params, timeout=15)
    batch = resp.json()
    if not batch:
        break
    all_events.extend(batch)

print(f"Total events: {len(all_events)}\n")

for event in all_events:
    title = event.get("title", "")
    if " vs " not in title and " vs. " not in title:
        continue

    print(f"=== {title} ===")
    print(f"  Event ID: {event.get('id')}")
    print(f"  Tags: {event.get('tags', [])}")
    markets = event.get("markets", [])
    print(f"  Market count: {len(markets)}")

    for i, m in enumerate(markets):
        print(f"\n  Market {i}:")
        print(f"    question: {m.get('question', '')}")
        print(f"    groupItemTitle: {m.get('groupItemTitle', '')}")
        print(f"    clobTokenIds type: {type(m.get('clobTokenIds'))}")
        print(f"    clobTokenIds: {m.get('clobTokenIds', [])}")
        print(f"    outcomes: {m.get('outcomes', [])}")
        print(f"    outcomePrices: {m.get('outcomePrices', [])}")

    print("\n" + "="*60 + "\n")
