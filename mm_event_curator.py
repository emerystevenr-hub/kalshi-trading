"""
MM EVENT CURATOR — v3 (Volume-first)

v3 changes:
  - KILL the "exclude sports/crypto" filter. The $7.4M bot operated on sports.
    Dead political markets = zero fills = zero P&L. We go where the action is.
  - RANK by 24h volume + book activity, not spread width.
  - KEEP negRisk filter (still need well-defined resolution semantics).
  - LOWER spread minimum from 4c to 2c (1c net capture is fine at high frequency).
  - ADD: number of outcomes per event (more outcomes = more tokens to MM = more fills).
  - NEW: for each market, compute "quotable_spread" = spread after we post inside.
    On 2c spread: quotable = 0c (can't post inside — join queue instead).
    On 3c spread: quotable = 1c.
    On 4c+: quotable = spread - 2c.

The right universe is: high-volume events where our quotes have a nonzero
chance of being filled within an hour.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "mm_universe.json")

# ── Selection criteria ──────────────────────────────────────────────
MIN_SPREAD = 0.02               # 2c minimum
MIN_VOLUME_24H_EVENT = 1000.0   # $1k daily volume on the EVENT (sum of markets)
MIN_BID = 0.01                  # both sides need a live bid
MID_RANGE = (0.05, 0.95)        # avoid tails
TARGET_UNIVERSE_SIZE = 50       # top N events by volume

CONCURRENCY = 30
_SESSION = requests.Session()
_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=40, pool_maxsize=40))


# ──────────────────────────────────────────────────────────────────────

def fetch_all_events() -> List[dict]:
    events = []
    for offset in range(0, 5000, 100):
        try:
            r = _SESSION.get(GAMMA_URL, params={
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


def fetch_book(token_id: str) -> dict:
    try:
        r = _SESSION.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        b = r.json()
    except Exception:
        return {"best_bid": 0, "best_ask": 0, "bid_sz": 0, "ask_sz": 0}
    bids = b.get("bids", []) or []
    asks = [a for a in b.get("asks", []) or [] if float(a.get("price", 0)) > 0]
    if not bids or not asks:
        return {"best_bid": 0, "best_ask": 0, "bid_sz": 0, "ask_sz": 0}
    best_bid = max(float(x["price"]) for x in bids)
    bid_sz = sum(float(x["size"]) for x in bids if float(x["price"]) == best_bid)
    best_ask = min(float(x["price"]) for x in asks)
    ask_sz = sum(float(x["size"]) for x in asks if float(x["price"]) == best_ask)
    return {"best_bid": best_bid, "best_ask": best_ask, "bid_sz": bid_sz, "ask_sz": ask_sz}


def fetch_books_parallel(tokens: List[str]) -> Dict[str, dict]:
    out = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(fetch_book, t): t for t in tokens}
        done = 0
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                out[t] = fut.result()
            except Exception:
                out[t] = {"best_bid": 0, "best_ask": 0, "bid_sz": 0, "ask_sz": 0}
            done += 1
            if done % 500 == 0:
                rate = done / max(time.time() - t0, 0.001)
                eta = (len(tokens) - done) / max(rate, 0.001)
                print(f"  {done}/{len(tokens)}  ({rate:.0f}/s  eta {eta:.0f}s)")
    return out


# ──────────────────────────────────────────────────────────────────────

def extract_markets(ev: dict) -> List[dict]:
    out = []
    for m in ev.get("markets", []):
        if m.get("closed") or not m.get("active", True):
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
            "question": m.get("question") or m.get("title"),
            "label": m.get("groupItemTitle") or m.get("question"),
            "yes_token": tokens[0],
            "no_token": tokens[1],
            "volume_24h": float(m.get("volume24hr") or 0),
            "volume_total": float(m.get("volume") or 0),
            "liquidity": float(m.get("liquidity") or 0),
        })
    return out


def days_to_resolution(ev: dict) -> Optional[float]:
    end = ev.get("endDate") or ev.get("end_date")
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        delta = dt - datetime.now(timezone.utc)
        return delta.total_seconds() / 86400.0
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────

def curate():
    print("=" * 90)
    print(f"MM EVENT CURATOR v3 (volume-first)  ({datetime.now().isoformat(timespec='seconds')})")
    print("=" * 90)

    events = fetch_all_events()
    print(f"active events: {len(events)}")

    # Stage 1: negRisk + volume threshold + has markets
    stage1 = []
    for ev in events:
        if ev.get("negRisk") is not True:
            continue
        markets = extract_markets(ev)
        if not markets:
            continue
        ev_vol_24h = sum(m["volume_24h"] for m in markets)
        if ev_vol_24h < MIN_VOLUME_24H_EVENT:
            continue
        dtr = days_to_resolution(ev)
        if dtr is not None and dtr < 1.0:
            continue  # expired or resolving today — skip
        stage1.append((ev, markets, ev_vol_24h, dtr))

    print(f"negRisk + vol >= ${MIN_VOLUME_24H_EVENT:.0f}/day: {len(stage1)} events")

    # Rank by 24h volume descending, take top N
    stage1.sort(key=lambda x: -x[2])
    stage1 = stage1[:TARGET_UNIVERSE_SIZE * 2]  # over-select, then filter by book quality

    # Stage 2: fetch books for all YES tokens in candidate events
    tokens = set()
    for ev, markets, _, _ in stage1:
        for m in markets:
            tokens.add(m["yes_token"])
    print(f"fetching {len(tokens)} books...")
    books = fetch_books_parallel(list(tokens))

    # Stage 3: score markets, build universe
    universe = []
    for ev, markets, ev_vol_24h, dtr in stage1:
        scored_markets = []
        for m in markets:
            bk = books.get(m["yes_token"], {})
            bb, ba = bk.get("best_bid", 0), bk.get("best_ask", 0)
            if bb < MIN_BID or ba <= 0:
                continue
            spread = ba - bb
            if spread < MIN_SPREAD:
                continue
            mid = (bb + ba) / 2.0
            if mid < MID_RANGE[0] or mid > MID_RANGE[1]:
                continue
            # "quotable spread" = what we capture after posting 1 tick inside each side
            quotable = max(0.0, spread - 0.02)  # 2c eaten by our 1c-inside on each side
            scored_markets.append({
                **m,
                "best_bid": bb,
                "best_ask": ba,
                "spread": spread,
                "quotable_spread": quotable,
                "mid": mid,
                "bid_size_usd": bb * bk.get("bid_sz", 0),
                "ask_size_usd": ba * bk.get("ask_sz", 0),
            })
        if scored_markets:
            universe.append({
                "event_slug": ev.get("slug"),
                "event_title": ev.get("title"),
                "days_to_resolution": round(dtr, 1) if dtr else None,
                "neg_risk": True,
                "event_volume_24h": ev_vol_24h,
                "n_quotable_markets": len(scored_markets),
                "markets": scored_markets,
            })

    # Final rank by volume, trim to target
    universe.sort(key=lambda x: -x["event_volume_24h"])
    universe = universe[:TARGET_UNIVERSE_SIZE]

    with open(OUTPUT_PATH, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": "v3-volume-first",
            "criteria": {
                "min_spread": MIN_SPREAD,
                "min_volume_24h_event": MIN_VOLUME_24H_EVENT,
                "mid_range": MID_RANGE,
            },
            "events": universe,
        }, f, indent=2)

    print("\n" + "=" * 90)
    print(f"curated universe: {len(universe)} events  |  "
          f"{sum(e['n_quotable_markets'] for e in universe)} quotable markets")
    print("=" * 90)
    for uev in universe[:50]:
        n = uev["n_quotable_markets"]
        avg_spread = sum(m["spread"] for m in uev["markets"]) / n if n else 0
        avg_vol = sum(m["volume_24h"] for m in uev["markets"]) / n if n else 0
        print(f"  ${uev['event_volume_24h']:>10,.0f}/d  n={n:<3d}  "
              f"spread={avg_spread*100:.1f}c  avgmktvol=${avg_vol:,.0f}/d  "
              f"dtr={uev['days_to_resolution'] or '?':>5}d  "
              f"{uev['event_title'][:50]}")

    print(f"\nwrote {OUTPUT_PATH}")


if __name__ == "__main__":
    curate()
