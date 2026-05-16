"""Tiny diagnostic — hit Kalshi /orderbook for one known market and print
the raw response. Tells us exactly why the depth script got zeros."""

import sys
import json
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Try a known-active macro market (any YES/NO market from KXCPIYOY-26APR should exist)
candidates = [
    "KXCPIYOY-26APR-T3.0",
    "KXCPIYOY-26APR-T3.5",
    "KXCPIYOY-26APR-T2.5",
    "KXCPI-26APR-T0.3",
    "KXFED-26JUN-T4.25",
]

for ticker in candidates:
    print(f"\n=== {ticker} ===")
    # First get the market itself so we have something to compare
    r = requests.get(f"{BASE}/markets/{ticker}", timeout=15)
    print(f"  /markets/{ticker}  →  HTTP {r.status_code}")
    if r.status_code == 200:
        m = r.json().get("market", {})
        print(f"    yes_bid={m.get('yes_bid')}  yes_ask={m.get('yes_ask')}  "
              f"no_bid={m.get('no_bid')}  no_ask={m.get('no_ask')}")
        print(f"    volume={m.get('volume')}  open_interest={m.get('open_interest')}")
    # Now the orderbook
    r2 = requests.get(f"{BASE}/markets/{ticker}/orderbook", timeout=15)
    print(f"  /markets/{ticker}/orderbook  →  HTTP {r2.status_code}")
    body = r2.text
    print(f"  body[:500]: {body[:500]}")
    if r2.status_code == 200:
        try:
            parsed = r2.json()
            print(f"  keys: {list(parsed.keys())}")
            ob = parsed.get("orderbook")
            print(f"  orderbook type: {type(ob).__name__}")
            if isinstance(ob, dict):
                print(f"  orderbook keys: {list(ob.keys())}")
                for side in ob:
                    print(f"    {side}: {ob[side][:3] if isinstance(ob[side], list) else ob[side]}")
        except Exception as e:
            print(f"  json parse err: {e}")
    if r2.status_code == 200 and r2.json().get("orderbook"):
        sys.exit(0)
