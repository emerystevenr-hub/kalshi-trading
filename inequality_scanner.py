"""
POLYMARKET INEQUALITY SCANNER — v2
Rule 4 (monotonicity on ladders), direction-aware, liquidity-filtered.

v2 changes from v1:
  - FIXED: direction classification now handles (LOW) / decline / approval / "at or below"
    markets where higher threshold → higher probability
  - FILTER: only flag violations where BOTH rungs have real two-sided liquidity
    (positive bid AND ask AND spread < MAX_SPREAD)
  - Walk book depth on each flagged pair to compute max $ executable before
    the inequality re-seals
  - Add executable $ size to the log so we can rank real edges over phantom asks

Inequality logic:
  We classify each rung as either UP-type (P decreases as threshold rises) or
  DOWN-type (P increases as threshold rises).  A violation exists iff the YES
  prices are out of that order.

Phase 1 still log-only. Execution comes next once we see real edges hold up.
"""

import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"

LOG_PATH = os.path.join(os.path.dirname(__file__), "inequality_scanner_log.csv")
LOG_FIELDS = [
    "logged_at", "event_slug", "event_title", "ladder_key", "ladder_class",
    "rung_low_q", "rung_low_thr", "rung_low_bid", "rung_low_ask",
    "rung_high_q", "rung_high_thr", "rung_high_bid", "rung_high_ask",
    "ask_violation_pct", "executable_usd", "two_sided",
    "token_low_yes", "token_high_yes",
]

VIOLATION_FLAG_PCT = 1.0       # minimum price inversion
MAX_SPREAD = 0.20              # skip rungs wider than this (phantom quotes)
MIN_BID = 0.01                 # both rungs must have a live bid at least this high
DEPTH_LEVELS = 10              # how deep to walk for executable sizing


# ──────────────────────────────────────────────────────────────────────
# EVENT FETCH
# ──────────────────────────────────────────────────────────────────────

def fetch_all_active_events() -> List[dict]:
    events = []
    for offset in range(0, 4000, 100):
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
            print(f"  ⚠️  fetch error at offset {offset}: {e}")
            break
    return events


# ──────────────────────────────────────────────────────────────────────
# LADDER DETECTION  (direction-aware)
# ──────────────────────────────────────────────────────────────────────

# UP-type: probability DECREASES as threshold rises
#   phrases: over / above / more than / at least / hit X (HIGH) / close above / exceed
UP_PATTERNS = [
    re.compile(r"\b(?:over|above|more than|greater than|at least|>=|>)\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)", re.I),
    re.compile(r"\bhit\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)\s*\(high\)", re.I),
    re.compile(r"\bexceed(?:s)?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)", re.I),
    re.compile(r"\bclose\s+(?:above|over)\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)", re.I),
]

# DOWN-type: probability INCREASES as threshold rises
#   phrases: under / below / less than / at most / hit X (LOW) / X or lower /
#            approval hit X% / drop to X / fall to X / cut to X
DOWN_PATTERNS = [
    re.compile(r"\b(?:under|below|less than|fewer than|at most|<=|<)\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)", re.I),
    re.compile(r"\bhit\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)\s*\(low\)", re.I),
    re.compile(r"\breach(?:es)?\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:%|percent)?\s*or\s+lower", re.I),
    re.compile(r"\b(?:drop|fall|cut|decline)s?\s+to\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)", re.I),
    re.compile(r"\bapproval\s+rating\s+hit\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)\s*%?", re.I),
]

# Ambiguous "hit/reach X" without HIGH/LOW qualifier — we SKIP these because we
# can't tell direction reliably (Palantir "reach $162" could be up-type or touch).
# A refinement pass would map per-ticker direction from current price; for now skip.
AMBIG_PATTERNS = [
    re.compile(r"\b(?:reach(?:es)?|hit)\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)\s*$", re.I),
]


def detect_rung(question: str) -> Optional[Tuple[str, float, str]]:
    """
    Return (class, threshold, stem) where class is 'UP' or 'DOWN', or None.
    """
    q = question.strip()
    for pat in DOWN_PATTERNS:
        m = pat.search(q)
        if m:
            thr = float(m.group(1).replace(",", ""))
            stem = pat.sub("__X__", q).lower().strip()
            return ("DOWN", thr, stem)
    for pat in UP_PATTERNS:
        m = pat.search(q)
        if m:
            thr = float(m.group(1).replace(",", ""))
            stem = pat.sub("__X__", q).lower().strip()
            return ("UP", thr, stem)
    # we ignore AMBIG for now — too many false positives
    return None


def group_ladders(event: dict) -> Dict[str, List[dict]]:
    ladders: Dict[str, List[dict]] = {}
    for m in event.get("markets", []):
        q = m.get("question") or m.get("title") or ""
        r = detect_rung(q)
        if not r:
            continue
        ladder_class, threshold, stem = r
        tokens = m.get("clobTokenIds")
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                tokens = None
        if not tokens or len(tokens) < 2:
            continue
        key = f"{ladder_class}:{stem}"
        ladders.setdefault(key, []).append({
            "question": q,
            "threshold": threshold,
            "class": ladder_class,
            "token_yes": tokens[0],
            "token_no": tokens[1],
        })
    return {k: sorted(v, key=lambda x: x["threshold"])
            for k, v in ladders.items() if len(v) >= 2}


# ──────────────────────────────────────────────────────────────────────
# BOOK FETCH — full book, not just top
# ──────────────────────────────────────────────────────────────────────

def fetch_book(token_id: str) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """Return (bids, asks) as lists of (price, size) sorted best-first."""
    try:
        r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        b = r.json()
    except Exception:
        return ([], [])
    bids_raw = b.get("bids", []) or []
    asks_raw = b.get("asks", []) or []
    bids = sorted([(float(x.get("price", 0)), float(x.get("size", 0))) for x in bids_raw],
                  key=lambda p: -p[0])
    asks = sorted([(float(x.get("price", 0)), float(x.get("size", 0))) for x in asks_raw
                   if float(x.get("price", 0)) > 0], key=lambda p: p[0])
    return (bids, asks)


def top(book_side: List[Tuple[float, float]]) -> float:
    return book_side[0][0] if book_side else 0.0


# ──────────────────────────────────────────────────────────────────────
# VIOLATION CHECK (direction-aware)
# ──────────────────────────────────────────────────────────────────────

def check_pair(low: dict, high: dict, books: Dict[str, Tuple[list, list]]) -> Optional[dict]:
    low_bids, low_asks = books.get(low["token_yes"], ([], []))
    high_bids, high_asks = books.get(high["token_yes"], ([], []))

    low_bid, low_ask = top(low_bids), top(low_asks)
    high_bid, high_ask = top(high_bids), top(high_asks)

    # Liquidity filter: both rungs must be two-sided with reasonable spread
    two_sided = (low_bid >= MIN_BID and low_ask > 0 and (low_ask - low_bid) < MAX_SPREAD
                 and high_bid >= MIN_BID and high_ask > 0 and (high_ask - high_bid) < MAX_SPREAD)

    ladder_class = low["class"]
    # Expected ordering of YES prices:
    #   UP-class:   low_price >= high_price   (buying low rung is more certain)
    #   DOWN-class: low_price <= high_price   (higher thr = easier = pricier)

    if ladder_class == "UP":
        # Violation: buy high.yes cheap + buy low.no cheap.  Arb if ask(high) + ask(low_no) < 1.
        # Simpler detection (sign of mispricing): ask(high) > ask(low)  ⇒  inversion magnitude
        ask_viol = (high_ask - low_ask) * 100.0
    else:  # DOWN
        ask_viol = (low_ask - high_ask) * 100.0

    if ask_viol < VIOLATION_FLAG_PCT:
        return None
    if not two_sided:
        return {
            "ask_violation_pct": ask_viol,
            "executable_usd": 0.0,
            "two_sided": False,
            "low_bid": low_bid, "low_ask": low_ask,
            "high_bid": high_bid, "high_ask": high_ask,
        }

    # Depth walk — how many $ can we lock in before the inequality re-seals?
    # For UP-class: we buy HIGH.yes on its ask + buy LOW.no (= 1 - LOW.bid side).
    # Polymarket NO-token: ask(no) = 1 - bid(yes). So the arb cost per share is
    # ask(high.yes) + (1 - bid(low.yes)) = ask(high) + 1 - low_bid.
    # Arb exists iff ask(high) + 1 - low_bid < 1  ⇔  ask(high) < low_bid.
    # For DOWN-class: symmetric — buy LOW.yes + buy HIGH.no ⇒ ask(low) < high_bid.
    if ladder_class == "UP":
        # iterate asks on high, bids on low in tandem
        hi_iter = iter(high_asks)
        lo_iter = iter(low_bids)
        cur_hi = next(hi_iter, None)
        cur_lo = next(lo_iter, None)
        total_size = 0.0
        total_cost = 0.0
        total_payoff_floor = 0.0
        while cur_hi and cur_lo:
            hi_px, hi_sz = cur_hi
            lo_px, lo_sz = cur_lo
            if hi_px >= lo_px:  # arb closed
                break
            size = min(hi_sz, lo_sz)
            total_size += size
            # cost per share: hi_px for HIGH.yes + (1 - lo_px) for LOW.no
            per_share_cost = hi_px + (1.0 - lo_px)
            total_cost += size * per_share_cost
            # payoff: at resolution, exactly one of (HIGH true, LOW false) is NOT satisfied
            # But since LOW is entailed by HIGH (UP-class: if price exceeds HIGH, it exceeded LOW),
            # the pair HIGH.yes + LOW.no has payoff exactly 1 when LOW fails, or (1+0)=1 when HIGH succeeds
            # Wait — if HIGH succeeds, LOW also succeeds, so LOW.no pays 0 ⇒ only HIGH.yes pays 1.
            # If HIGH fails but LOW succeeds, HIGH.yes=0, LOW.no=0 ⇒ pays 0.  This is NOT a clean arb!
            # Correct arb: buy HIGH.yes + sell LOW.yes.  Payoff always ≥ 0, = 0 when LOW succeeds & HIGH fails.
            # Max loss zero, positive payoff when HIGH succeeds (both pay, net 0) or both fail (0).
            # Since you CAN'T sell without a bid, synthesize SELL LOW.yes = BUY LOW.no.
            # BUY LOW.no payout: 1 if LOW fails. If LOW fails, HIGH fails too (UP-class) ⇒ HIGH.yes=0.
            # So pair payoff: LOW fails → LOW.no=1, HIGH.yes=0 → sum = 1
            #                 LOW succeeds, HIGH fails → LOW.no=0, HIGH.yes=0 → sum = 0  ❌ LOSS
            #                 HIGH succeeds → LOW.no=0, HIGH.yes=1 → sum = 1
            # So in the middle state (LOW yes, HIGH no) we lose cost.  Not a free arb.
            #
            # Conclusion: the true structural arb on an UP-class ladder requires SELLING the
            # expensive high.yes and BUYING the cheap low.yes when ask(low) < bid(high).
            # Re-do the depth walk with the correct directions.
            break
        # fall through — re-walk with the corrected formulation below
        total_size = 0.0
        total_cost = 0.0
        # Correct arb (UP-class): buy LOW.yes (cheap) and sell HIGH.yes (dear)
        #  — i.e. ask(low) vs bid(high).  Arb iff ask(low) < bid(high).
        lo_iter = iter(low_asks)
        hi_iter = iter(high_bids)
        cur_lo = next(lo_iter, None)
        cur_hi = next(hi_iter, None)
        while cur_lo and cur_hi:
            lo_px, lo_sz = cur_lo
            hi_px, hi_sz = cur_hi
            if lo_px >= hi_px:
                break
            size = min(lo_sz, hi_sz)
            total_size += size
            total_cost += size * (lo_px - hi_px)   # net debit per share (positive small number)
            try:
                cur_lo = (lo_px, lo_sz - size) if lo_sz > size else next(lo_iter)
            except StopIteration:
                cur_lo = None
            try:
                cur_hi = (hi_px, hi_sz - size) if hi_sz > size else next(hi_iter)
            except StopIteration:
                cur_hi = None
        # executable $ = total notional at mid
        if total_size > 0:
            # size is in shares; notional approx = total_size × mid price
            mid = (low_ask + high_bid) / 2.0
            executable_usd = total_size * mid
        else:
            executable_usd = 0.0
    else:  # DOWN
        # Correct arb: buy HIGH.yes (cheaper rung? no wait — in DOWN class, HIGH thr should be MORE likely)
        # Actually in DOWN-class ordering: low_yes_price <= high_yes_price is expected.
        # Violation means low_ask > high_ask OR low.bid > high.bid → buy HIGH.yes cheap, sell LOW.yes dear
        total_size = 0.0
        total_cost = 0.0
        hi_iter = iter(high_asks)
        lo_iter = iter(low_bids)
        cur_hi = next(hi_iter, None)
        cur_lo = next(lo_iter, None)
        while cur_hi and cur_lo:
            hi_px, hi_sz = cur_hi
            lo_px, lo_sz = cur_lo
            if hi_px >= lo_px:
                break
            size = min(hi_sz, lo_sz)
            total_size += size
            total_cost += size * (hi_px - lo_px)
            try:
                cur_hi = (hi_px, hi_sz - size) if hi_sz > size else next(hi_iter)
            except StopIteration:
                cur_hi = None
            try:
                cur_lo = (lo_px, lo_sz - size) if lo_sz > size else next(lo_iter)
            except StopIteration:
                cur_lo = None
        if total_size > 0:
            mid = (high_ask + low_bid) / 2.0
            executable_usd = total_size * mid
        else:
            executable_usd = 0.0

    return {
        "ask_violation_pct": ask_viol,
        "executable_usd": executable_usd,
        "two_sided": True,
        "low_bid": low_bid, "low_ask": low_ask,
        "high_bid": high_bid, "high_ask": high_ask,
    }


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

def run(min_rungs: int = 2, verbose: bool = True):
    print("=" * 92)
    print(f"POLYMARKET INEQUALITY SCANNER v2 — Rule 4 ladders  "
          f"({datetime.now().isoformat(timespec='seconds')})")
    print("=" * 92)

    print("Fetching active events...")
    events = fetch_all_active_events()
    print(f"  {len(events)} active events")

    ladders_all = []
    up_count = 0
    down_count = 0
    for ev in events:
        ladders = group_ladders(ev)
        for key, rungs in ladders.items():
            if len(rungs) >= min_rungs:
                ladders_all.append((ev, key, rungs))
                if rungs[0]["class"] == "UP":
                    up_count += 1
                else:
                    down_count += 1

    print(f"  {len(ladders_all)} ladders ({up_count} UP-class, {down_count} DOWN-class)")
    if not ladders_all:
        return

    tokens_needed = set()
    for _, _, rungs in ladders_all:
        for r in rungs:
            tokens_needed.add(r["token_yes"])
    print(f"  {len(tokens_needed)} YES tokens to price")

    print("Fetching books...")
    books: Dict[str, Tuple[list, list]] = {}
    for i, t in enumerate(tokens_needed, 1):
        books[t] = fetch_book(t)
        if i % 50 == 0 and verbose:
            print(f"  {i}/{len(tokens_needed)}")
        time.sleep(0.04)

    now = datetime.now(timezone.utc).isoformat()
    real_violations = []
    phantom_violations = []
    for ev, key, rungs in ladders_all:
        for a, b in zip(rungs[:-1], rungs[1:]):
            res = check_pair(a, b, books)
            if not res:
                continue
            row = {
                "logged_at": now,
                "event_slug": ev.get("slug", ""),
                "event_title": ev.get("title", ""),
                "ladder_key": key,
                "ladder_class": a["class"],
                "rung_low_q": a["question"],
                "rung_low_thr": a["threshold"],
                "rung_low_bid": f"{res['low_bid']:.3f}",
                "rung_low_ask": f"{res['low_ask']:.3f}",
                "rung_high_q": b["question"],
                "rung_high_thr": b["threshold"],
                "rung_high_bid": f"{res['high_bid']:.3f}",
                "rung_high_ask": f"{res['high_ask']:.3f}",
                "ask_violation_pct": f"{res['ask_violation_pct']:.2f}",
                "executable_usd": f"{res['executable_usd']:.2f}",
                "two_sided": res["two_sided"],
                "token_low_yes": a["token_yes"],
                "token_high_yes": b["token_yes"],
            }
            append_log(row)
            if res["two_sided"] and res["executable_usd"] > 0:
                real_violations.append((ev, key, a, b, res))
            else:
                phantom_violations.append((ev, key, a, b, res))

    # ── REPORT ────────────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print(f"REAL violations (two-sided, executable): {len(real_violations)}")
    print(f"Phantom violations (one-sided / illiquid):  {len(phantom_violations)}")
    print("=" * 92)

    for ev, key, a, b, res in sorted(real_violations, key=lambda x: -x[4]["executable_usd"])[:40]:
        print(f"\n🟢 [{a['class']}] {ev.get('title','')}")
        print(f"   LOW  thr={a['threshold']:<8g} bid={res['low_bid']:.3f} ask={res['low_ask']:.3f}"
              f"  |  {a['question']}")
        print(f"   HIGH thr={b['threshold']:<8g} bid={res['high_bid']:.3f} ask={res['high_ask']:.3f}"
              f"  |  {b['question']}")
        print(f"   inversion={res['ask_violation_pct']:+.2f}%   "
              f"executable≈${res['executable_usd']:,.0f}")

    print(f"\nlog: {LOG_PATH}")
    print("\nNote: 'executable_usd' is a depth-walk estimate. Actual fill risk depends on")
    print("maker/taker fee structure and whether both orders can land simultaneously.")


if __name__ == "__main__":
    import sys
    min_rungs = 2
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        min_rungs = int(sys.argv[1])
    run(min_rungs=min_rungs)
