"""Polymarket reconnaissance — cross-venue match against Kalshi catalyst positions.

Run this on your Mac (Cowork sandbox is allowlist-blocked from polymarket.com).
Output: matched candidate pairs for Engine 3 arb detection.

Dependencies:
  pip install py-clob-client 'httpx[socks]' requests --break-system-packages

Usage:
  python3 polymarket_recon.py
  python3 polymarket_recon.py --verbose       # show all candidates, not just matches
  python3 polymarket_recon.py --spread-min 3  # min spread in cents to flag as arb

Outputs two files next to this script:
  - polymarket_recon_<timestamp>.json   (full machine-readable)
  - polymarket_recon_<timestamp>.md     (human-readable match report)
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False
    print("WARN: py-clob-client not installed. Falling back to Gamma API only.",
          file=sys.stderr)
    print("      Install with: pip install py-clob-client 'httpx[socks]' --break-system-packages",
          file=sys.stderr)


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


# ──────────────────────────────────────────────────────────────────────
# Kalshi positions to match against (derived from current catalyst state).
# Edit this list to match your actual open positions, or read from the
# kalshi_catalyst_fills.csv dynamically (see _read_kalshi_positions below).
# ──────────────────────────────────────────────────────────────────────

KALSHI_POSITIONS = [
    {
        "ticker": "KXHORMUZNORM-26MAR17-B260501",
        "thesis_id": "hfi_hormuz_may01",
        "title": "Hormuz normalization by May 1, 2026",
        "search_terms": ["hormuz", "strait of hormuz", "iran oil", "oil shipping"],
        "side": "NO",
        "entry_price": 0.79,
        "resolution_date": "2026-05-01",
    },
    {
        "ticker": "KXHORMUZNORM-26MAR17-B260515",
        "thesis_id": "hfi_hormuz_may15",
        "title": "Hormuz normalization by May 15, 2026",
        "search_terms": ["hormuz", "strait of hormuz"],
        "side": "NO",
        "entry_price": 0.57,
        "resolution_date": "2026-05-15",
    },
    {
        "ticker": "KXUAPFILES-27",
        "thesis_id": "uap_files_fade",
        "title": "Trump releases UFO/UAP files before 2027",
        "search_terms": ["ufo", "uap", "alien", "extraterrestrial", "unidentified"],
        "side": "NO",
        "entry_price": 0.21,
        "resolution_date": "2026-12-31",
    },
    {
        "ticker": "KXUSAIRANAGREEMENT-27-26JUN",
        "thesis_id": "hfi_iran_jun",
        "title": "US-Iran agreement by June 2026",
        "search_terms": ["iran", "iran deal", "iran nuclear", "iran agreement"],
        "side": "NO",
        "entry_price": 0.53,
        "resolution_date": "2026-06-30",
    },
    {
        "ticker": "KXUSAIRANAGREEMENT-27-26MAY",
        "thesis_id": "hfi_iran_may",
        "title": "US-Iran agreement by May 2026",
        "search_terms": ["iran", "iran deal", "iran nuclear"],
        "side": "NO",
        "entry_price": 0.74,
        "resolution_date": "2026-05-31",
    },
]


# ──────────────────────────────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────────────────────────────

@dataclass
class PolyMarket:
    condition_id: str
    question: str
    slug: str
    description: str
    category: str
    tags: List[str]
    active: bool
    closed: bool
    end_date: str
    volume: float
    liquidity: float
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    yes_price: Optional[float] = None
    no_price: Optional[float] = None
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    # Full text blob from ALL string fields, lowercased — used for defensive matching
    _searchable: str = ""


@dataclass
class ArbCandidate:
    kalshi_ticker: str
    kalshi_thesis: str
    kalshi_side: str
    kalshi_entry_price: float
    kalshi_current_mark: float        # inferred from entry (we can't hit Kalshi from here)
    poly_condition_id: str
    poly_question: str
    poly_slug: str
    poly_yes_price: float             # implied YES probability on Polymarket
    poly_no_price: float              # implied NO probability
    # Arb analysis (from Kalshi's NO-bet perspective)
    kalshi_no_implied: float          # our NO position = betting against YES
    poly_implied_equivalent: float    # aligned to Kalshi's side
    spread_dollars: float             # absolute price diff
    spread_direction: str             # which venue is rich
    poly_volume: float
    match_confidence: str             # "high", "medium", "low"
    match_reason: str


# ──────────────────────────────────────────────────────────────────────
# Gamma API helpers
# ──────────────────────────────────────────────────────────────────────

def gamma_search_markets(search_term: str, limit: int = 100) -> List[dict]:
    """Search Polymarket markets via Gamma API. Case-insensitive substring match."""
    # Gamma supports filtering but keyword search is limited; we fetch broadly and filter client-side
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        r = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=15)
        r.raise_for_status()
        markets = r.json()
    except Exception as e:
        print(f"  [gamma] fetch error for term={search_term!r}: {e}", file=sys.stderr)
        return []
    term_lower = search_term.lower()
    matches = []
    for m in markets:
        question = (m.get("question") or "").lower()
        slug = (m.get("slug") or "").lower()
        description = (m.get("description") or "").lower()
        if (term_lower in question or term_lower in slug or term_lower in description):
            matches.append(m)
    return matches


def gamma_fetch_all_active(max_pages: int = 10, page_size: int = 500) -> List[dict]:
    """Fetch the full active-markets universe, paginated."""
    all_markets = []
    for page in range(max_pages):
        params = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": page * page_size,
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            r = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=15)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"  [gamma] page {page} error: {e}", file=sys.stderr)
            break
        if not batch:
            break
        all_markets.extend(batch)
        print(f"  [gamma] page {page}: {len(batch)} markets (cumulative: {len(all_markets)})",
              file=sys.stderr)
        if len(batch) < page_size:
            break
        time.sleep(0.2)   # gentle rate limit
    return all_markets


def extract_poly_market(raw: dict) -> Optional[PolyMarket]:
    """Flatten a Gamma API market dict into our PolyMarket dataclass.

    Defensive: use any available ID field (conditionId, condition_id, id,
    slug) so we don't silently drop markets when schema varies across
    endpoints. Build a _searchable text blob from every string-valued
    field so keyword matching never misses because the text lives in
    description/tags/outcomes instead of question/slug.
    """
    cid = (raw.get("conditionId") or raw.get("condition_id")
           or raw.get("id") or raw.get("slug") or "")
    if not cid:
        return None

    # Tokens: CLOB markets have `tokens`, older Gamma markets have `clobTokenIds` + `outcomes`
    yes_tid, no_tid = None, None
    tokens = raw.get("tokens") or []
    for t in tokens:
        outcome = (t.get("outcome") or "").lower()
        tid = t.get("token_id") or t.get("tokenId")
        if outcome == "yes":
            yes_tid = tid
        elif outcome == "no":
            no_tid = tid
    # Fallback: clobTokenIds array paired with outcomes array
    if not (yes_tid or no_tid):
        clob_tids = raw.get("clobTokenIds") or []
        if isinstance(clob_tids, str):
            try:
                clob_tids = json.loads(clob_tids)
            except Exception:
                clob_tids = []
        outcomes = raw.get("outcomes") or []
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []
        for i, outcome in enumerate(outcomes):
            if i >= len(clob_tids):
                break
            ol = (outcome or "").lower()
            if ol == "yes":
                yes_tid = clob_tids[i]
            elif ol == "no":
                no_tid = clob_tids[i]

    # Tags can be a list of dicts, list of strings, or absent
    tags_raw = raw.get("tags") or []
    tags: List[str] = []
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, dict):
                tags.append(str(t.get("label") or t.get("slug") or t.get("name") or ""))
            elif isinstance(t, str):
                tags.append(t)
    elif isinstance(tags_raw, str):
        tags = [tags_raw]

    # Extract prices from outcomePrices array when available
    yes_price, no_price = None, None
    outcome_prices = raw.get("outcomePrices")
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            outcome_prices = None
    if isinstance(outcome_prices, list):
        outcomes = raw.get("outcomes") or []
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []
        for i, o in enumerate(outcomes):
            if i >= len(outcome_prices):
                break
            ol = (o or "").lower()
            try:
                p = float(outcome_prices[i])
            except (ValueError, TypeError):
                continue
            if ol == "yes":
                yes_price = p
            elif ol == "no":
                no_price = p

    # Build searchable blob from ALL string-valued fields
    searchable_parts = [
        raw.get("question", ""),
        raw.get("slug", ""),
        raw.get("description", ""),
        raw.get("category", ""),
        raw.get("subcategory", ""),
        raw.get("groupItemTitle", ""),
    ] + tags
    searchable = " ".join(str(s) for s in searchable_parts if s).lower()

    return PolyMarket(
        condition_id=cid,
        question=raw.get("question", ""),
        slug=raw.get("slug", ""),
        description=raw.get("description", ""),
        category=raw.get("category", ""),
        tags=tags,
        active=bool(raw.get("active", False)),
        closed=bool(raw.get("closed", False)),
        end_date=raw.get("endDate") or raw.get("end_date", ""),
        volume=float(raw.get("volume", 0) or 0),
        liquidity=float(raw.get("liquidity", 0) or 0),
        yes_token_id=yes_tid,
        no_token_id=no_tid,
        yes_price=yes_price,
        no_price=no_price,
        _searchable=searchable,
    )


# ──────────────────────────────────────────────────────────────────────
# CLOB price enrichment
# ──────────────────────────────────────────────────────────────────────

def enrich_prices(markets: List[PolyMarket]) -> None:
    """Populate YES/NO prices via Gamma's /prices endpoint and/or CLOB SDK.
    Mutates markets in place."""
    if not markets:
        return
    if HAS_CLOB:
        try:
            clob = ClobClient(CLOB_BASE, chain_id=POLYGON)
            for m in markets:
                if m.yes_token_id:
                    try:
                        book = clob.get_order_book(m.yes_token_id)
                        # order_book has bids/asks — top of book
                        bids = book.bids or []
                        asks = book.asks or []
                        if bids:
                            m.yes_bid = float(bids[0].price)
                        if asks:
                            m.yes_ask = float(asks[0].price)
                        if m.yes_bid and m.yes_ask:
                            m.yes_price = (m.yes_bid + m.yes_ask) / 2.0
                        mid = clob.get_midpoint(m.yes_token_id)
                        if mid and isinstance(mid, dict) and "mid" in mid:
                            m.yes_price = float(mid["mid"])
                    except Exception as e:
                        print(f"  [clob] book fetch failed for {m.slug[:40]}: {e}",
                              file=sys.stderr)
                if m.no_token_id and m.yes_price is not None:
                    m.no_price = round(1.0 - m.yes_price, 4)
                time.sleep(0.1)
        except Exception as e:
            print(f"  [clob] client init failed: {e}", file=sys.stderr)
            print(f"  [clob] falling back to Gamma prices", file=sys.stderr)
    # Fallback: Gamma's lastTradePrice is available on market objects
    for m in markets:
        if m.yes_price is None:
            # Already extracted into dataclass; nothing more to do
            pass


# ──────────────────────────────────────────────────────────────────────
# Cross-venue matching
# ──────────────────────────────────────────────────────────────────────

def score_match(kalshi_pos: dict, poly: PolyMarket) -> Tuple[str, str]:
    """Return (confidence, reason) for a potential cross-venue match.

    Confidence: high / medium / low / none

    Uses the full _searchable blob (question + slug + description + tags
    + category) so matches aren't missed because the keyword lives in a
    less-obvious field.
    """
    combined = poly._searchable or f"{poly.question.lower()} {poly.slug.lower()}"

    # Count keyword hits
    hits = sum(1 for kw in kalshi_pos["search_terms"] if kw.lower() in combined)

    # Resolution date proximity
    date_match = False
    kalshi_date = kalshi_pos.get("resolution_date", "")
    if kalshi_date and poly.end_date:
        try:
            kd = datetime.fromisoformat(kalshi_date)
            pd = datetime.fromisoformat(poly.end_date.replace("Z", "+00:00"))
            delta_days = abs((pd - kd).total_seconds()) / 86400
            date_match = delta_days < 30
        except Exception:
            pass

    # Scoring
    if hits >= 2 and date_match:
        return ("high", f"{hits} keyword hits + resolution within 30d")
    if hits >= 2:
        return ("medium", f"{hits} keyword hits, resolution dates differ")
    if hits >= 1 and date_match:
        return ("medium", f"{hits} keyword hit + resolution match")
    if hits >= 1:
        return ("low", f"{hits} keyword hit only")
    return ("none", "no keyword overlap")


def find_matches(kalshi_positions: List[dict],
                 poly_markets: List[PolyMarket]) -> List[ArbCandidate]:
    """For each Kalshi position, find matching Polymarket contracts."""
    candidates = []
    for kp in kalshi_positions:
        for pm in poly_markets:
            if pm.closed or not pm.active:
                continue
            confidence, reason = score_match(kp, pm)
            if confidence == "none":
                continue
            # Compute spread analysis from Kalshi NO perspective
            # Kalshi NO price = 1 - Kalshi YES implied probability
            # Assuming we're still near entry (we can't fetch Kalshi from here),
            # kalshi_no_implied = kalshi entry_price (NO side already)
            kalshi_no_implied = kp["entry_price"] if kp["side"] == "NO" else 1 - kp["entry_price"]
            # Polymarket equivalent: if Kalshi is on NO, we want Polymarket NO price
            poly_equivalent = pm.no_price if pm.no_price is not None else (
                (1.0 - pm.yes_price) if pm.yes_price is not None else 0.0
            )
            if poly_equivalent <= 0:
                continue  # no price data
            spread = poly_equivalent - kalshi_no_implied
            direction = "poly_richer" if spread > 0 else "kalshi_richer"
            candidates.append(ArbCandidate(
                kalshi_ticker=kp["ticker"],
                kalshi_thesis=kp["thesis_id"],
                kalshi_side=kp["side"],
                kalshi_entry_price=kp["entry_price"],
                kalshi_current_mark=kp["entry_price"],   # approximation
                poly_condition_id=pm.condition_id,
                poly_question=pm.question,
                poly_slug=pm.slug,
                poly_yes_price=pm.yes_price or 0.0,
                poly_no_price=pm.no_price or 0.0,
                kalshi_no_implied=kalshi_no_implied,
                poly_implied_equivalent=poly_equivalent,
                spread_dollars=abs(spread),
                spread_direction=direction,
                poly_volume=pm.volume,
                match_confidence=confidence,
                match_reason=reason,
            ))
    # Sort by confidence × spread
    conf_rank = {"high": 3, "medium": 2, "low": 1}
    candidates.sort(key=lambda c: (conf_rank.get(c.match_confidence, 0),
                                    c.spread_dollars), reverse=True)
    return candidates


# ──────────────────────────────────────────────────────────────────────
# Output writers
# ──────────────────────────────────────────────────────────────────────

def write_json(candidates: List[ArbCandidate], all_poly: List[PolyMarket],
               path: str):
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kalshi_positions_checked": len(KALSHI_POSITIONS),
        "polymarket_universe_size": len(all_poly),
        "candidates": [asdict(c) for c in candidates],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[recon] wrote {path}")


def write_markdown(candidates: List[ArbCandidate], all_poly: List[PolyMarket],
                   path: str, min_spread: float):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append(f"# Polymarket Cross-Venue Match Report ({now})")
    lines.append("")
    lines.append(f"- Kalshi positions checked: {len(KALSHI_POSITIONS)}")
    lines.append(f"- Polymarket universe size: {len(all_poly)}")
    lines.append(f"- Match candidates: {len(candidates)}")
    lines.append(f"- Min-spread threshold for arb flag: ${min_spread:.3f}")
    lines.append("")

    arb_worthy = [c for c in candidates if c.spread_dollars >= min_spread]
    if arb_worthy:
        lines.append(f"## ARB CANDIDATES (spread >= ${min_spread:.3f})")
        lines.append("")
        lines.append("| Conf | Kalshi thesis | Polymarket slug | Spread | Direction | Poly vol |")
        lines.append("|---|---|---|---:|---|---:|")
        for c in arb_worthy:
            lines.append(
                f"| {c.match_confidence} | `{c.kalshi_thesis}` | `{c.poly_slug[:40]}` | "
                f"${c.spread_dollars:.3f} | {c.spread_direction} | ${c.poly_volume:,.0f} |"
            )
        lines.append("")

    lines.append("## All match candidates by Kalshi thesis")
    lines.append("")
    by_thesis = {}
    for c in candidates:
        by_thesis.setdefault(c.kalshi_thesis, []).append(c)
    for thesis, cands in by_thesis.items():
        lines.append(f"### `{thesis}`")
        lines.append("")
        lines.append(f"- Kalshi side: **{cands[0].kalshi_side}** @ entry ${cands[0].kalshi_entry_price:.3f}")
        lines.append(f"- Candidates found: {len(cands)}")
        lines.append("")
        for i, c in enumerate(cands[:10], 1):
            lines.append(f"**{i}. [{c.match_confidence.upper()}]** {c.poly_question}")
            lines.append(f"   - slug: `{c.poly_slug}`")
            lines.append(f"   - condition_id: `{c.poly_condition_id}`")
            lines.append(f"   - Poly YES: ${c.poly_yes_price:.3f} / NO: ${c.poly_no_price:.3f}")
            lines.append(f"   - Kalshi NO implied: ${c.kalshi_no_implied:.3f} / Poly equivalent: ${c.poly_implied_equivalent:.3f}")
            lines.append(f"   - **spread: ${c.spread_dollars:.3f}** ({c.spread_direction})")
            lines.append(f"   - Poly 24h vol: ${c.poly_volume:,.0f}")
            lines.append(f"   - match reason: {c.match_reason}")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_Generated by `polymarket_recon.py`._")

    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(f"[recon] wrote {path}")


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="Print all candidates, not just matches")
    ap.add_argument("--spread-min", type=float, default=0.03,
                    help="Min spread (dollars) to flag as arb (default 0.03)")
    ap.add_argument("--fetch-universe", action="store_true",
                    help="Fetch full Polymarket universe (slow, ~30s) instead of keyword search")
    args = ap.parse_args()

    print(f"[recon] starting Polymarket cross-venue match "
          f"(positions={len(KALSHI_POSITIONS)})", file=sys.stderr)

    # Step 1: Collect Polymarket candidates
    all_poly_raw = []
    if args.fetch_universe:
        print("[recon] fetching full active-markets universe...", file=sys.stderr)
        all_poly_raw = gamma_fetch_all_active()
    else:
        # Keyword-driven search: union across all search terms
        seen_cids = set()
        for kp in KALSHI_POSITIONS:
            for term in kp["search_terms"]:
                hits = gamma_search_markets(term, limit=100)
                for h in hits:
                    cid = h.get("conditionId") or h.get("condition_id") or h.get("id")
                    if cid and cid not in seen_cids:
                        seen_cids.add(cid)
                        all_poly_raw.append(h)
                print(f"  [search] term={term!r}: {len(hits)} hits", file=sys.stderr)

    print(f"[recon] total unique Polymarket markets: {len(all_poly_raw)}", file=sys.stderr)

    # Step 2: Flatten
    all_poly: List[PolyMarket] = []
    for raw in all_poly_raw:
        pm = extract_poly_market(raw)
        if pm:
            all_poly.append(pm)
    print(f"[recon] flattened into {len(all_poly)} PolyMarket records", file=sys.stderr)

    # Step 3: Enrich with prices
    print(f"[recon] enriching prices via CLOB...", file=sys.stderr)
    enrich_prices(all_poly)

    # Step 4: Match
    candidates = find_matches(KALSHI_POSITIONS, all_poly)
    print(f"[recon] found {len(candidates)} match candidates", file=sys.stderr)

    # Debug: if 0 matches, dump samples so we can diagnose schema/keyword issues
    if len(candidates) == 0 and all_poly:
        print("", file=sys.stderr)
        print("[debug] ZERO MATCHES — dumping diagnostic info", file=sys.stderr)
        # Ad-hoc keyword scan across ALL markets' _searchable text
        debug_keywords = ["iran", "hormuz", "ufo", "uap", "alien"]
        for kw in debug_keywords:
            hits = [m for m in all_poly if kw in m._searchable]
            print(f"  [debug] keyword={kw!r}: {len(hits)} markets contain this term",
                  file=sys.stderr)
            for h in hits[:3]:
                print(f"      - {h.slug[:60]}  |  {h.question[:70]}",
                      file=sys.stderr)
        print("", file=sys.stderr)
        print("[debug] First 3 extracted PolyMarket records (schema sanity):",
              file=sys.stderr)
        for m in all_poly[:3]:
            print(f"  condition_id={m.condition_id[:30]}  active={m.active}  "
                  f"closed={m.closed}", file=sys.stderr)
            print(f"    question: {m.question[:80]}", file=sys.stderr)
            print(f"    slug: {m.slug[:60]}", file=sys.stderr)
            print(f"    tags: {m.tags[:5]}", file=sys.stderr)
            print(f"    searchable_len: {len(m._searchable)}", file=sys.stderr)
        print("", file=sys.stderr)

    # Step 5: Write outputs
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    here = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(here, f"polymarket_recon_{ts}.json")
    md_path = os.path.join(here, f"polymarket_recon_{ts}.md")
    write_json(candidates, all_poly, json_path)
    write_markdown(candidates, all_poly, md_path, args.spread_min)

    # Terminal summary
    print("")
    print("=" * 80)
    print("POLYMARKET RECON — COMPLETE")
    print("=" * 80)
    print(f"  Universe:    {len(all_poly)} Polymarket markets")
    print(f"  Candidates:  {len(candidates)} matches to Kalshi positions")
    arb_worthy = [c for c in candidates if c.spread_dollars >= args.spread_min]
    print(f"  Arb-worthy:  {len(arb_worthy)} (spread >= ${args.spread_min:.3f})")
    print("")
    if arb_worthy:
        print("  Top 5 arb candidates:")
        for c in arb_worthy[:5]:
            print(f"    [{c.match_confidence}] {c.kalshi_thesis} <-> {c.poly_slug[:50]}")
            print(f"        spread ${c.spread_dollars:.3f} ({c.spread_direction})  poly vol ${c.poly_volume:,.0f}")
    print("")
    print(f"  JSON:  {json_path}")
    print(f"  MD:    {md_path}")


if __name__ == "__main__":
    main()
