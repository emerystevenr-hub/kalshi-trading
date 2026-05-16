"""T7 Kalshi Market Probe — discover daily-close macro markets.

Builds on the pattern in terminal3_kalshi_macro_depth.py. Probes a wide
candidate list of series tickers across rates, commodities, FX, equities,
and crypto. For each hit: lists open events + sample markets + orderbook
depth. Output answers the build-blocker question: do we have liquid
Kalshi markets for 10Y / WTI / DXY (or close substitutes)?

Run: python3 ~/Documents/t7_kalshi_market_probe.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
TIMEOUT = 30
PAGE_LIMIT = 200

# Cast a wide net. Kalshi's ticker grammar shifts; we accept any series
# whose /events endpoint returns at least one open event.
CANDIDATES = [
    # Rates
    "KX10Y", "KX10YR", "KX10YIELD", "KX10YYIELD", "KXUST10",
    "KX2Y", "KX2YIELD", "KX30Y", "KXTSY", "KXTSY10",
    "KXTSYIELD", "KXTNX", "KXBOND",
    # Crude / energy
    "KXWTI", "KXOIL", "KXCRUDE", "KXWTICRUDE", "KXOILPRICE",
    "KXBRENT", "KXBRENTCRUDE", "KXNATGAS", "KXNG",
    # FX / dollar
    "KXDXY", "KXUSD", "KXUSDOLLAR", "KXDOLLAR",
    "KXEURUSD", "KXEUR", "KXUSDJPY", "KXJPY", "KXGBP",
    # Equities
    "KXSPX", "KXSP500", "KXSPY", "KXNDX", "KXNASDAQ",
    "KXNDQ", "KXDOW", "KXDJI",
    # Crypto (highest cadence — both BTC and ETH)
    "KXBTC", "KXBTCD", "KXBTCPRICE", "KXBITCOIN",
    "KXETH", "KXETHD", "KXETHEREUM",
    # Inflation expectations / breakevens
    "KXBREAKEVEN", "KXBREAK10Y",
]


def fetch_events(series: str, status: str) -> List[dict]:
    out: List[dict] = []
    cursor = None
    while True:
        params: Dict[str, object] = {
            "series_ticker": series,
            "status": status,
            "limit": PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{BASE}/events", params=params, timeout=TIMEOUT)
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


def fetch_markets(event_ticker: str) -> List[dict]:
    try:
        r = requests.get(
            f"{BASE}/markets",
            params={"event_ticker": event_ticker, "limit": PAGE_LIMIT},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    return r.json().get("markets", []) or []


def fetch_orderbook(ticker: str) -> Optional[dict]:
    try:
        r = requests.get(f"{BASE}/markets/{ticker}/orderbook", timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return r.json().get("orderbook_fp") or r.json().get("orderbook")


def parse_levels(levels) -> List[tuple]:
    out = []
    for lvl in levels or []:
        try:
            p = float(lvl[0])
            s = float(lvl[1])
            pc = int(round(p * 100)) if p < 2 else int(round(p))
            if 0 < pc < 100:
                out.append((pc, int(s)))
        except (ValueError, TypeError, IndexError):
            continue
    out.sort()
    return out


def main() -> int:
    print(f"[T7 Kalshi Macro Probe] {datetime.now(timezone.utc).isoformat()}")
    print(f"Probing {len(CANDIDATES)} candidate series tickers...")
    print("=" * 80)

    hits = []
    for series in CANDIDATES:
        events = fetch_events(series, "open")
        if not events:
            continue
        hits.append((series, events))

    if not hits:
        print("\nNO HITS. None of the candidate series tickers returned open events.")
        print("Either Kalshi has retired these markets or naming has shifted.")
        return 1

    print(f"\nHITS: {len(hits)} series with open events\n")

    for series, events in hits:
        print(f"\n[{series}]  {len(events)} open event(s)")
        for ev in events[:3]:  # cap at 3 events per series for readability
            ev_ticker = ev.get("event_ticker")
            title = (ev.get("title") or "")[:70]
            close_time = ev.get("close_time") or ev.get("expected_expiration_time") or "?"
            print(f"  {ev_ticker}  | {title}  | closes={close_time}")
            markets = fetch_markets(ev_ticker)
            # Sample up to 3 markets per event for depth
            sample = markets[:3]
            for m in sample:
                m_ticker = m.get("ticker")
                vol_24h = m.get("volume_24h", 0)
                vol_total = m.get("volume", 0)
                ob = fetch_orderbook(m_ticker) or {}
                yes_levels = parse_levels(ob.get("yes_dollars") or ob.get("yes") or [])
                no_levels = parse_levels(ob.get("no_dollars") or ob.get("no") or [])
                yes_top = max(yes_levels, key=lambda x: x[0]) if yes_levels else None
                no_top = max(no_levels, key=lambda x: x[0]) if no_levels else None
                yes_str = f"{yes_top[0]}¢×{yes_top[1]}" if yes_top else "—"
                no_str = f"{no_top[0]}¢×{no_top[1]}" if no_top else "—"
                spread = (
                    100 - yes_top[0] - no_top[0]
                    if (yes_top and no_top) else None
                )
                spread_str = f"{spread}¢" if spread is not None else "—"
                subtitle = (m.get("subtitle") or m.get("yes_sub_title") or "")[:50]
                print(f"    {m_ticker}  YES={yes_str:<10} NO={no_str:<10} "
                      f"spr={spread_str:<5} vol24h={vol_24h:<6} "
                      f"vol_total={vol_total:<8} | {subtitle}")
            if len(markets) > 3:
                print(f"    (+{len(markets)-3} more markets in event)")

    print("\n" + "=" * 80)
    print(f"Series with open events: {[h[0] for h in hits]}")
    print("\nLiquidity bar for T7:")
    print("  avg yes top-of-book size ≥ 25 contracts AND median spread ≤ 5¢ → BUILD")
    print("  size 10-25 OR spread 5-10¢ → marginal, build only with conservative sizing")
    print("  size <10 OR spread >10¢ → skip this asset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
