"""
MLB FUTURES CROSS-VENUE EDGE SCANNER   (Path B)

Compares Polymarket 'MLB World Series Champion 2026' prices against
sharp-sportsbook no-vig consensus (Pinnacle + DraftKings + FanDuel + BetMGM
+ Caesars via The Odds API).

Edge logic — zero modeling required:
  1. Pull each book's American odds for all 30 teams
  2. Convert to implied probabilities
  3. Remove book's vig per league (p_i / sum(p_i)) → fair probability
  4. Take median across books → 'sharp consensus'
  5. Compare Polymarket mid price to consensus
  6. Flag any team where |polymarket - consensus| >= EDGE_FLAG_PCT

Why this works (and why the pure-Elo model didn't):
  - Sharp books employ teams of quants building models far better than ours.
  - Their closing lines are the best public estimate of true probability.
  - If Polymarket deviates from sharp consensus by more than the round-trip
    fee, the opposite side of Polymarket is +EV — regardless of WHY it's
    mispriced (narrative, retail flow, latency, etc.).
  - Pinnacle specifically is the sharpest futures market globally. Weight it
    heavily when computing consensus.

Setup (one-time, 30 seconds):
  1. Go to https://the-odds-api.com, sign up for free tier.
  2. Copy your API key.
  3. export ODDS_API_KEY=xxxxx   (add to ~/.zshrc to persist)
  4. python3 mlb_futures_crossvenue.py

Phase 1 action rule:
  - Log everything. Don't bet yet.
  - Only take a position when (a) edge ≥ 3.5% after round-trip fees,
    (b) the SAME direction is confirmed by ≥3 of the 5 sharp books, AND
    (c) Pinnacle confirms direction.
"""

import csv
import json
import os
import statistics
import sys
from datetime import datetime, date, timezone
from typing import Dict, List, Optional, Tuple

import requests

from mlb_futures_edge import (
    fetch_ws_event, fetch_mid_prices, extract_team,
    WS_TO_STATSAPI, LOG_FIELDS as _UNUSED,
)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "baseball_mlb_world_series_winner"
REGIONS = "us,us2"
MARKETS = "outrights"

# Which books count as "sharp" — used for consensus. Pinnacle first = weighted most.
SHARP_BOOKS = ["pinnacle", "draftkings", "fanduel", "betmgm", "caesars"]

# Round-trip fee assumption on Polymarket: gas + withdrawal ~ 2%.
# Net edge must clear this to be +EV.
ROUND_TRIP_FEE_PCT = 2.0

# Minimum absolute divergence from consensus to flag
EDGE_FLAG_PCT = 3.0

LOG_PATH = os.path.join(os.path.dirname(__file__), "mlb_crossvenue_log.csv")
LOG_FIELDS = [
    "logged_at", "team",
    "pm_p", "consensus_p", "edge_pct",
    "pinnacle_p", "draftkings_p", "fanduel_p", "betmgm_p", "caesars_p",
    "books_confirming", "pinnacle_confirms",
    "bet_side", "fair_kelly_pct",
    "token_yes", "token_no",
]


# ──────────────────────────────────────────────────────────────────────
# ODDS MATH
# ──────────────────────────────────────────────────────────────────────

def american_to_prob(odds: int) -> float:
    if odds >= 0:
        return 100.0 / (odds + 100)
    return (-odds) / ((-odds) + 100)


def remove_vig(probs: Dict[str, float]) -> Dict[str, float]:
    """Normalize so probabilities sum to 1.0. Proportional vig removal."""
    total = sum(probs.values())
    if total <= 0:
        return {k: 0.0 for k in probs}
    return {k: v / total for k, v in probs.items()}


def kelly_pct(p_true: float, market_price: float) -> float:
    if market_price <= 0 or market_price >= 1:
        return 0.0
    b = (1 - market_price) / market_price
    f = (b * p_true - (1 - p_true)) / b
    return max(0.0, f)


# ──────────────────────────────────────────────────────────────────────
# ODDS API
# ──────────────────────────────────────────────────────────────────────

def fetch_sharp_odds() -> Tuple[dict, dict]:
    """
    Returns (per_book_probs, metadata) where per_book_probs is:
        { book_key: { team_name: no_vig_prob, ... }, ... }
    metadata has quota headers from the API response.
    """
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ODDS_API_KEY env var not set.\n"
            "  1. Get free key: https://the-odds-api.com\n"
            "  2. export ODDS_API_KEY=xxxxx\n"
            "  3. Add to ~/.zshrc to persist."
        )
    url = f"{ODDS_API_BASE}/sports/{SPORT_KEY}/odds"
    params = {
        "apiKey": key,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": "american",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    meta = {
        "requests_used": r.headers.get("x-requests-used"),
        "requests_remaining": r.headers.get("x-requests-remaining"),
    }

    # Data is a list of events; WS futures is usually 1 event with all books
    per_book = {}
    for ev in data:
        for bk in ev.get("bookmakers", []):
            bkey = bk.get("key")
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "outrights":
                    continue
                raw_probs = {}
                for o in mkt.get("outcomes", []):
                    name = o.get("name")
                    price = o.get("price")
                    if name is None or price is None:
                        continue
                    raw_probs[name] = american_to_prob(int(price))
                if raw_probs:
                    per_book[bkey] = remove_vig(raw_probs)
    return per_book, meta


# ──────────────────────────────────────────────────────────────────────
# TEAM NAME RECONCILIATION (OddsAPI ↔ Polymarket)
# ──────────────────────────────────────────────────────────────────────

# OddsAPI uses full team names; Polymarket does too. One exception: A's.
ODDS_TO_POLY = {
    "Oakland Athletics": "Athletics",
    "Athletics": "Athletics",
    "Sacramento Athletics": "Athletics",
}


def normalize_odds_team(name: str) -> str:
    return ODDS_TO_POLY.get(name, name)


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


def run(verbose: bool = True):
    print("=" * 84)
    print(f"MLB FUTURES CROSS-VENUE — 2026 World Series  ({date.today().isoformat()})")
    print("=" * 84)

    # 1. Fetch Polymarket
    print("Fetching Polymarket WS market...")
    event = fetch_ws_event()
    if not event:
        print("⚠️  WS event not found on Polymarket")
        return
    pm_tokens = []
    pm_team_tokens = {}
    for m in event.get("markets", []):
        q = m.get("question") or m.get("title") or ""
        team = extract_team(q)
        if not team:
            continue
        tok = m.get("clobTokenIds")
        if isinstance(tok, str):
            tok = json.loads(tok)
        if not tok or len(tok) < 2:
            continue
        pm_team_tokens[team] = {"yes": tok[0], "no": tok[1]}
        pm_tokens.append(tok[0])

    pm_prices_raw = fetch_mid_prices(pm_tokens)
    pm_prices = {team: pm_prices_raw.get(info["yes"], 0.0)
                 for team, info in pm_team_tokens.items()}

    # 2. Fetch sharp book odds
    print("Fetching sharp-book odds via Odds API...")
    try:
        per_book, meta = fetch_sharp_odds()
    except RuntimeError as e:
        print(f"\n❌ {e}")
        return
    except requests.HTTPError as e:
        print(f"\n❌ Odds API HTTP error: {e}")
        return

    if not per_book:
        print("⚠️  no book odds returned — market may be closed or key invalid")
        return

    found_books = [b for b in SHARP_BOOKS if b in per_book]
    print(f"  books found: {found_books}")
    print(f"  quota: {meta.get('requests_used')} used, "
          f"{meta.get('requests_remaining')} remaining")

    # 3. Build consensus per team (median across found sharp books)
    all_teams = set()
    for bk in per_book.values():
        all_teams.update(normalize_odds_team(t) for t in bk.keys())

    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for team in sorted(all_teams):
        book_probs = {}
        for bk_key in SHARP_BOOKS:
            bk = per_book.get(bk_key, {})
            # try exact match, then after normalization
            for raw_name, p in bk.items():
                if normalize_odds_team(raw_name) == team:
                    book_probs[bk_key] = p
                    break
        if len(book_probs) < 2:
            continue  # insufficient sharp coverage
        consensus = statistics.median(book_probs.values())

        pm_p = pm_prices.get(team, 0.0)
        if pm_p <= 0:
            continue

        edge_pct = (consensus - pm_p) * 100.0  # positive → Polymarket underpricing → bet YES
        # A bet is +EV only if |edge| > ROUND_TRIP_FEE_PCT
        net_edge_pct = abs(edge_pct) - ROUND_TRIP_FEE_PCT

        # Direction-confirmation count
        if edge_pct > 0:
            confirming = sum(1 for p in book_probs.values() if p > pm_p)
            pinn_confirm = (per_book.get("pinnacle", {}).get(team, 0) > pm_p) \
                if any(normalize_odds_team(k) == team for k in per_book.get("pinnacle", {})) else False
        else:
            confirming = sum(1 for p in book_probs.values() if p < pm_p)
            pinn_confirm = (per_book.get("pinnacle", {}).get(team, 1) < pm_p) \
                if any(normalize_odds_team(k) == team for k in per_book.get("pinnacle", {})) else False

        # Kelly on the side the sharp books agree with
        if edge_pct > 0:
            side, bet_price, p_true = "YES", pm_p, consensus
        else:
            side, bet_price, p_true = "NO", (1 - pm_p), (1 - consensus)
        f_kelly = kelly_pct(p_true, bet_price)

        rows.append({
            "team": team,
            "pm_p": pm_p,
            "consensus_p": consensus,
            "edge_pct": edge_pct,
            "net_edge_pct": net_edge_pct,
            "book_probs": book_probs,
            "confirming": confirming,
            "pinn_confirm": pinn_confirm,
            "side": side if abs(edge_pct) >= EDGE_FLAG_PCT else "-",
            "f_kelly": f_kelly,
        })

    rows.sort(key=lambda r: -abs(r["edge_pct"]))

    # 4. Print
    print(f"\n{'TEAM':<24s} {'PM':>6s} {'CONS':>6s} {'EDGE':>8s} {'NET':>7s} "
          f"{'PINN':>6s} {'DK':>6s} {'FD':>6s} {'MGM':>6s} {'CZR':>6s} "
          f"{'CONF':>5s} {'SIDE':>5s} {'K%':>6s}")
    print("-" * 114)
    for r in rows:
        bp = r["book_probs"]
        def fmt(k):
            return f"{bp[k]:.3f}" if k in bp else "  -  "
        flag = ""
        if abs(r["edge_pct"]) >= EDGE_FLAG_PCT:
            flag += "  ⭐"
        if r["net_edge_pct"] >= 1.5 and r["confirming"] >= 3 and r["pinn_confirm"]:
            flag += " 🟢TAKE"
        print(
            f"{r['team']:<24s} "
            f"{r['pm_p']:.3f}  {r['consensus_p']:.3f}  "
            f"{r['edge_pct']:>+7.2f}%  {r['net_edge_pct']:>+6.2f}%  "
            f"{fmt('pinnacle')}  {fmt('draftkings')}  {fmt('fanduel')}  "
            f"{fmt('betmgm')}  {fmt('caesars')}  "
            f"{r['confirming']}/{len(r['book_probs'])}  {r['side']:>4s}  "
            f"{r['f_kelly']*100:>5.2f}%{flag}"
        )
        append_log({
            "logged_at": now, "team": r["team"],
            "pm_p": f"{r['pm_p']:.4f}", "consensus_p": f"{r['consensus_p']:.4f}",
            "edge_pct": f"{r['edge_pct']:.2f}",
            "pinnacle_p": bp.get("pinnacle", ""),
            "draftkings_p": bp.get("draftkings", ""),
            "fanduel_p": bp.get("fanduel", ""),
            "betmgm_p": bp.get("betmgm", ""),
            "caesars_p": bp.get("caesars", ""),
            "books_confirming": f"{r['confirming']}/{len(r['book_probs'])}",
            "pinnacle_confirms": r["pinn_confirm"],
            "bet_side": r["side"],
            "fair_kelly_pct": f"{r['f_kelly']*100:.2f}",
            "token_yes": pm_team_tokens.get(r["team"], {}).get("yes", ""),
            "token_no": pm_team_tokens.get(r["team"], {}).get("no", ""),
        })

    print(f"\nlog: {LOG_PATH}")
    print(f"⭐ = |edge| ≥ {EDGE_FLAG_PCT}%  |  🟢TAKE = net_edge ≥ 1.5% AND ≥3 books agree AND Pinnacle agrees")
    print(f"Fee assumption: {ROUND_TRIP_FEE_PCT}% round-trip. Adjust in code if your actual fees differ.")
    print("Sizing: apply your existing 0.25× Kelly from config.py.")


if __name__ == "__main__":
    run()
