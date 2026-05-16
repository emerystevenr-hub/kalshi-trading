"""
MLB FUTURES EDGE SCANNER  (Phase 1 — revised)

Polymarket only lists MLB futures markets (no daily games), so our model
target is the 2026 World Series champion market.

Flow:
  1. Build Elo ratings from all 2025+2026 regular-season games
  2. Fetch current 2026 standings + remaining schedule from MLB StatsAPI
  3. Run a 10,000-season Monte Carlo (Elo-driven) → team → P(WS win)
  4. Fetch Polymarket's 'MLB World Series Champion 2026' sub-markets
  5. Compute edge per team: model_p - market_p
  6. Rank and log

We are NOT betting. Phase 1 = prove signal exists across a full season.
If pure-Elo sim disagrees with Polymarket by >3% on multiple teams AND
the direction is consistent with known team strengths, the model has edge
and we can add starting-pitcher + injury overlays to sharpen it.
"""

import csv
import json
import os
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional

import requests

from mlb_model import (
    build_model, fetch_current_standings, fetch_remaining_schedule,
    simulate_season,
)

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"
WS_EVENT_SLUG = "mlb-world-series-champion-2026"

LOG_PATH = os.path.join(os.path.dirname(__file__), "mlb_futures_edge_log.csv")
LOG_FIELDS = [
    "logged_at", "team", "model_p_ws", "market_p_ws",
    "edge_pct", "bet_side", "token_yes", "token_no",
    "fair_kelly_pct",
]

# Minimum disagreement (absolute percentage points) to flag
EDGE_FLAG_PCT = 3.0


# ──────────────────────────────────────────────────────────────────────
# TEAM NAME CANONICALIZATION (WS market question → model key)
# ──────────────────────────────────────────────────────────────────────

# The WS market asks "Will the [Team Name] win the 2026 World Series?"
# MLB StatsAPI names we need to match:
WS_TO_STATSAPI = {
    "New York Yankees": "New York Yankees",
    "Toronto Blue Jays": "Toronto Blue Jays",
    "Tampa Bay Rays": "Tampa Bay Rays",
    "Baltimore Orioles": "Baltimore Orioles",
    "Boston Red Sox": "Boston Red Sox",
    "Cleveland Guardians": "Cleveland Guardians",
    "Chicago White Sox": "Chicago White Sox",
    "Minnesota Twins": "Minnesota Twins",
    "Detroit Tigers": "Detroit Tigers",
    "Kansas City Royals": "Kansas City Royals",
    "Houston Astros": "Houston Astros",
    "Seattle Mariners": "Seattle Mariners",
    "Los Angeles Angels": "Los Angeles Angels",
    "Texas Rangers": "Texas Rangers",
    "Athletics": "Athletics",
    "Atlanta Braves": "Atlanta Braves",
    "New York Mets": "New York Mets",
    "Philadelphia Phillies": "Philadelphia Phillies",
    "Miami Marlins": "Miami Marlins",
    "Washington Nationals": "Washington Nationals",
    "St. Louis Cardinals": "St. Louis Cardinals",
    "Milwaukee Brewers": "Milwaukee Brewers",
    "Chicago Cubs": "Chicago Cubs",
    "Cincinnati Reds": "Cincinnati Reds",
    "Pittsburgh Pirates": "Pittsburgh Pirates",
    "Los Angeles Dodgers": "Los Angeles Dodgers",
    "San Diego Padres": "San Diego Padres",
    "San Francisco Giants": "San Francisco Giants",
    "Arizona Diamondbacks": "Arizona Diamondbacks",
    "Colorado Rockies": "Colorado Rockies",
}


def extract_team(question: str) -> Optional[str]:
    """Parse 'Will the X win the 2026 World Series?' → X."""
    q = question.strip()
    prefix = "Will the "
    suffix_candidates = [" win the 2026 World Series?", " win the World Series?"]
    if not q.startswith(prefix):
        return None
    for suf in suffix_candidates:
        if q.endswith(suf):
            return q[len(prefix):-len(suf)]
    return None


# ──────────────────────────────────────────────────────────────────────
# MARKET FETCH
# ──────────────────────────────────────────────────────────────────────

def fetch_ws_event() -> Optional[dict]:
    """Pull the 2026 WS champion event by slug (search if needed)."""
    # Direct slug endpoint first
    try:
        r = requests.get(f"{GAMMA_URL}", params={"slug": WS_EVENT_SLUG}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
    except Exception:
        pass
    # Fallback: scan active events
    for offset in range(0, 1500, 100):
        try:
            r = requests.get(GAMMA_URL, params={
                "active": "true", "closed": "false",
                "limit": 100, "offset": offset,
            }, timeout=15)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for e in batch:
                if e.get("slug") == WS_EVENT_SLUG:
                    return e
        except Exception as err:
            print(f"  ⚠️  scan fetch failed: {err}")
            break
    return None


def fetch_mid_prices(token_ids: List[str]) -> Dict[str, float]:
    if not token_ids:
        return {}
    payload = [{"token_id": t, "side": s} for t in token_ids for s in ("buy", "sell")]
    try:
        r = requests.post(f"{CLOB_BASE}/prices", json=payload, timeout=15,
                          headers={"Content-Type": "application/json"})
        r.raise_for_status()
        resp = r.json()
    except Exception as e:
        print(f"  ⚠️  CLOB prices fetch failed: {e}")
        return {}
    out = {}
    for t in token_ids:
        entry = resp.get(t, {})
        b = float(entry.get("BUY", 0) or 0)
        s = float(entry.get("SELL", 0) or 0)
        if b > 0 and s > 0:
            out[t] = (b + s) / 2.0
        elif b > 0:
            out[t] = b
        elif s > 0:
            out[t] = s
    return out


# ──────────────────────────────────────────────────────────────────────
# KELLY SIZING (full-Kelly fraction of bankroll — we'll apply 0.25x later)
# ──────────────────────────────────────────────────────────────────────

def kelly_pct(p_model: float, market_price: float) -> float:
    """For a bet on YES at price=market_price, bankroll-fraction Kelly."""
    if market_price <= 0 or market_price >= 1:
        return 0.0
    q = 1 - p_model
    b = (1 - market_price) / market_price  # net odds
    f = (b * p_model - q) / b
    return max(0.0, f)


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────

def ensure_log():
    if not os.path.exists(LOG_PATH):
        with open(LOG_PATH, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writeheader()


def append_log(row: dict):
    ensure_log()
    with open(LOG_PATH, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=LOG_FIELDS).writerow(
            {k: row.get(k, "") for k in LOG_FIELDS})


def run(n_sims: int = 10000, verbose: bool = True):
    print("=" * 78)
    print(f"MLB FUTURES EDGE — 2026 World Series  ({date.today().isoformat()})")
    print("=" * 78)

    # 1. build Elo through yesterday
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    model = build_model(start="2025-03-27", end=yesterday, verbose=verbose)
    if len(model) < 25:
        print("⚠️  fewer than 25 teams in model — aborting")
        return

    # 2. standings + remaining schedule
    print("Fetching 2026 standings...")
    standings = fetch_current_standings(season=2026)
    print(f"  {len(standings)} teams in standings")

    print("Fetching remaining 2026 schedule...")
    remaining = fetch_remaining_schedule(season=2026)
    print(f"  {len(remaining)} remaining games")

    # 3. simulate
    print(f"Running {n_sims:,}-season Monte Carlo...")
    sim = simulate_season(model, standings, remaining, n_sims=n_sims)

    # 4. fetch Polymarket WS market
    print("Fetching Polymarket WS market...")
    event = fetch_ws_event()
    if not event:
        print("⚠️  WS event not found on Polymarket")
        return
    sub_markets = event.get("markets", [])
    print(f"  {len(sub_markets)} WS sub-markets")

    rows = []
    all_tokens = []
    team_to_market = {}
    for m in sub_markets:
        q = m.get("question") or m.get("title") or ""
        team = extract_team(q)
        if not team:
            continue
        tokens = m.get("clobTokenIds")
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        if not tokens or len(tokens) < 2:
            continue
        team_to_market[team] = {"token_yes": tokens[0], "token_no": tokens[1]}
        all_tokens.append(tokens[0])

    prices = fetch_mid_prices(all_tokens)

    # 5. compute edge
    now = datetime.now(timezone.utc).isoformat()
    results = []
    for team, mk in team_to_market.items():
        stats_team = WS_TO_STATSAPI.get(team)
        if not stats_team or stats_team not in sim:
            print(f"  ⚠️  unmatched: {team}")
            continue
        p_model = sim[stats_team]["ws"]
        p_market = prices.get(mk["token_yes"], 0.0)
        if p_market <= 0:
            continue
        edge_pct = (p_model - p_market) * 100.0
        side = "YES" if edge_pct > 0 else "NO"
        f_kelly = kelly_pct(p_model if edge_pct > 0 else (1 - p_model),
                            p_market if edge_pct > 0 else (1 - p_market))
        results.append({
            "team": team,
            "model_p_ws": p_model,
            "market_p_ws": p_market,
            "edge_pct": edge_pct,
            "bet_side": side if abs(edge_pct) >= EDGE_FLAG_PCT else "-",
            "token_yes": mk["token_yes"],
            "token_no": mk["token_no"],
            "fair_kelly_pct": f_kelly * 100.0,
        })

    results.sort(key=lambda r: -abs(r["edge_pct"]))

    print(f"\n{'TEAM':<25s} {'MODEL':>7s} {'MARKET':>8s} {'EDGE':>8s} {'SIDE':>5s} {'KELLY%':>7s}")
    print("-" * 78)
    for r in results:
        flag = "  ⭐" if abs(r["edge_pct"]) >= EDGE_FLAG_PCT else ""
        print(f"{r['team']:<25s} {r['model_p_ws']:>6.3f}  {r['market_p_ws']:>7.3f}  "
              f"{r['edge_pct']:>+7.2f}%  {r['bet_side']:>4s}  {r['fair_kelly_pct']:>6.2f}%{flag}")
        append_log({"logged_at": now, **r})

    print(f"\nlog: {LOG_PATH}")
    print(f"⭐ = edge ≥ {EDGE_FLAG_PCT}% absolute. Kelly% shown is FULL kelly — apply 0.25x for sizing.")


if __name__ == "__main__":
    import sys
    n = 10000
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        n = int(sys.argv[1])
    run(n_sims=n)
