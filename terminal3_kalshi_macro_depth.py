"""Terminal 3 — Kalshi Macro Event Liquidity Snapshot.

Purpose: answer the liquidity audit question — what's real TOB depth on Kalshi
macro release markets? A one-time snapshot across CPI, PCE, Jobs (NFP),
unemployment rate, GDP, and Fed decision event families.

This is NOT a logger. Kalshi does not expose historical orderbook depth, so
this script tells you what depth looks like RIGHT NOW for currently open macro
events. If you want longitudinal depth, pair this with the macro logger
(forthcoming) running over a full release cycle.

Output: a single JSONL file + a readable summary to stdout.
    ~/Documents/terminal3_data/kalshi_macro_depth_{YYYY-MM-DD_HHMM}.jsonl

Usage:
    mkdir -p ~/Documents/terminal3_data
    python3 ~/Documents/terminal3_kalshi_macro_depth.py

No auth required. Uses public /events, /markets, /orderbook endpoints.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests


BASE = "https://api.elections.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT = 30
PAGE_LIMIT = 200

# Candidate series tickers for macro events on Kalshi. We probe each one; if
# the series doesn't exist the endpoint returns empty and we move on. The
# exact ticker grammar has shifted over time, so we cast a wide net.
MACRO_SERIES_CANDIDATES = [
    # Inflation
    "KXCPI", "KXCPIYOY", "KXCORECPI", "KXCORECPIYOY",
    "KXPCE", "KXCOREPCE", "KXPCEYOY",
    # Jobs
    "KXPAYROLLS", "KXNFP", "KXUNRATE", "KXUNEMPLOYMENT",
    "KXJOBLESSCLAIMS", "KXINITIALCLAIMS",
    # Growth
    "KXGDP", "KXGDPQOQ",
    # Fed
    "KXFED", "KXFEDDECISION", "KXFEDRATE",
    # Misc market-moving
    "KXRETAILSALES", "KXPPI",
]

OUTPUT_DIR = Path.home() / "Documents" / "terminal3_data"


def fetch_events_for_series(series_ticker: str, status: str = "open") -> List[dict]:
    """List events for a series. Returns [] if series doesn't exist."""
    out: List[dict] = []
    cursor = None
    while True:
        params: Dict[str, object] = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{BASE}/events", params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            return out
        if r.status_code != 200:
            return out
        body = r.json()
        out.extend(body.get("events", []) or [])
        cursor = body.get("cursor")
        if not cursor:
            break
    return out


def fetch_markets_for_event(event_ticker: str) -> List[dict]:
    """Fetch all markets in an event."""
    try:
        r = requests.get(
            f"{BASE}/markets",
            params={"event_ticker": event_ticker, "limit": PAGE_LIMIT},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return r.json().get("markets", []) or []


def fetch_orderbook(ticker: str) -> Optional[dict]:
    """Fetch full orderbook. Real response schema:
        {"orderbook_fp": {"yes_dollars": [[price_str, size_str], ...],
                          "no_dollars":  [[price_str, size_str], ...]}}
    Prices are string dollars ("0.0100" = 1¢). Sizes are string contract counts.
    """
    try:
        r = requests.get(
            f"{BASE}/markets/{ticker}/orderbook",
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    body = r.json()
    return body.get("orderbook_fp") or body.get("orderbook")


def _levels_to_cents(levels):
    """Parse Kalshi orderbook level list. Input: [["0.0100", "11900.00"], ...].
    Output: list of (price_cents_int, size_int) sorted ascending by price.
    Handles both dollar-string and cent-int formats defensively.
    """
    out = []
    for lvl in levels or []:
        try:
            p_raw, s_raw = lvl[0], lvl[1]
            p = float(p_raw) if isinstance(p_raw, str) else float(p_raw)
            s = float(s_raw) if isinstance(s_raw, str) else float(s_raw)
            # If price already in cents (>=1), keep; if dollars (<1), convert.
            price_cents = int(round(p * 100)) if p < 2 else int(round(p))
            size = int(round(s))
            if 0 < price_cents < 100:
                out.append((price_cents, size))
        except (ValueError, TypeError, IndexError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def _meaningful_best(levels_asc, tail_threshold=5, min_size=1):
    """Return the best (highest-price) bid that isn't just MM tail spam.

    Ignores bids at or below tail_threshold (cents) if there's a real bid
    above. Falls back to the absolute max if the whole book is tail.
    """
    real = [(p, s) for (p, s) in levels_asc if p > tail_threshold and s >= min_size]
    if real:
        return max(real, key=lambda x: x[0])
    if levels_asc:
        return max(levels_asc, key=lambda x: x[0])
    return None


def summarize_book(ob: Optional[dict], market: Optional[dict] = None) -> dict:
    """Compute depth metrics.

    Real Kalshi schema: {"yes_dollars": [[price_str, size_str], ...],
                         "no_dollars":  [...]}
    Prices are dollar-denominated strings; sizes are contract counts.

    Metrics:
      - yes_tob_*, no_tob_*: naive best bid (may be MM tail spam)
      - yes_real_*, no_real_*: best bid excluding 1-5¢ tail
      - spread_raw_cents: naive TOB spread
      - spread_real_cents: TOB spread excluding tails
      - yes_depth_5c, no_depth_5c: contracts within 5¢ of the *real* best bid
    """
    out = {
        "yes_tob_price": None, "yes_tob_size": 0,
        "no_tob_price": None, "no_tob_size": 0,
        "yes_real_price": None, "yes_real_size": 0,
        "no_real_price": None, "no_real_size": 0,
        "yes_depth_5c": 0, "no_depth_5c": 0,
        "yes_total": 0, "no_total": 0,
        "yes_levels": 0, "no_levels": 0,
        "implied_mid": None,
        "spread_raw_cents": None,
        "spread_real_cents": None,
        "depth_source": "none",
    }

    if ob:
        yes_raw = ob.get("yes_dollars") or ob.get("yes") or []
        no_raw = ob.get("no_dollars") or ob.get("no") or []
        yes = _levels_to_cents(yes_raw)
        no = _levels_to_cents(no_raw)

        if yes or no:
            out["depth_source"] = "orderbook"

        yes_tob = max(yes, key=lambda x: x[0]) if yes else None
        no_tob = max(no, key=lambda x: x[0]) if no else None
        yes_real = _meaningful_best(yes)
        no_real = _meaningful_best(no)

        def depth_within(levels, ref_price, distance):
            if ref_price is None:
                return 0
            return sum(s for (p, s) in levels if abs(p - ref_price) <= distance)

        if yes_tob:
            out["yes_tob_price"], out["yes_tob_size"] = yes_tob
        if no_tob:
            out["no_tob_price"], out["no_tob_size"] = no_tob
        if yes_real:
            out["yes_real_price"], out["yes_real_size"] = yes_real
        if no_real:
            out["no_real_price"], out["no_real_size"] = no_real

        out["yes_depth_5c"] = depth_within(yes, out["yes_real_price"], 5)
        out["no_depth_5c"] = depth_within(no, out["no_real_price"], 5)
        out["yes_total"] = sum(s for _, s in yes)
        out["no_total"] = sum(s for _, s in no)
        out["yes_levels"] = len(yes)
        out["no_levels"] = len(no)

    if out["yes_tob_price"] is not None and out["no_tob_price"] is not None:
        out["spread_raw_cents"] = 100 - out["no_tob_price"] - out["yes_tob_price"]
    if out["yes_real_price"] is not None and out["no_real_price"] is not None:
        out["spread_real_cents"] = 100 - out["no_real_price"] - out["yes_real_price"]
        yes_ask_implied = 100 - out["no_real_price"]
        out["implied_mid"] = (out["yes_real_price"] + yes_ask_implied) / 2.0

    return out


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out_path = OUTPUT_DIR / f"kalshi_macro_depth_{stamp}.jsonl"

    print(f"[Terminal 3 — Macro Depth Snapshot] {stamp} UTC")
    print(f"Output: {out_path}")
    print("=" * 70)

    total_series_hit = 0
    total_events = 0
    total_markets = 0
    by_series_summary: Dict[str, dict] = {}

    with open(out_path, "w") as f:
        for series in MACRO_SERIES_CANDIDATES:
            events_open = fetch_events_for_series(series, status="open")
            events_pending = fetch_events_for_series(series, status="initialized")
            events = events_open + events_pending
            if not events:
                continue
            total_series_hit += 1
            total_events += len(events)

            series_yes_tob_total = 0
            series_markets = 0
            series_spreads = []

            print(f"\n[{series}] {len(events)} event(s) open/pending")
            for ev in events:
                ev_ticker = ev.get("event_ticker")
                ev_title = (ev.get("title") or "")[:60]
                print(f"  event: {ev_ticker}  ({ev_title})")
                markets = fetch_markets_for_event(ev_ticker)
                for m in markets:
                    m_ticker = m.get("ticker")
                    ob = fetch_orderbook(m_ticker)
                    depth = summarize_book(ob, market=m)
                    total_markets += 1
                    series_markets += 1
                    # Track "real" TOB (excludes 1-5¢ MM tail spam)
                    if depth["yes_real_size"]:
                        series_yes_tob_total += depth["yes_real_size"]
                    if depth["spread_real_cents"] is not None:
                        series_spreads.append(depth["spread_real_cents"])
                    row = {
                        "snapshot_ts_utc": datetime.now(timezone.utc).isoformat(),
                        "series_ticker": series,
                        "event_ticker": ev_ticker,
                        "event_title": ev.get("title"),
                        "market_ticker": m_ticker,
                        "market_subtitle": m.get("subtitle") or m.get("yes_sub_title"),
                        "close_time": m.get("close_time"),
                        "volume_total": m.get("volume", 0),
                        "volume_24h": m.get("volume_24h", 0),
                        "open_interest": m.get("open_interest", 0),
                        **depth,
                    }
                    f.write(json.dumps(row) + "\n")

            by_series_summary[series] = {
                "events": len(events),
                "markets": series_markets,
                "avg_real_tob_size": (
                    series_yes_tob_total / series_markets if series_markets else 0
                ),
                "median_real_spread_cents": (
                    sorted(series_spreads)[len(series_spreads) // 2]
                    if series_spreads else None
                ),
                "markets_with_real_spread": len(series_spreads),
            }

    print("\n" + "=" * 70)
    print(f"Series hit: {total_series_hit}/{len(MACRO_SERIES_CANDIDATES)}")
    print(f"Events: {total_events}")
    print(f"Markets: {total_markets}")
    print("\n--- PER-SERIES SUMMARY (excl. 1-5¢ MM tail spam) ---")
    print(f"{'series':<25} {'events':>7} {'markets':>8} {'avg_real_tob_sz':>16} {'med_real_spread_¢':>18} {'w/real_spr':>11}")
    for s, info in by_series_summary.items():
        med_spread = info["median_real_spread_cents"]
        med_spread_str = f"{med_spread}" if med_spread is not None else "—"
        print(
            f"{s:<25} {info['events']:>7} {info['markets']:>8} "
            f"{info['avg_real_tob_size']:>16.0f} {med_spread_str:>18} {info['markets_with_real_spread']:>11}"
        )
    print(f"\nWrote: {out_path}")
    print("\nInterpretation guide:")
    print("  avg_real_tob_size = contracts at the best bid ABOVE the 1-5¢ MM-tail zone.")
    print("  med_real_spread_¢ = (100 - NO_real_bid - YES_real_bid). The real bid-ask.")
    print("  Scaling thresholds (capital ceiling per market):")
    print("    spread ≥ 20¢ → untradable (edge gets eaten by fees + spread)")
    print("    spread 10-20¢ → marginal (needs huge edge to overcome)")
    print("    spread 5-10¢ → borderline tradable")
    print("    spread <5¢ → tight, scalable")
    print("  Real answer on scalability: sum(avg_real_tob_size) across all markets × $1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
