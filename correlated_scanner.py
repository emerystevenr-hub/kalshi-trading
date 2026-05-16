"""
POLYMARKET CORRELATED-OUTCOME SCANNER  (Rule 2)

Hunts structural arbitrage across mutually-exclusive outcomes within a single event.

Core identities that MUST hold:

  For an N-way exhaustive event (outcomes A, B, C, ...):
     (1)  sum over i of P(i_yes)        == 1.0
     (2)  P(i_no)                       == 1 - P(i_yes)   (per market)
     (3)  P(i_no)                       == sum over j≠i of P(j_yes)

Arb formulations (per-share, ignoring fees):

  Buy-all-YES arb:
      cost = sum(ask(i_yes)).  If cost < 1.0, buy one share of each YES → $1 payout guaranteed.
      edge = 1 - cost

  Buy-YES-complement arb (rule 2 classic, "home_no + draw_yes" case):
      For each outcome i:  cost = ask(i_no) + sum over j≠i of ask(j_yes)
      If cost < 1.0 → free lunch.  This catches dislocations that the pure
      sum-of-YES test misses when a single NO token is mispriced vs the rest.

  Sell-all-YES arb (sum of bids > 1.0):
      If sum(bid(i_yes)) > 1.0 you could short the whole basket.  On Polymarket
      you can't short, so we instead BUY the NO side of each outcome:
      cost_short = sum(ask(i_no)).  If < N-1, there's a long-NO arb.

Phase 1: log-only with full book depth walk for executable sizing.
"""

import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from itertools import combinations
from typing import Dict, List, Tuple

import requests

# Reusable session with connection pooling — huge speedup vs per-request sockets
_SESSION = requests.Session()
_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=40, pool_maxsize=40, max_retries=2)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)

CONCURRENCY = 30   # parallel book fetches

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"

LOG_PATH = os.path.join(os.path.dirname(__file__), "correlated_scanner_log.csv")
LOG_FIELDS = [
    "logged_at", "event_slug", "event_title", "n_outcomes", "arb_type",
    "edge_pct", "executable_usd", "sum_of_asks", "detail_json",
]

MIN_OUTCOMES = 2                 # include binary events for the yes+no check
MAX_OUTCOMES = 8                 # cap to prevent pathological huge events
EDGE_FLAG_PCT = 0.5              # minimum edge to log
MIN_BID = 0.005
MAX_SPREAD = 0.30


# ──────────────────────────────────────────────────────────────────────

def fetch_all_active_events() -> List[dict]:
    events = []
    for offset in range(0, 5000, 100):
        try:
            r = requests.get(GAMMA_URL, params={
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


def fetch_book(token_id: str) -> Tuple[list, list]:
    try:
        r = _SESSION.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        b = r.json()
    except Exception:
        return ([], [])
    bids = sorted([(float(x.get("price", 0)), float(x.get("size", 0)))
                   for x in b.get("bids", []) or []], key=lambda p: -p[0])
    asks = sorted([(float(x.get("price", 0)), float(x.get("size", 0)))
                   for x in b.get("asks", []) or []
                   if float(x.get("price", 0)) > 0], key=lambda p: p[0])
    return (bids, asks)


def fetch_books_parallel(token_ids: List[str], concurrency: int = CONCURRENCY,
                         progress_every: int = 500) -> Dict[str, Tuple[list, list]]:
    """Fetch books for many tokens in parallel. ~20-30x faster than serial."""
    results: Dict[str, Tuple[list, list]] = {}
    total = len(token_ids)
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(fetch_book, tid): tid for tid in token_ids}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                results[tid] = fut.result()
            except Exception:
                results[tid] = ([], [])
            done += 1
            if done % progress_every == 0:
                rate = done / max(time.time() - t0, 0.001)
                eta = (total - done) / max(rate, 0.001)
                print(f"  {done}/{total}  ({rate:.0f}/s  eta {eta:.0f}s)")
    return results


def top(side):
    return (side[0][0], side[0][1]) if side else (0.0, 0.0)


# ──────────────────────────────────────────────────────────────────────
# EVENT → OUTCOMES
# ──────────────────────────────────────────────────────────────────────

def extract_outcomes(event: dict) -> List[dict]:
    """
    Return list of outcomes.  Each outcome is:
        { 'label': str, 'yes_token': str, 'no_token': str }
    Only includes outcomes with two parseable tokens.
    """
    out = []
    for m in event.get("markets", []):
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
        label = m.get("groupItemTitle") or m.get("question") or m.get("title") or ""
        out.append({
            "label": label,
            "yes_token": tokens[0],
            "no_token": tokens[1],
        })
    return out


# ──────────────────────────────────────────────────────────────────────
# ARB CHECKS
# ──────────────────────────────────────────────────────────────────────

def walk_buy_all_yes(outcomes: List[dict], books: Dict[str, Tuple[list, list]]) -> Tuple[float, float]:
    """
    Simulate buying one share of every outcome's YES simultaneously.
    Walks each ask ladder in sync.  Returns (edge_per_share_final, executable_usd).
    """
    # For each outcome, we have a sorted ask ladder.  We fill one share across
    # all outcomes at a time: at each step, total cost = sum over i of ask_price_i.
    # Keep going while cost < 1.0.
    ladders = [list(books.get(o["yes_token"], ([], []))[1]) for o in outcomes]
    if any(not l for l in ladders):
        return (0.0, 0.0)

    pointers = [0] * len(outcomes)
    remaining = [l[0][1] for l in ladders]   # shares remaining at current level
    total_cost = 0.0
    total_shares = 0.0
    total_edge = 0.0

    while True:
        # current prices
        prices = []
        valid = True
        for i, l in enumerate(ladders):
            if pointers[i] >= len(l):
                valid = False
                break
            prices.append(l[pointers[i]][0])
        if not valid:
            break
        step_cost = sum(prices)
        if step_cost >= 1.0 - 1e-4:
            break
        # size we can fill: min of remaining at current level
        step_size = min(remaining)
        if step_size <= 0:
            break
        total_shares += step_size
        total_cost += step_size * step_cost
        total_edge += step_size * (1.0 - step_cost)
        # decrement
        for i in range(len(ladders)):
            remaining[i] -= step_size
            if remaining[i] <= 1e-9:
                pointers[i] += 1
                if pointers[i] < len(ladders[i]):
                    remaining[i] = ladders[i][pointers[i]][1]
                else:
                    remaining[i] = 0

    if total_shares == 0:
        return (0.0, 0.0)
    # executable notional at $1 payout per share
    return (total_edge / total_shares * 100.0, total_shares * 1.0)


def walk_long_no_basket(outcomes: List[dict], books: Dict[str, Tuple[list, list]]) -> Tuple[float, float]:
    """
    Buy one share of every NO token.  Guaranteed payout = N-1 (exactly one outcome
    resolves YES so N-1 of the NO tokens pay 1 each).  Arb iff sum(ask_no) < N-1.
    """
    n = len(outcomes)
    if n < 2:
        return (0.0, 0.0)
    ladders = [list(books.get(o["no_token"], ([], []))[1]) for o in outcomes]
    if any(not l for l in ladders):
        return (0.0, 0.0)

    pointers = [0] * n
    remaining = [l[0][1] for l in ladders]
    total_shares = 0.0
    total_edge = 0.0
    target = float(n - 1)

    while True:
        prices = []
        valid = True
        for i, l in enumerate(ladders):
            if pointers[i] >= len(l):
                valid = False
                break
            prices.append(l[pointers[i]][0])
        if not valid:
            break
        step_cost = sum(prices)
        if step_cost >= target - 1e-4:
            break
        step_size = min(remaining)
        if step_size <= 0:
            break
        total_shares += step_size
        total_edge += step_size * (target - step_cost)
        for i in range(n):
            remaining[i] -= step_size
            if remaining[i] <= 1e-9:
                pointers[i] += 1
                if pointers[i] < len(ladders[i]):
                    remaining[i] = ladders[i][pointers[i]][1]
                else:
                    remaining[i] = 0

    if total_shares == 0:
        return (0.0, 0.0)
    # edge is in "absolute $" units since payout is N-1 not 1
    # normalize to per-share-per-$ = edge / shares / (payout)
    edge_pct = (total_edge / (total_shares * target)) * 100.0
    executable_usd = total_shares * target
    return (edge_pct, executable_usd)


def walk_no_plus_others_yes(outcomes: List[dict], books: Dict[str, Tuple[list, list]]) -> List[dict]:
    """
    For each outcome i, check: ask(i_no) + sum_{j!=i} ask(j_yes) < 1.0 ?
    This is the classic dislocation catch (e.g. home_no + draw_yes + away_yes cheap).
    Payout logic:
      - if i occurs: i_no=0, all j_yes=0 → pays 0.  Loses entire cost.  ❌
    Wait — that means this is NOT a clean arb.
    Correct version: hedge i_no against buying j_yes only for ALL OTHER outcomes.
    Payout if i occurs: i_no=0 (cost lost), j_yes=0 for all j≠i (cost lost) → total 0.
    That's just going long on "not i" via two different routes.  NOT an arb.

    The real Rule 2 arb is between two DIFFERENT ways to price the same event:
        ask(i_no)   vs   sum_{j != i} bid(j_yes)
    because BUYING i_no is equivalent to SHORTING i, which on Polymarket equals
    buying {j_yes for all j != i}.  Arb iff ask(i_no) < sum_{j != i} bid(j_yes)
    (then: buy i_no, sell each j_yes via hitting its bid → locks spread).
    OR the reverse: sum_{j != i} ask(j_yes) < bid(i_no).

    We check BOTH directions for every outcome.
    """
    results = []
    n = len(outcomes)
    if n < 2:
        return results

    # Pre-compute tops for each token
    yes_top_ask = {}
    yes_top_bid = {}
    no_top_ask = {}
    no_top_bid = {}
    for o in outcomes:
        yb, ya = books.get(o["yes_token"], ([], []))
        nb, na = books.get(o["no_token"], ([], []))
        yes_top_ask[o["label"]] = top(ya)
        yes_top_bid[o["label"]] = top(yb)
        no_top_ask[o["label"]] = top(na)
        no_top_bid[o["label"]] = top(nb)

    for i, oi in enumerate(outcomes):
        label_i = oi["label"]
        i_no_ask, i_no_ask_sz = no_top_ask[label_i]
        i_no_bid, i_no_bid_sz = no_top_bid[label_i]

        others = [o for j, o in enumerate(outcomes) if j != i]
        # collect others' top yes ask/bid
        sum_j_yes_ask = 0.0
        min_j_yes_ask_sz = float("inf")
        sum_j_yes_bid = 0.0
        min_j_yes_bid_sz = float("inf")
        all_valid_ask = True
        all_valid_bid = True
        for oj in others:
            ja_px, ja_sz = yes_top_ask[oj["label"]]
            jb_px, jb_sz = yes_top_bid[oj["label"]]
            if ja_px <= 0:
                all_valid_ask = False
            else:
                sum_j_yes_ask += ja_px
                min_j_yes_ask_sz = min(min_j_yes_ask_sz, ja_sz)
            if jb_px <= 0:
                all_valid_bid = False
            else:
                sum_j_yes_bid += jb_px
                min_j_yes_bid_sz = min(min_j_yes_bid_sz, jb_sz)

        # Direction A: buy i_no, sell each j_yes (hit their bids)
        #   cost_side = i_no_ask (we pay this)
        #   hedge_credit = sum(j_yes_bid) (we receive this)
        #   Arb iff i_no_ask < sum_j_yes_bid
        if i_no_ask > 0 and all_valid_bid:
            edge_a = sum_j_yes_bid - i_no_ask
            if edge_a > 0:
                size_a = min(i_no_ask_sz, min_j_yes_bid_sz)
                if size_a > 0 and (edge_a / max(i_no_ask, 1e-9)) * 100 >= EDGE_FLAG_PCT:
                    results.append({
                        "arb_type": f"buy_{label_i}_NO + sell_others_YES",
                        "edge_pct": edge_a * 100.0,
                        "executable_usd": size_a * i_no_ask,
                        "detail": {
                            "i_no_ask": i_no_ask,
                            "sum_others_yes_bid": sum_j_yes_bid,
                            "size_shares": size_a,
                        },
                    })

        # Direction B: sell i_no (hit its bid), buy each j_yes
        #   credit = i_no_bid
        #   cost = sum_j_yes_ask
        #   Arb iff i_no_bid > sum_j_yes_ask
        if i_no_bid > 0 and all_valid_ask:
            edge_b = i_no_bid - sum_j_yes_ask
            if edge_b > 0:
                size_b = min(i_no_bid_sz, min_j_yes_ask_sz)
                if size_b > 0 and (edge_b / max(sum_j_yes_ask, 1e-9)) * 100 >= EDGE_FLAG_PCT:
                    results.append({
                        "arb_type": f"sell_{label_i}_NO + buy_others_YES",
                        "edge_pct": edge_b * 100.0,
                        "executable_usd": size_b * i_no_bid,
                        "detail": {
                            "i_no_bid": i_no_bid,
                            "sum_others_yes_ask": sum_j_yes_ask,
                            "size_shares": size_b,
                        },
                    })

    return results


# ──────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────

def ensure_log():
    if not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0:
        with open(LOG_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writeheader()


def append_log(row: dict):
    ensure_log()
    with open(LOG_PATH, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=LOG_FIELDS).writerow(
            {k: row.get(k, "") for k in LOG_FIELDS})


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────

def run():
    print("=" * 92)
    print(f"POLYMARKET CORRELATED-OUTCOME SCANNER — Rule 2  "
          f"({datetime.now().isoformat(timespec='seconds')})")
    print("=" * 92)

    events = fetch_all_active_events()
    print(f"active events: {len(events)}")

    # Filter to events tagged negRisk==true (mutually-exclusive + exhaustive).
    # This is Polymarket's own flag for true "one-winner" events — exactly the
    # structure the Rule 2 math requires.  Everything else (nested by-date
    # ladders, cumulative thresholds, multi-winner brackets) breaks our assumptions
    # and produces false-positive "arbs".
    neg_risk_events = [ev for ev in events if ev.get("negRisk") is True]
    non_neg_risk = len(events) - len(neg_risk_events)
    print(f"negRisk events: {len(neg_risk_events)}   "
          f"(filtered out {non_neg_risk} non-exclusive events)")

    candidate_events = []
    for ev in neg_risk_events:
        outs = extract_outcomes(ev)
        if MIN_OUTCOMES <= len(outs) <= MAX_OUTCOMES:
            candidate_events.append((ev, outs))
    print(f"candidate multi-outcome events (negRisk + size ok): {len(candidate_events)}")

    # Collect every token we need (yes + no per outcome)
    tokens = set()
    for ev, outs in candidate_events:
        for o in outs:
            tokens.add(o["yes_token"])
            tokens.add(o["no_token"])
    print(f"tokens to price: {len(tokens)}")

    print(f"fetching books (concurrency={CONCURRENCY})...")
    t_start = time.time()
    books = fetch_books_parallel(list(tokens))
    print(f"  done in {time.time() - t_start:.1f}s")

    now = datetime.now(timezone.utc).isoformat()
    hits_buy_all = []
    hits_long_no = []
    hits_cross = []

    for ev, outs in candidate_events:
        n = len(outs)

        # ── Test 1: sum of YES asks ────────────────────────────────
        if n >= 2:
            edge_pct, exec_usd = walk_buy_all_yes(outs, books)
            if edge_pct >= EDGE_FLAG_PCT and exec_usd > 0:
                hits_buy_all.append((ev, outs, edge_pct, exec_usd))
                append_log({
                    "logged_at": now,
                    "event_slug": ev.get("slug", ""),
                    "event_title": ev.get("title", ""),
                    "n_outcomes": n,
                    "arb_type": "buy_all_yes",
                    "edge_pct": f"{edge_pct:.3f}",
                    "executable_usd": f"{exec_usd:.2f}",
                    "sum_of_asks": "",
                    "detail_json": json.dumps([o["label"] for o in outs])[:2000],
                })

        # ── Test 2: long-NO-basket (sum ask_no < n-1) ──────────────
        if n >= 2:
            edge_pct, exec_usd = walk_long_no_basket(outs, books)
            if edge_pct >= EDGE_FLAG_PCT and exec_usd > 0:
                hits_long_no.append((ev, outs, edge_pct, exec_usd))
                append_log({
                    "logged_at": now,
                    "event_slug": ev.get("slug", ""),
                    "event_title": ev.get("title", ""),
                    "n_outcomes": n,
                    "arb_type": "long_no_basket",
                    "edge_pct": f"{edge_pct:.3f}",
                    "executable_usd": f"{exec_usd:.2f}",
                    "sum_of_asks": "",
                    "detail_json": json.dumps([o["label"] for o in outs])[:2000],
                })

        # ── Test 3: cross checks (i_no vs sum_others_yes) ──────────
        cross_hits = walk_no_plus_others_yes(outs, books)
        for hit in cross_hits:
            hits_cross.append((ev, hit))
            append_log({
                "logged_at": now,
                "event_slug": ev.get("slug", ""),
                "event_title": ev.get("title", ""),
                "n_outcomes": n,
                "arb_type": hit["arb_type"],
                "edge_pct": f"{hit['edge_pct']:.3f}",
                "executable_usd": f"{hit['executable_usd']:.2f}",
                "sum_of_asks": "",
                "detail_json": json.dumps(hit["detail"])[:2000],
            })

    # ── REPORT ────────────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print(f"BUY-ALL-YES hits:     {len(hits_buy_all)}")
    print(f"LONG-NO-BASKET hits:  {len(hits_long_no)}")
    print(f"CROSS-MARKET hits:    {len(hits_cross)}")
    print("=" * 92)

    for ev, outs, edge, usd in sorted(hits_buy_all, key=lambda x: -x[3])[:25]:
        print(f"\n🟢 BUY-ALL-YES  edge={edge:.2f}%  executable=${usd:,.0f}")
        print(f"   {ev.get('title','')}   (n={len(outs)})")
        for o in outs:
            _, ask = top(books.get(o['yes_token'], ([], []))[1])
            ask_px = books.get(o['yes_token'], ([], []))[1][0][0] if books.get(o['yes_token'], ([], []))[1] else 0
            print(f"     · {o['label']:<40s}  yes_ask={ask_px:.3f}")

    for ev, outs, edge, usd in sorted(hits_long_no, key=lambda x: -x[3])[:25]:
        print(f"\n🟢 LONG-NO-BASKET  edge={edge:.2f}%  executable=${usd:,.0f}")
        print(f"   {ev.get('title','')}   (n={len(outs)})")

    for ev, hit in sorted(hits_cross, key=lambda x: -x[1]["executable_usd"])[:25]:
        print(f"\n🟢 CROSS  edge={hit['edge_pct']:.2f}%  executable=${hit['executable_usd']:,.0f}")
        print(f"   {ev.get('title','')}")
        print(f"   {hit['arb_type']}")
        print(f"   detail: {hit['detail']}")

    print(f"\nlog: {LOG_PATH}")


if __name__ == "__main__":
    run()
