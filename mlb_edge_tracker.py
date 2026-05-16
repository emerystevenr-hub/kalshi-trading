"""
MLB EDGE TRACKER  (Phase 1)

For each MLB game on today's slate:
  1. Pull starting lineups from MLB StatsAPI
  2. Pull moneyline markets from Polymarket
  3. Run mlb_model (Elo + HFA) to get P(home wins)
  4. Compare model probability to Polymarket mid price
  5. Log every game to mlb_edge_log.csv for CLV tracking

Rule for PHASE 1: we are NOT betting. We are logging predictions.
Only after 100+ games of logged predictions do we know if we have edge.

The log captures:
    date, game, model_p_home, pm_p_home, edge_pct, bet_side, opened_price,
    closing_price (filled later), result, clv_beat (filled later)

Run this daily. Run it again post-game to backfill closing price + result.
"""

import csv
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

from mlb_model import build_model, predict_home_winprob, STATSAPI

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"

LOG_PATH = os.path.join(os.path.dirname(__file__), "mlb_edge_log.csv")
LOG_FIELDS = [
    "logged_at", "game_date", "home", "away",
    "model_p_home", "pm_p_home", "pm_p_away",
    "edge_pct", "bet_side", "bet_price",
    "token_home", "token_away",
    "closing_p_home", "actual_home_win", "clv_beat",
]

# Minimum disagreement to log as "edge" (paper bet).  Below this, just record.
EDGE_THRESHOLD_PCT = 3.0

# Team-name normalization.  Polymarket titles vary ("Yankees vs Red Sox",
# "NYY vs BOS", "New York Yankees vs Boston Red Sox").  We map all of
# them to the canonical MLB StatsAPI name.
CANONICAL = {
    # AL East
    "yankees": "New York Yankees", "nyy": "New York Yankees",
    "red sox": "Boston Red Sox", "bos": "Boston Red Sox",
    "blue jays": "Toronto Blue Jays", "tor": "Toronto Blue Jays",
    "orioles": "Baltimore Orioles", "bal": "Baltimore Orioles",
    "rays": "Tampa Bay Rays", "tb": "Tampa Bay Rays", "tbr": "Tampa Bay Rays",
    # AL Central
    "guardians": "Cleveland Guardians", "cle": "Cleveland Guardians",
    "tigers": "Detroit Tigers", "det": "Detroit Tigers",
    "royals": "Kansas City Royals", "kc": "Kansas City Royals", "kcr": "Kansas City Royals",
    "twins": "Minnesota Twins", "min": "Minnesota Twins",
    "white sox": "Chicago White Sox", "cws": "Chicago White Sox", "chw": "Chicago White Sox",
    # AL West
    "astros": "Houston Astros", "hou": "Houston Astros",
    "rangers": "Texas Rangers", "tex": "Texas Rangers",
    "mariners": "Seattle Mariners", "sea": "Seattle Mariners",
    "angels": "Los Angeles Angels", "laa": "Los Angeles Angels",
    "athletics": "Oakland Athletics", "oak": "Oakland Athletics",
    "a's": "Oakland Athletics",
    # NL East
    "braves": "Atlanta Braves", "atl": "Atlanta Braves",
    "phillies": "Philadelphia Phillies", "phi": "Philadelphia Phillies",
    "mets": "New York Mets", "nym": "New York Mets",
    "marlins": "Miami Marlins", "mia": "Miami Marlins",
    "nationals": "Washington Nationals", "wsh": "Washington Nationals", "was": "Washington Nationals",
    # NL Central
    "brewers": "Milwaukee Brewers", "mil": "Milwaukee Brewers",
    "cubs": "Chicago Cubs", "chc": "Chicago Cubs",
    "cardinals": "St. Louis Cardinals", "stl": "St. Louis Cardinals",
    "reds": "Cincinnati Reds", "cin": "Cincinnati Reds",
    "pirates": "Pittsburgh Pirates", "pit": "Pittsburgh Pirates",
    # NL West
    "dodgers": "Los Angeles Dodgers", "lad": "Los Angeles Dodgers",
    "giants": "San Francisco Giants", "sf": "San Francisco Giants", "sfg": "San Francisco Giants",
    "padres": "San Diego Padres", "sd": "San Diego Padres", "sdp": "San Diego Padres",
    "rockies": "Colorado Rockies", "col": "Colorado Rockies",
    "diamondbacks": "Arizona Diamondbacks", "ari": "Arizona Diamondbacks",
    "dbacks": "Arizona Diamondbacks", "d-backs": "Arizona Diamondbacks",
}


def canonicalize(name: str) -> Optional[str]:
    if not name:
        return None
    s = name.strip().lower()
    # direct
    if s in CANONICAL:
        return CANONICAL[s]
    # substring
    for k, v in CANONICAL.items():
        if k in s:
            return v
    return None


# ──────────────────────────────────────────────────────────────────────
# MLB SCHEDULE FOR TODAY
# ──────────────────────────────────────────────────────────────────────

def fetch_today_games(d: str) -> List[dict]:
    """Return list of dicts: {home, away, status, game_pk}."""
    r = requests.get(
        f"{STATSAPI}/schedule",
        params={"sportId": 1, "startDate": d, "endDate": d, "gameTypes": "R"},
        timeout=20,
    )
    r.raise_for_status()
    out = []
    for day in r.json().get("dates", []):
        for g in day.get("games", []):
            out.append({
                "game_pk": g.get("gamePk"),
                "home": g["teams"]["home"]["team"]["name"],
                "away": g["teams"]["away"]["team"]["name"],
                "status": g.get("status", {}).get("abstractGameState"),
                "start_utc": g.get("gameDate"),
                "home_score": g["teams"]["home"].get("score"),
                "away_score": g["teams"]["away"].get("score"),
            })
    return out


# ──────────────────────────────────────────────────────────────────────
# POLYMARKET MLB MARKETS
# ──────────────────────────────────────────────────────────────────────

def fetch_polymarket_mlb_events() -> List[dict]:
    """All active events with baseball/mlb tag or 'vs' title."""
    events = []
    for offset in range(0, 1500, 100):
        params = {"active": "true", "closed": "false", "limit": 100, "offset": offset}
        try:
            r = requests.get(GAMMA_URL, params=params, timeout=15)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            events.extend(batch)
        except Exception as e:
            print(f"  ⚠️  gamma fetch failed offset={offset}: {e}")
            break
    mlb = []
    for e in events:
        title = e.get("title", "").lower()
        tags = [t.get("label", "").lower() for t in e.get("tags", [])]
        is_baseball = "baseball" in tags or "mlb" in tags
        has_vs = " vs " in title or " vs. " in title
        if is_baseball or (has_vs and ("mlb" in title or "baseball" in title
                                        or any(k in title for k in CANONICAL))):
            mlb.append(e)
    return mlb


def fetch_mid_prices(token_ids: List[str]) -> Dict[str, float]:
    """Return token_id → mid price from CLOB. Falls back to 0.0 on failure."""
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
        return {t: 0.0 for t in token_ids}
    # resp is dict token_id → {"BUY": px, "SELL": px}
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
        else:
            out[t] = 0.0
    return out


def parse_mlb_market(event: dict) -> Optional[dict]:
    """
    Return {home, away, token_home, token_away} from an event whose title
    looks like 'Team A vs Team B …'. Convention: title reads AWAY vs HOME
    for US sports (but not always — we reconcile against outcomes array).
    """
    title = event.get("title", "")
    markets = event.get("markets", [])
    if not markets:
        return None
    m = markets[0]  # moneyline is the first/only market on per-game events
    tokens = m.get("clobTokenIds", [])
    outcomes = m.get("outcomes", [])
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if len(tokens) < 2 or len(outcomes) < 2:
        return None

    t1, t2 = canonicalize(outcomes[0]), canonicalize(outcomes[1])
    if t1 is None or t2 is None:
        return None

    # Figure out home/away from title order. Polymarket usually lists
    # "Away vs Home". Verify by matching canonicals.
    lower = title.lower()
    i1 = lower.find(outcomes[0].lower())
    i2 = lower.find(outcomes[1].lower())
    if i1 == -1 or i2 == -1:
        # fallback: assume first outcome is away (Polymarket convention)
        away, home = t1, t2
        tok_away, tok_home = tokens[0], tokens[1]
    elif i1 < i2:
        # outcome[0] appears first → it's the away team
        away, home = t1, t2
        tok_away, tok_home = tokens[0], tokens[1]
    else:
        away, home = t2, t1
        tok_away, tok_home = tokens[1], tokens[0]

    return {
        "title": title,
        "home": home, "away": away,
        "token_home": tok_home, "token_away": tok_away,
        "event_id": event.get("id"),
    }


# ──────────────────────────────────────────────────────────────────────
# CORE TRACKER
# ──────────────────────────────────────────────────────────────────────

def ensure_log():
    if not os.path.exists(LOG_PATH):
        with open(LOG_PATH, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            w.writeheader()


def append_log(row: dict):
    ensure_log()
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writerow({k: row.get(k, "") for k in LOG_FIELDS})


def scan(target_date: Optional[str] = None, dry_log: bool = False):
    d = target_date or date.today().isoformat()
    print("=" * 78)
    print(f"MLB EDGE SCAN — {d}")
    print("=" * 78)

    # 1. today's slate
    slate = fetch_today_games(d)
    print(f"MLB slate: {len(slate)} games")
    if not slate:
        print("no games today. exiting.")
        return

    # 2. build model through yesterday
    yesterday = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
    model = build_model(start="2025-03-27", end=yesterday, verbose=True)

    # 3. pull all Polymarket MLB markets
    pm_events = fetch_polymarket_mlb_events()
    print(f"Polymarket MLB-ish events: {len(pm_events)}")
    pm_parsed = [p for p in (parse_mlb_market(e) for e in pm_events) if p]
    print(f"parsed to team matchups: {len(pm_parsed)}")

    # index by (home, away)
    pm_by_key = {(p["home"], p["away"]): p for p in pm_parsed}

    # 4. for each game, match + compute edge
    unmatched = []
    all_tokens = []
    matched_rows = []
    for g in slate:
        key = (g["home"], g["away"])
        pm = pm_by_key.get(key)
        if pm is None:
            unmatched.append(g)
            continue
        all_tokens.extend([pm["token_home"], pm["token_away"]])
        matched_rows.append((g, pm))

    # batch price pull
    prices = fetch_mid_prices(all_tokens)

    print(f"\n{'MATCHUP':<40s} {'MODEL':>7s} {'PM':>6s} {'EDGE':>8s} {'SIDE':>6s}")
    print("-" * 78)

    for g, pm in matched_rows:
        p_model = predict_home_winprob(model, pm["home"], pm["away"])
        p_pm_home = prices.get(pm["token_home"], 0.0)
        p_pm_away = prices.get(pm["token_away"], 0.0)

        if p_model is None or p_pm_home <= 0:
            print(f"{pm['away']:<18s} @ {pm['home']:<18s}  NO PM PRICE")
            continue

        edge_pct = (p_model - p_pm_home) * 100.0  # positive → model says home undervalued
        if abs(edge_pct) < EDGE_THRESHOLD_PCT:
            bet_side = "-"
            bet_price = ""
        elif edge_pct > 0:
            bet_side = "HOME"
            bet_price = f"{p_pm_home:.4f}"
        else:
            bet_side = "AWAY"
            bet_price = f"{p_pm_away:.4f}"

        print(f"{pm['away']:<18s} @ {pm['home']:<18s}  "
              f"{p_model:>6.3f}  {p_pm_home:>6.3f}  {edge_pct:>+7.2f}%  {bet_side:>6s}")

        append_log({
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "game_date": d,
            "home": pm["home"],
            "away": pm["away"],
            "model_p_home": f"{p_model:.4f}",
            "pm_p_home": f"{p_pm_home:.4f}",
            "pm_p_away": f"{p_pm_away:.4f}",
            "edge_pct": f"{edge_pct:.2f}",
            "bet_side": bet_side,
            "bet_price": bet_price,
            "token_home": pm["token_home"],
            "token_away": pm["token_away"],
            "closing_p_home": "",
            "actual_home_win": "",
            "clv_beat": "",
        })

    if unmatched:
        print(f"\n{len(unmatched)} MLB games without a Polymarket market:")
        for g in unmatched:
            print(f"  {g['away']} @ {g['home']}")

    print(f"\nlog: {LOG_PATH}")
    print("Run again post-game with --backfill to record closing prices & results.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--date":
        scan(target_date=sys.argv[2])
    else:
        scan()
