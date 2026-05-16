"""Terminal 1 — Logger Zero-Match Diagnostic.

Answers Steve's 4 questions:
  1. First 20 market records that SHOULD match weather — ticker/event/title/
     subtitle/series/category fields inspected.
  2. Exact boolean reasons for rejection on known-good tickers from v1 run.
  3. Object-shape comparison: v1 JSONL vs current /markets response.
  4. What the logger is actually filtering on + whether fields are populated.

Usage:
    python3 ~/Documents/terminal1_diagnose_logger.py
"""

import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import requests


BASE = "https://api.elections.kalshi.com/trade-api/v2"
V1_NYC = Path.home() / "Documents" / "terminal1_data" / "kalshi_NYC_2026-04-22.jsonl"
V1_ORD = Path.home() / "Documents" / "terminal1_data" / "kalshi_ORD_2026-04-22.jsonl"
V1_LAX = Path.home() / "Documents" / "terminal1_data" / "kalshi_LAX_2026-04-22.jsonl"

STATIONS = {
    "NYC": ["NY", "NYC", "NEW YORK"],
    "ORD": ["CHI", "CHICAGO", "ORD"],
    "LAX": ["LA", "LAX", "LOS ANGELES"],
}


def banner(s: str) -> None:
    print()
    print("=" * 88)
    print(s)
    print("=" * 88)


def _match_station(ticker: str, title: str) -> Tuple[Optional[str], Optional[str], dict]:
    """Return (station, matching_pattern, per_pattern_trace)."""
    upper = (ticker + " " + (title or "")).upper()
    trace = {}
    for station, patterns in STATIONS.items():
        for p in patterns:
            wb_match = bool(re.search(rf"\b{p}\b", upper))
            sub_match = p in ticker.upper()
            trace[f"{station}/{p}"] = (wb_match, sub_match)
            if wb_match or sub_match:
                return station, p, trace
    return None, None, trace


# -----------------------------------------------------------------------------
# 1. What did v1 capture? Load ground truth from the only file we have.
# -----------------------------------------------------------------------------
banner("1. V1 CAPTURED GROUND TRUTH")

def load_jsonl(path: Path) -> List[dict]:
    out = []
    if not path.exists():
        print(f"  [warn] not found: {path}")
        return out
    with open(path) as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


v1_nyc = load_jsonl(V1_NYC)
v1_ord = load_jsonl(V1_ORD)
v1_lax = load_jsonl(V1_LAX)

print(f"v1 captured: NYC={len(v1_nyc)}  ORD={len(v1_ord)}  LAX={len(v1_lax)}")
print(f"\nFirst 3 NYC records (shape Claude's code produced):")
for r in v1_nyc[:3]:
    print(f"  ticker        = {r.get('ticker')!r}")
    print(f"  event_ticker  = {r.get('event_ticker')!r}")
    print(f"  title         = {r.get('title','')[:70]!r}")
    print(f"  close_time    = {r.get('close_time')!r}")
    print(f"  yes_bid/ask   = {r.get('yes_bid')} / {r.get('yes_ask')}")
    print()

known_good: List[str] = [r["ticker"] for r in (v1_nyc + v1_ord + v1_lax)]
print(f"Known-good weather tickers captured by v1: {len(known_good)} total")
print(f"Sample: {known_good[:5]}")

# -----------------------------------------------------------------------------
# 2. What does /markets return RIGHT NOW — shape comparison
# -----------------------------------------------------------------------------
banner("2. /markets RESPONSE SHAPE (current)")

r = requests.get(f"{BASE}/markets", params={"limit": 1000, "status": "open"}, timeout=30)
print(f"HTTP {r.status_code}")
r.raise_for_status()
data = r.json()
print(f"Top-level keys: {list(data.keys())}")
markets = data.get("markets", [])
print(f"markets[] length: {len(markets)}")
print(f"cursor: {repr(data.get('cursor'))[:70]}")

if markets:
    print(f"\nFirst market full-keys: {sorted(markets[0].keys())}")
    print(f"\nFirst market sample (top 40 lines of pretty-printed):")
    print(json.dumps(markets[0], indent=2)[:1400])

# -----------------------------------------------------------------------------
# 3. Paginate fully, track duplicates, hunt for weather markets
# -----------------------------------------------------------------------------
banner("3. FULL PAGINATION + DUP TRACKING")

cursor = None
page = 0
all_weather: List[dict] = []
seen: set = set()
dupes = 0
total_scanned = 0
first_tickers_per_page: List[str] = []

while page < 60:
    params = {"limit": 1000, "status": "open"}
    if cursor:
        params["cursor"] = cursor
    rr = requests.get(f"{BASE}/markets", params=params, timeout=30)
    if rr.status_code != 200:
        print(f"  page {page} HTTP {rr.status_code} — stopping")
        break
    d2 = rr.json()
    batch = d2.get("markets", [])
    if not batch:
        print(f"  page {page}: EMPTY batch — stopping")
        break
    first_tickers_per_page.append(batch[0].get("ticker", "?"))
    total_scanned += len(batch)
    for m in batch:
        t = m.get("ticker", "")
        if t in seen:
            dupes += 1
            continue
        seen.add(t)
        if t.startswith(("KXHIGH", "KXLOW")):
            all_weather.append(m)
    cursor = d2.get("cursor")
    page += 1
    if not cursor:
        print(f"  page {page}: cursor null — stopping")
        break

print(f"Pages actually scanned: {page}")
print(f"Raw records seen:       {total_scanned}")
print(f"Unique tickers:         {len(seen)}")
print(f"Duplicate hits:         {dupes}")
print(f"  (non-zero dupes → pagination is looping)")
print(f"Weather markets found:  {len(all_weather)}")
print(f"\nFirst ticker of each page (first 15 pages):")
for i, t in enumerate(first_tickers_per_page[:15]):
    print(f"  page {i:2d}: {t}")
if len(first_tickers_per_page) > 15:
    print(f"  ... ({len(first_tickers_per_page) - 15} more)")
    print(f"Last 5 pages:")
    for i, t in enumerate(first_tickers_per_page[-5:], start=len(first_tickers_per_page)-5):
        print(f"  page {i:2d}: {t}")

# -----------------------------------------------------------------------------
# 4. Inspect first 20 weather markets — full field dump
# -----------------------------------------------------------------------------
banner("4. FIRST 20 WEATHER MARKETS — FIELD INSPECTION")
if not all_weather:
    print("  NO WEATHER MARKETS RETURNED IN THIS PAGINATION.")
else:
    for i, m in enumerate(all_weather[:20]):
        st, pat, trace = _match_station(m.get("ticker",""), m.get("title",""))
        print(f"[{i}] {m.get('ticker')!r}")
        print(f"    event_ticker   = {m.get('event_ticker')!r}")
        print(f"    title          = {m.get('title','')[:80]!r}")
        print(f"    subtitle       = {m.get('subtitle','')[:80]!r}")
        print(f"    series_ticker  = {m.get('series_ticker')!r}")
        print(f"    category       = {m.get('category')!r}")
        print(f"    matched_station= {st}  via_pattern={pat!r}")
        if st is None:
            # Show why it failed every pattern:
            print(f"    rejection trace (wb_match, substr_match):")
            for key, (wb, sub) in trace.items():
                if wb or sub:
                    print(f"      {key}: wb={wb} sub={sub}")
        print()

# -----------------------------------------------------------------------------
# 5. Rejection trace for v1 known-good tickers against current response
# -----------------------------------------------------------------------------
banner("5. REJECTION TRACE — v1 KNOWN-GOOD TICKERS vs CURRENT RESPONSE")
if known_good and seen:
    hit_in_current = sum(1 for t in set(known_good) if t in seen)
    print(f"Of {len(set(known_good))} unique v1-captured tickers, {hit_in_current} "
          f"are present in current response")
    print(f"\nPer-ticker trace (first 8):")
    for t in list(set(known_good))[:8]:
        present = t in seen
        sample_title = ""
        if present:
            for w in all_weather:
                if w["ticker"] == t:
                    sample_title = w.get("title","")[:60]
                    break
        st, pat, _ = _match_station(t, sample_title)
        print(f"  {t}")
        print(f"    present_in_current_response = {present}")
        print(f"    station_match_on_ticker+title = {st} ({pat})")
        print()

# -----------------------------------------------------------------------------
# 6. Field population audit
# -----------------------------------------------------------------------------
banner("6. FIELD POPULATION AUDIT (from page-1 markets)")
if markets:
    counts = {
        "ticker": 0,
        "event_ticker": 0,
        "title": 0,
        "subtitle": 0,
        "series_ticker": 0,
        "category": 0,
    }
    for m in markets:
        for k in counts:
            v = m.get(k)
            if v not in (None, ""):
                counts[k] += 1
    for k, c in counts.items():
        pct = 100.0 * c / len(markets)
        print(f"  {k:16s} populated in {c:4d} / {len(markets)}  ({pct:.1f}%)")

print()
print("Done.")
