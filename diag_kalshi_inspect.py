"""
Inspect Kalshi API response structure so we know the real field names
and can filter out parlay/combo products (KXMV prefix).
"""

import json
import requests
from collections import Counter

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Pull a batch and dump the first market's keys + values
r = requests.get(f"{BASE}/markets", params={"limit": 50, "status": "open"}, timeout=15)
data = r.json()
markets = data.get("markets", [])

print(f"status: {r.status_code}")
print(f"batch size: {len(markets)}")
print()

if markets:
    print("=== FIRST MARKET — all fields ===")
    for k, v in markets[0].items():
        vs = json.dumps(v)[:80]
        print(f"  {k:<30s}  {vs}")
    print()

# Count ticker prefix types so we know what's a parlay vs. primary
prefixes = Counter()
for m in markets:
    t = m.get("ticker", "")
    # First letter sequence
    import re
    pfx = re.match(r"^([A-Z]+)", t)
    prefixes[pfx.group(1) if pfx else "?"] += 1
print("=== TICKER PREFIX DISTRIBUTION (first batch) ===")
for p, c in prefixes.most_common():
    print(f"  {p:<20s}  {c}")
print()

# Look for volume-related fields
print("=== VOLUME-LIKE FIELDS PRESENT ===")
vol_keys = set()
for m in markets:
    for k in m.keys():
        lk = k.lower()
        if "volume" in lk or "vol" in lk or "liquid" in lk or "oi" in lk or "dollar" in lk:
            vol_keys.add(k)
print(f"  {sorted(vol_keys)}")
print()

# Show non-parlay top markets by any volume field we find
print("=== SAMPLE NON-PARLAY MARKETS (any ticker NOT starting with KXMV or KXMB) ===")
n = 0
for m in markets:
    t = m.get("ticker", "")
    if t.startswith("KXMV") or t.startswith("KXMB"):
        continue
    print(f"  ticker={t}")
    print(f"    title: {m.get('title', '')[:80]}")
    for k in vol_keys:
        print(f"    {k}: {m.get(k)}")
    print(f"    status: {m.get('status')}  close: {m.get('close_time')}")
    n += 1
    if n >= 5:
        break

# Also try the /events endpoint which groups markets into events
print("\n=== /events ENDPOINT ===")
r2 = requests.get(f"{BASE}/events", params={"limit": 20, "status": "open"}, timeout=15)
if r2.status_code == 200:
    events = r2.json().get("events", [])
    print(f"  status: {r2.status_code}, events: {len(events)}")
    if events:
        print(f"  first event keys: {sorted(events[0].keys())}")
        for e in events[:5]:
            print(f"    event={e.get('event_ticker')}  title={e.get('title', '')[:60]}")
else:
    print(f"  status: {r2.status_code}  body: {r2.text[:200]}")
