"""
Finds likely Kalshi ↔ Polymarket market pairs.

Pulls Kalshi primary markets + Polymarket events, matches by title similarity.
Outputs a starter venue_pairs.json that you can review and prune.

Focuses on overlapping event categories: NBA, NHL, Champions League, Premier League,
major politicals. These are where both venues have material volume on same outcomes.
"""

import json
import os
import re
import time
from typing import Dict, List, Optional

import requests

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com/events"
POLY_CLOB = "https://clob.polymarket.com"

OUT_PATH = os.path.join(os.path.dirname(__file__), "venue_pairs.json")

# Kalshi series/event prefixes we want to match on Polymarket
TARGET_KALSHI_PREFIXES = [
    "KXNBAF-",        # NBA Finals winner
    "KXNBAS-",        # NBA conference / series
    "KXNHLSC-",       # NHL Stanley Cup
    "KXNCAAM-",       # NCAA championship
    "KXMARMAD-",      # March Madness
    "KXUCL-",         # UEFA Champions League
    "KXEPL-",         # English Premier League
    "KXFTBOL-",       # other football
    "KXPGATOUR-",     # PGA tours
    "KXLPGATOUR-",    # LPGA
    "KXKFTOUR-",      # Korn Ferry
    "KXELON-",        # Elon-related
    "KXTRUMPADMIN",   # Trump admin changes
    "KXPRES",         # Presidential
    "KXFEDDECISION-", # Fed
    "KXGOVTSHUT",     # government shutdown
    "KXNEXTAG-",      # next AG
]


def fetch_kalshi_markets() -> List[dict]:
    out = []
    cursor = None
    while True:
        params = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{KALSHI_BASE}/markets", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        batch = data.get("markets", [])
        out.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return out


def fetch_poly_events() -> List[dict]:
    out = []
    for offset in range(0, 5000, 100):
        try:
            r = requests.get(POLY_GAMMA, params={
                "active": "true", "closed": "false",
                "limit": 100, "offset": offset,
            }, timeout=20)
            r.raise_for_status()
            b = r.json()
            if not b:
                break
            out.extend(b)
        except Exception:
            break
    return out


def normalize_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"20\d{2}", "", t)   # strip years
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


TOKEN_WORDS_COMMON = {"will", "win", "the", "a", "an", "2025", "2026", "be", "next"}


def kalshi_market_tokens(m: dict) -> set:
    title = normalize_title(m.get("title", ""))
    words = set(title.split()) - TOKEN_WORDS_COMMON
    return words


def poly_event_tokens(e: dict) -> set:
    title = normalize_title(e.get("title", ""))
    words = set(title.split()) - TOKEN_WORDS_COMMON
    return words


def find_matches(kalshi: List[dict], poly: List[dict]) -> List[dict]:
    # Filter Kalshi to our target prefixes
    kalshi_filtered = [m for m in kalshi if any(m.get("ticker", "").startswith(p) for p in TARGET_KALSHI_PREFIXES)]
    print(f"Kalshi filtered to target prefixes: {len(kalshi_filtered)}")

    # Pre-compute poly event tokens
    poly_enriched = []
    for e in poly:
        if not e.get("negRisk"):  # stick to negRisk events where outcomes are well-defined
            continue
        tokens = poly_event_tokens(e)
        if not tokens:
            continue
        poly_enriched.append({"event": e, "tokens": tokens})

    print(f"Poly negRisk events: {len(poly_enriched)}")

    matches = []
    for km in kalshi_filtered:
        k_tokens = kalshi_market_tokens(km)
        if not k_tokens:
            continue
        best_score = 0
        best_poly = None
        best_poly_market = None
        for pe in poly_enriched:
            overlap = len(k_tokens & pe["tokens"])
            if overlap >= 2 and overlap > best_score:
                best_score = overlap
                best_poly = pe["event"]
                # Find the specific outcome (Polymarket market) within this event
                # that best matches the Kalshi market's outcome
                for pm in best_poly.get("markets", []):
                    p_tokens = set(normalize_title(pm.get("question", "")).split()) - TOKEN_WORDS_COMMON
                    p_tokens |= set(normalize_title(pm.get("groupItemTitle", "")).split()) - TOKEN_WORDS_COMMON
                    o = len(k_tokens & p_tokens)
                    if o >= 1 and (best_poly_market is None or o > best_poly_market[1]):
                        best_poly_market = (pm, o)
        if best_poly and best_poly_market:
            pm, _ = best_poly_market
            tokens_field = pm.get("clobTokenIds")
            if isinstance(tokens_field, str):
                try:
                    tokens_field = json.loads(tokens_field)
                except Exception:
                    tokens_field = None
            if tokens_field and len(tokens_field) >= 2:
                matches.append({
                    "name": f"{best_poly['title']} - {pm.get('groupItemTitle') or pm.get('question', '')[:40]}",
                    "kalshi_ticker": km["ticker"],
                    "kalshi_title": km.get("title", ""),
                    "poly_token_id": tokens_field[0],
                    "poly_event_title": best_poly.get("title", ""),
                    "poly_market_question": pm.get("question", ""),
                    "match_score": best_score,
                    "notes": "AUTO-MATCHED — verify before using",
                })
    return matches


def main():
    print("Fetching Kalshi markets...")
    kalshi = fetch_kalshi_markets()
    print(f"  got {len(kalshi)} Kalshi markets")

    print("Fetching Polymarket events...")
    poly = fetch_poly_events()
    print(f"  got {len(poly)} Polymarket events")

    print("\nMatching...")
    matches = find_matches(kalshi, poly)
    print(f"  found {len(matches)} suggested pairs")

    # Sort by match score
    matches.sort(key=lambda x: -x["match_score"])

    with open(OUT_PATH, "w") as f:
        json.dump(matches, f, indent=2)
    print(f"\nwrote {OUT_PATH}")
    print("\nTOP 20 MATCHES (review for accuracy — match quality varies):")
    for m in matches[:20]:
        print(f"  [{m['match_score']}]  Kalshi: {m['kalshi_ticker']:<30s}  ↔  Poly: {m['poly_event_title'][:50]}")

    print(f"\n⚠️  AUTO-MATCHES ARE HEURISTIC. Review venue_pairs.json and delete bad matches before running the scanner.")
    print("   Especially check: team-vs-team matchups, specific outcome wording.")


if __name__ == "__main__":
    main()
