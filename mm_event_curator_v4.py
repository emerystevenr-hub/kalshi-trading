"""
MM EVENT CURATOR — v4 (Market-first structural filter)

v4 changes vs v3:
  - Volume floor applied PER MARKET, not per event. $10k/day minimum per market.
    Previously: $1k/day event-level sum → dead outcomes included. Fix is structural.
  - Spread floor raised to 3c (was 2c). 2c with quote-join = 0c net gross capture.
  - Days-to-resolution >= 7 days (was 1d). Terminal-week MM is a different strategy.
  - Queue depth ceiling: drop markets where front-of-book USD > $5k. Your clips
    need queue position; if whales own the top, you're buried.
  - Book activity check: 2 samples 60s apart. Drop markets where nothing moved.
    Polymarket has many stale books — a frozen book is not tradeable at any clip.
  - Funnel diagnostic output at every stage. See where candidates drop so you
    know whether the strategy has a universe or not.

Output:
  - Writes mm_universe_v4.json (same schema as v3, consumable by live_mm.py
    and paper_mm.py). Does NOT overwrite mm_universe.json.

Usage:
  python3 mm_event_curator_v4.py
  (no args — tune constants below or override via env vars)
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "mm_universe_v4.json")

# ── Selection criteria (v4 — tick-aware) ────────────────────────────
# v4.1 fix: filter spread in TICKS, not absolute cents. Polymarket negRisk
# markets use 0.001 ticks. A "3 cent" filter excluded everything; "3 ticks"
# correctly compares precision-relative.
MIN_SPREAD_TICKS = int(os.environ.get("MM_MIN_SPREAD_TICKS", "3"))
MIN_MARKET_VOL_24H = float(os.environ.get("MM_MIN_MARKET_VOL", "10000"))
MIN_BID = 0.01
MID_RANGE = (0.05, 0.95)
MIN_DAYS_TO_RESOLUTION = float(os.environ.get("MM_MIN_DTR", "7.0"))
MAX_FRONT_OF_BOOK_USD = float(os.environ.get("MM_MAX_FRONT_BOOK", "5000"))
ACTIVITY_SAMPLE_GAP_S = int(os.environ.get("MM_ACTIVITY_GAP_S", "60"))  # 0 = skip
TARGET_UNIVERSE_SIZE = 50

CONCURRENCY = 1
_SESSION = requests.Session()
_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=40, pool_maxsize=40))


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
    # Match the known-working diagnostic pattern: plain requests.get, 15s timeout,
    # filter bids by size>0 (not just existence), track reason for empty.
    try:
        r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=15)
        r.raise_for_status()
        b = r.json()
    except Exception as e:
        return {"best_bid": 0.0, "best_ask": 0.0, "bid_sz": 0.0, "ask_sz": 0.0,
                "tick_size": 0.01, "min_order_size": 5.0, "_reason": f"http_error:{type(e).__name__}"}
    bids = [x for x in (b.get("bids") or []) if float(x.get("size", 0)) > 0]
    asks = [x for x in (b.get("asks") or []) if float(x.get("size", 0)) > 0 and float(x.get("price", 0)) > 0]
    tick_size = float(b.get("tick_size") or 0.01)
    min_order_size = float(b.get("min_order_size") or 5)
    if not bids and not asks:
        return {"best_bid": 0.0, "best_ask": 0.0, "bid_sz": 0.0, "ask_sz": 0.0,
                "tick_size": tick_size, "min_order_size": min_order_size, "_reason": "both_empty"}
    if not bids:
        return {"best_bid": 0.0, "best_ask": 0.0, "bid_sz": 0.0, "ask_sz": 0.0,
                "tick_size": tick_size, "min_order_size": min_order_size, "_reason": "no_bids"}
    if not asks:
        return {"best_bid": 0.0, "best_ask": 0.0, "bid_sz": 0.0, "ask_sz": 0.0,
                "tick_size": tick_size, "min_order_size": min_order_size, "_reason": "no_asks"}
    best_bid = max(float(x["price"]) for x in bids)
    bid_sz = sum(float(x["size"]) for x in bids if float(x["price"]) == best_bid)
    best_ask = min(float(x["price"]) for x in asks)
    ask_sz = sum(float(x["size"]) for x in asks if float(x["price"]) == best_ask)
    return {"best_bid": best_bid, "best_ask": best_ask, "bid_sz": bid_sz, "ask_sz": ask_sz,
            "tick_size": tick_size, "min_order_size": min_order_size, "_reason": "ok"}


def fetch_books_parallel(tokens: List[str]) -> Dict[str, dict]:
    """
    With CONCURRENCY=1 runs sequentially. Explicit small delay between fetches
    matches the known-working diagnostic rate (0.5-0.7s/req).
    """
    out: Dict[str, dict] = {}
    t0 = time.time()
    REQ_DELAY = 0.5 if CONCURRENCY <= 2 else 0.0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {}
        for i, t in enumerate(tokens):
            if REQ_DELAY > 0 and i > 0:
                time.sleep(REQ_DELAY)
            futs[ex.submit(fetch_book, t)] = t
        done = 0
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                out[t] = fut.result()
            except Exception:
                out[t] = {"best_bid": 0.0, "best_ask": 0.0, "bid_sz": 0.0, "ask_sz": 0.0}
            done += 1
            if done % 50 == 0:
                rate = done / max(time.time() - t0, 0.001)
                eta = (len(tokens) - done) / max(rate, 0.001)
                print(f"    {done}/{len(tokens)}  ({rate:.1f}/s  eta {eta:.0f}s)")
    return out


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


def book_changed(a: dict, b: dict) -> bool:
    """True if any of (bid, ask, bid_sz, ask_sz) differs between samples."""
    for k in ("best_bid", "best_ask", "bid_sz", "ask_sz"):
        if abs(a.get(k, 0) - b.get(k, 0)) > 1e-9:
            return True
    return False


def curate():
    print("=" * 90)
    print(f"MM EVENT CURATOR v4 (market-first)  ({datetime.now().isoformat(timespec='seconds')})")
    print("=" * 90)
    print(f"criteria: spread>={MIN_SPREAD_TICKS} ticks  vol/mkt>=${MIN_MARKET_VOL_24H:,.0f}/d  "
          f"dtr>={MIN_DAYS_TO_RESOLUTION:.0f}d  front<=${MAX_FRONT_OF_BOOK_USD:,.0f}  "
          f"activity_gap={ACTIVITY_SAMPLE_GAP_S}s")
    print()

    # --- STAGE 1: fetch all active events ---
    events = fetch_all_events()
    print(f"STAGE 1  events fetched:                                 {len(events):>6}")

    # --- STAGE 2: negRisk + has markets + days-to-resolution >= MIN ---
    stage2 = []
    for ev in events:
        if ev.get("negRisk") is not True:
            continue
        markets = extract_markets(ev)
        if not markets:
            continue
        dtr = days_to_resolution(ev)
        if dtr is not None and dtr < MIN_DAYS_TO_RESOLUTION:
            continue
        stage2.append((ev, markets, dtr))
    print(f"STAGE 2  negRisk + dtr >= {MIN_DAYS_TO_RESOLUTION:.0f}d:                              {len(stage2):>6}")

    # --- STAGE 3: per-market volume floor ---
    stage3 = []  # (ev, market_dict, dtr)
    total_candidate_markets = 0
    for ev, markets, dtr in stage2:
        for m in markets:
            total_candidate_markets += 1
            if m["volume_24h"] < MIN_MARKET_VOL_24H:
                continue
            stage3.append((ev, m, dtr))
    print(f"         (total candidate markets in those events:       {total_candidate_markets:>6})")
    print(f"STAGE 3  per-market vol_24h >= ${MIN_MARKET_VOL_24H:,.0f}:                 {len(stage3):>6}")

    if not stage3:
        print("\n*** NO MARKETS SURVIVE VOLUME FILTER ***")
        print("Signal: Polymarket's negRisk universe doesn't have markets doing")
        print(f"${MIN_MARKET_VOL_24H:,.0f}+/day individually. Options:")
        print("  1) Lower MM_MIN_MARKET_VOL (set env var) and re-run")
        print("  2) Drop the negRisk requirement to include binary markets")
        print("  3) Rethink the strategy — MM may not be viable here")
        return

    # --- STAGE 4: fetch books, apply spread/mid/queue filters ---
    print(f"\n  fetching {len(stage3)} books (sample 1 of 2)...")
    tokens = [m["yes_token"] for _, m, _ in stage3]
    books_t0 = fetch_books_parallel(tokens)

    stage4 = []  # (ev, market_enriched, dtr, book_t0)
    drop_book = drop_spread = drop_mid = drop_queue = 0
    drop_reasons: Dict[str, int] = {}
    for ev, m, dtr in stage3:
        bk = books_t0.get(m["yes_token"], {})
        bb, ba = bk.get("best_bid", 0), bk.get("best_ask", 0)
        tick = bk.get("tick_size", 0.01)
        if bb < MIN_BID or ba <= 0:
            drop_book += 1
            reason = bk.get("_reason", "unknown")
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
            continue
        spread = ba - bb
        spread_ticks = spread / tick if tick > 0 else 0
        # Filter: spread must be >= MIN_SPREAD_TICKS (tick-aware, not absolute cents)
        if spread_ticks < MIN_SPREAD_TICKS - 1e-9:
            drop_spread += 1
            continue
        mid = (bb + ba) / 2.0
        if mid < MID_RANGE[0] or mid > MID_RANGE[1]:
            drop_mid += 1
            continue
        bid_sz_usd = bb * bk.get("bid_sz", 0)
        ask_sz_usd = ba * bk.get("ask_sz", 0)
        if bid_sz_usd + ask_sz_usd > MAX_FRONT_OF_BOOK_USD:
            drop_queue += 1
            continue
        # Quotable spread: how many ticks we capture after posting 1 tick inside each side
        quotable_ticks = max(0, spread_ticks - 2)
        m_enriched = {
            **m,
            "best_bid": bb,
            "best_ask": ba,
            "spread": spread,
            "spread_ticks": round(spread_ticks, 2),
            "tick_size": tick,
            "min_order_size": bk.get("min_order_size", 5),
            "quotable_ticks": quotable_ticks,
            "quotable_spread_usd": quotable_ticks * tick,  # actual $ captured per share per round-trip
            "mid": mid,
            "bid_size_usd": bid_sz_usd,
            "ask_size_usd": ask_sz_usd,
        }
        stage4.append((ev, m_enriched, dtr, bk))
    print(f"STAGE 4  book filters (spread >= {MIN_SPREAD_TICKS} ticks):                 {len(stage4):>6}")
    print(f"         dropped — no book:{drop_book}  spread:{drop_spread}  mid:{drop_mid}  queue_deep:{drop_queue}")
    if drop_reasons:
        reason_str = "  ".join(f"{k}={v}" for k, v in sorted(drop_reasons.items(), key=lambda x: -x[1]))
        print(f"         no-book reasons: {reason_str}")

    if not stage4:
        print("\n*** NO MARKETS SURVIVE BOOK FILTERS ***")
        return

    # --- STAGE 5: book activity check (if enabled) ---
    if ACTIVITY_SAMPLE_GAP_S > 0:
        print(f"\n  waiting {ACTIVITY_SAMPLE_GAP_S}s for activity sample 2...")
        time.sleep(ACTIVITY_SAMPLE_GAP_S)
        tokens_s4 = [m["yes_token"] for _, m, _, _ in stage4]
        print(f"  fetching {len(tokens_s4)} books (sample 2 of 2)...")
        books_t1 = fetch_books_parallel(tokens_s4)

        stage5 = []
        for ev, m, dtr, bk_t0 in stage4:
            bk_t1 = books_t1.get(m["yes_token"], {})
            if not book_changed(bk_t0, bk_t1):
                continue
            stage5.append((ev, m, dtr))
        print(f"STAGE 5  book changed in {ACTIVITY_SAMPLE_GAP_S}s (not frozen):            {len(stage5):>6}")

        if not stage5:
            print("\n*** NO MARKETS PASS ACTIVITY CHECK ***")
            print(f"All candidate books were unchanged over {ACTIVITY_SAMPLE_GAP_S}s.")
            print("No taker flow, no maker reposting. Nothing to quote into.")
            return
    else:
        stage5 = [(ev, m, dtr) for ev, m, dtr, _ in stage4]
        print(f"STAGE 5  skipped (MM_ACTIVITY_GAP_S=0):                  {len(stage5):>6}")

    # --- ASSEMBLE output (v3-compatible JSON schema) ---
    by_event: Dict[str, dict] = {}
    for ev, m, dtr in stage5:
        slug = ev.get("slug") or str(ev.get("id", ""))
        if slug not in by_event:
            by_event[slug] = {
                "event_slug": slug,
                "event_title": ev.get("title"),
                "days_to_resolution": round(dtr, 1) if dtr else None,
                "neg_risk": True,
                "event_volume_24h": 0.0,
                "markets": [],
            }
        by_event[slug]["markets"].append(m)
        by_event[slug]["event_volume_24h"] += m["volume_24h"]

    for e in by_event.values():
        e["n_quotable_markets"] = len(e["markets"])

    universe = sorted(by_event.values(), key=lambda x: -x["event_volume_24h"])[:TARGET_UNIVERSE_SIZE]

    with open(OUTPUT_PATH, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": "v4-market-first",
            "criteria": {
                "min_spread_ticks": MIN_SPREAD_TICKS,
                "min_market_vol_24h": MIN_MARKET_VOL_24H,
                "mid_range": list(MID_RANGE),
                "min_days_to_resolution": MIN_DAYS_TO_RESOLUTION,
                "max_front_of_book_usd": MAX_FRONT_OF_BOOK_USD,
                "activity_sample_gap_s": ACTIVITY_SAMPLE_GAP_S,
            },
            "events": universe,
        }, f, indent=2)

    # --- FINAL SUMMARY ---
    total_markets = sum(e["n_quotable_markets"] for e in universe)
    print("\n" + "=" * 90)
    print(f"FINAL universe: {len(universe)} events  |  {total_markets} quotable markets")
    print("=" * 90)
    for uev in universe:
        n = uev["n_quotable_markets"]
        avg_spread_ticks = sum(m["spread_ticks"] for m in uev["markets"]) / n if n else 0
        avg_vol = sum(m["volume_24h"] for m in uev["markets"]) / n if n else 0
        # Get the dominant tick size in this event
        ticks = [m.get("tick_size", 0.01) for m in uev["markets"]]
        dom_tick = max(set(ticks), key=ticks.count) if ticks else 0.01
        dtr_str = f"{uev['days_to_resolution']:.1f}" if uev['days_to_resolution'] else "?"
        print(f"  ${uev['event_volume_24h']:>10,.0f}/d  n={n:<3d}  "
              f"sp={avg_spread_ticks:4.1f}t  tick={dom_tick}  "
              f"avgmktvol=${avg_vol:>8,.0f}/d  "
              f"dtr={dtr_str:>6s}d  {uev['event_title'][:40]}")

    print(f"\nwrote {OUTPUT_PATH}")

    print("\n" + "-" * 90)
    print("NEXT STEPS")
    print("-" * 90)
    if total_markets == 0:
        print("  0 markets survived. Strategic signal — MM on Polymarket negRisk events")
        print("  at $10k+/day per market may not have a viable universe right now.")
        print("  Drop negRisk, lower vol floor, or pivot strategy.")
    elif total_markets < 5:
        print(f"  {total_markets} markets. Borderline. Low diversification, concentrated risk.")
        print("  Consider lowering MM_MIN_MARKET_VOL to $5k and re-running to compare.")
    else:
        print(f"  {total_markets} markets. Viable universe.")
        print("  To use: cp mm_universe_v4.json mm_universe.json  (then proceed to shadow/paper/live)")


if __name__ == "__main__":
    curate()
