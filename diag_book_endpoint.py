"""
Diagnostic: is 'no book' in curator v4 output real or an artifact of concurrency/rate limits?

Pulls the first 30 negRisk market tokens from Gamma, fetches each book
single-threaded with 500ms spacing, reports whether each has live quotes.

If most show bids=0 asks=0, then Gamma's volume_24h is stale — markets
reported as $10k+/day that actually have no current liquidity. Real signal.

If most show bids>0 asks>0, then the curator's concurrent fetch is hitting
rate limits. Fix by lowering CONCURRENCY in mm_event_curator_v4.py.
"""

import json
import time
import requests

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB = "https://clob.polymarket.com/book"

r = requests.get(GAMMA, params={"active": "true", "closed": "false", "limit": 200}, timeout=20)
events = r.json()

checked = 0
live = 0
empty = 0
for ev in events:
    if checked >= 30:
        break
    if not ev.get("negRisk"):
        continue
    for m in ev.get("markets", []):
        if checked >= 30:
            break
        if m.get("closed") or not m.get("active", True):
            continue
        vol = float(m.get("volume24hr") or 0)
        if vol < 10000:
            continue
        tokens = m.get("clobTokenIds")
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = None
        if not tokens or len(tokens) < 2:
            continue

        try:
            b = requests.get(CLOB, params={"token_id": tokens[0]}, timeout=15).json()
            n_bids = len([x for x in (b.get("bids") or []) if float(x.get("size", 0)) > 0])
            n_asks = len([x for x in (b.get("asks") or []) if float(x.get("size", 0)) > 0 and float(x.get("price", 0)) > 0])
        except Exception as e:
            n_bids = n_asks = -1
            print(f"  ERROR: {e}")

        status = "LIVE " if (n_bids > 0 and n_asks > 0) else "EMPTY"
        if n_bids > 0 and n_asks > 0:
            live += 1
        else:
            empty += 1
        print(f"  {status}  vol24h=${vol:>10,.0f}  bids={n_bids:<3}  asks={n_asks:<3}  {(m.get('question') or '')[:55]}")
        checked += 1
        time.sleep(0.5)

print()
print(f"checked: {checked}   live: {live}   empty: {empty}")
print()
if empty > checked * 0.5:
    print("VERDICT: >50% of $10k+ markets have empty books.")
    print("         Gamma volume_24h is stale. Real tradeable universe is smaller")
    print("         than volume data suggests. Curator 'no book' drops are REAL.")
else:
    print("VERDICT: Most markets have live books when fetched single-threaded.")
    print("         Curator's CONCURRENCY=30 is hitting rate limits.")
    print("         Lower CONCURRENCY to 5-10 and re-run curator v4 to recover them.")
