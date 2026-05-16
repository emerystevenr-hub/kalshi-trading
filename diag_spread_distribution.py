"""
Diagnostic: spread + book distribution across all $10k+/day negRisk markets.

Fetches every volume-qualified market single-threaded (no rate limit issues)
and reports a histogram of spreads + which markets are book-empty.

This tells us definitively:
  - How many markets have real books (vs. Gamma stale volume)
  - Spread distribution on live markets (1c, 2c, 3c, 4c+)
  - Whether MM is viable at any spread threshold we'd accept

Runtime: ~8-10 minutes for ~470 markets at 1 req/sec.
"""

import json
import time
from collections import Counter
from typing import List

import requests

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB = "https://clob.polymarket.com/book"

MIN_VOL = 10_000.0
MIN_DTR_DAYS = 7.0


def fetch_all_events():
    events = []
    for offset in range(0, 5000, 100):
        try:
            r = requests.get(GAMMA, params={
                "active": "true", "closed": "false",
                "limit": 100, "offset": offset,
            }, timeout=20)
            r.raise_for_status()
            b = r.json()
            if not b:
                break
            events.extend(b)
        except Exception as e:
            print(f"  fetch error offset {offset}: {e}")
            break
    return events


def candidates():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    out = []  # (ev_title, market_question, vol24h, token, days_to_res)
    for ev in fetch_all_events():
        if not ev.get("negRisk"):
            continue
        end = ev.get("endDate") or ev.get("end_date")
        dtr = None
        if end:
            try:
                dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                dtr = (dt - now).total_seconds() / 86400.0
            except Exception:
                pass
        if dtr is not None and dtr < MIN_DTR_DAYS:
            continue
        for m in ev.get("markets", []):
            if m.get("closed") or not m.get("active", True):
                continue
            vol = float(m.get("volume24hr") or 0)
            if vol < MIN_VOL:
                continue
            tokens = m.get("clobTokenIds")
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except Exception:
                    tokens = None
            if not tokens or len(tokens) < 2:
                continue
            out.append({
                "ev_title": ev.get("title") or "",
                "question": m.get("question") or "",
                "vol24h": vol,
                "token": tokens[0],
                "dtr": dtr,
            })
    return out


def fetch_book(token: str):
    try:
        r = requests.get(CLOB, params={"token_id": token}, timeout=15)
        r.raise_for_status()
        b = r.json()
    except Exception as e:
        return {"error": str(e), "bids": [], "asks": []}
    return {
        "bids": [x for x in (b.get("bids") or []) if float(x.get("size", 0)) > 0],
        "asks": [x for x in (b.get("asks") or []) if float(x.get("size", 0)) > 0 and float(x.get("price", 0)) > 0],
    }


def main():
    print("Fetching candidate markets...")
    cand = candidates()
    print(f"  {len(cand)} markets qualified (vol >= ${MIN_VOL:,.0f}/d, dtr >= {MIN_DTR_DAYS:.0f}d)")
    print()
    print("Fetching books single-threaded (0.7s/req, ~expected runtime:", f"{len(cand) * 0.7 / 60:.1f} min)...")
    print()

    spread_buckets = Counter()
    errors = 0
    empty_books = 0
    one_sided = 0
    live = []

    for i, m in enumerate(cand, 1):
        if i % 50 == 0:
            print(f"  {i}/{len(cand)}  spreads so far: {dict(spread_buckets)}")
        bk = fetch_book(m["token"])
        if "error" in bk:
            errors += 1
            continue
        bids, asks = bk["bids"], bk["asks"]
        if not bids and not asks:
            empty_books += 1
            continue
        if not bids or not asks:
            one_sided += 1
            continue
        bb = max(float(x["price"]) for x in bids)
        ba = min(float(x["price"]) for x in asks)
        spread = ba - bb
        # Bucket by cents
        sp_c = int(round(spread * 100))
        if sp_c < 1:
            spread_buckets["<1c"] += 1
        elif sp_c == 1:
            spread_buckets["1c"] += 1
        elif sp_c == 2:
            spread_buckets["2c"] += 1
        elif sp_c == 3:
            spread_buckets["3c"] += 1
        elif sp_c <= 5:
            spread_buckets["4-5c"] += 1
        elif sp_c <= 10:
            spread_buckets["6-10c"] += 1
        else:
            spread_buckets[">10c"] += 1
        live.append({**m, "bid": bb, "ask": ba, "spread": spread})
        time.sleep(0.7)

    print()
    print("=" * 80)
    print(f"TOTAL sampled:       {len(cand)}")
    print(f"  errors:            {errors}")
    print(f"  empty books:       {empty_books}")
    print(f"  one-sided books:   {one_sided}")
    print(f"  live (2-sided):    {len(live)}")
    print()
    print("SPREAD DISTRIBUTION (on 2-sided books):")
    order = ["<1c", "1c", "2c", "3c", "4-5c", "6-10c", ">10c"]
    for k in order:
        n = spread_buckets.get(k, 0)
        pct = 100 * n / max(len(live), 1)
        bar = "█" * int(pct / 2)
        print(f"  {k:>6s}  {n:>4}  ({pct:>5.1f}%)  {bar}")
    print()

    # Show top 20 widest-spread markets
    print("TOP 20 WIDEST-SPREAD MARKETS (most MM-viable):")
    for m in sorted(live, key=lambda x: -x["spread"])[:20]:
        print(f"  spread={m['spread']*100:4.1f}c  vol=${m['vol24h']:>9,.0f}/d  "
              f"bid={m['bid']:.3f}  ask={m['ask']:.3f}  dtr={m['dtr']:>5.1f}d  "
              f"{(m['question'] or '')[:50]}")
    print()

    # How many markets survive at each spread threshold
    print("SURVIVORS AT EACH SPREAD THRESHOLD:")
    for thr_c in [1, 2, 3, 4, 5]:
        n = sum(1 for m in live if m["spread"] * 100 >= thr_c - 0.5)
        print(f"  spread >= {thr_c}c:   {n} markets")
    print()

    # Save live market details to JSON for further analysis
    with open("diag_spread_live.json", "w") as f:
        json.dump(live, f, indent=2)
    print("Saved detailed per-market data to diag_spread_live.json")


if __name__ == "__main__":
    main()
