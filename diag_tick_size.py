"""
Diagnostic: what tick size do Polymarket's high-volume markets actually use?

If the answer is 0.001 (one-tenth cent), then the entire 'spreads are sub-penny'
problem is just our SDK config — we need to quote at 0.001 precision to compete.

Polymarket exposes tick_size per market via the /markets endpoint and embedded
in the book response.
"""

import json
import time
import requests

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com/events"

# Use the markets we already saw with sub-penny spreads
events = requests.get(GAMMA, params={"active":"true","closed":"false","limit":100}, timeout=20).json()

checked = 0
tick_counts = {}
for ev in events:
    if checked >= 25: break
    if not ev.get("negRisk"): continue
    for m in ev.get("markets", []):
        if checked >= 25: break
        if m.get("closed"): continue
        vol = float(m.get("volume24hr") or 0)
        if vol < 10000: continue
        tokens = m.get("clobTokenIds")
        if isinstance(tokens, str):
            try: tokens = json.loads(tokens)
            except: continue
        if not tokens: continue
        token = tokens[0]

        # Try /markets endpoint to get tick_size
        try:
            r = requests.get(f"{CLOB}/markets/{m.get('conditionId') or m.get('id')}", timeout=10)
            mkt_data = r.json() if r.status_code == 200 else {}
        except Exception:
            mkt_data = {}

        # Also check book response for tick info
        try:
            b = requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=10).json()
        except Exception:
            b = {}

        tick = mkt_data.get("minimum_tick_size") or mkt_data.get("tick_size") or b.get("tick_size")
        min_size = mkt_data.get("minimum_order_size") or b.get("minimum_order_size")

        # Inspect actual bid/ask prices to infer tick precision
        bids = [float(x["price"]) for x in (b.get("bids") or []) if float(x.get("size", 0)) > 0]
        asks = [float(x["price"]) for x in (b.get("asks") or []) if float(x.get("size", 0)) > 0]
        sample_prices = (bids[:3] + asks[:3])

        print(f"vol=${vol:>9,.0f}  tick={tick}  min_sz={min_size}  prices_sample={sample_prices}  {(m.get('question') or '')[:45]}")
        tick_counts[str(tick)] = tick_counts.get(str(tick), 0) + 1
        checked += 1
        time.sleep(0.5)

print()
print("TICK SIZE DISTRIBUTION:", tick_counts)
print()
if "0.001" in tick_counts and tick_counts["0.001"] > 0:
    print("CONFIRMED: Many markets use 0.001 tick. Your live_mm.py with TICK=0.01")
    print("           is hardcoded WRONG for these markets. Pros quote at 0.001 and")
    print("           you literally cannot get in front of them at 0.01 precision.")
    print("           Fix: read tick_size per market, use it in OrderArgs.")
elif "0.01" in tick_counts and tick_counts["0.01"] > 0:
    print("All markets use 0.01 tick. Sub-penny spreads must come from another mechanism.")
    print("Need deeper investigation.")
else:
    print("Tick size data missing — check API field names in /markets response.")
