"""
One-shot debug: dump the raw structure of every MLB-tagged Polymarket event
so we can see exactly how moneylines are represented. Run once, then delete.
"""
import json
import requests

GAMMA_URL = "https://gamma-api.polymarket.com/events"


def main():
    events = []
    for offset in range(0, 1500, 100):
        r = requests.get(GAMMA_URL, params={
            "active": "true", "closed": "false", "limit": 100, "offset": offset,
        }, timeout=15)
        r.raise_for_status()
        b = r.json()
        if not b:
            break
        events.extend(b)

    mlb = []
    for e in events:
        title = e.get("title", "").lower()
        tags = [t.get("label", "").lower() for t in e.get("tags", [])]
        if "baseball" in tags or "mlb" in tags or "mlb" in title:
            mlb.append(e)

    print(f"fetched {len(events)} events, {len(mlb)} mlb-tagged\n")

    for i, e in enumerate(mlb):
        print("=" * 78)
        print(f"[{i}] {e.get('title')}")
        print(f"    id={e.get('id')}  slug={e.get('slug')}")
        print(f"    tags={[t.get('label') for t in e.get('tags',[])]}")
        markets = e.get("markets", [])
        print(f"    markets: {len(markets)}")
        for j, m in enumerate(markets):
            q = m.get("question") or m.get("title")
            outcomes = m.get("outcomes")
            tokens = m.get("clobTokenIds")
            prices = m.get("outcomePrices")
            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except: pass
            if isinstance(tokens, str):
                try: tokens = json.loads(tokens)
                except: pass
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: pass
            print(f"      [{j}] q={q}")
            print(f"          outcomes={outcomes}")
            print(f"          prices={prices}")
            print(f"          tokens={tokens}")
        if i >= 4:  # cap at first 5 events
            print("\n(truncated after 5 events)")
            break


if __name__ == "__main__":
    main()
