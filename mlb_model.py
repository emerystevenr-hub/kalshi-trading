"""
MLB PREGAME WIN-PROBABILITY MODEL  (Phase 1, v1)

Pure Elo + HFA. No starting pitcher, no bullpen, no weather.
Keep it dumb and measurable before we layer.

Edge hypothesis: if pure Elo beats Polymarket closing line on >52% of games
across 100+ games, we have real signal. If not, we learn fast.

Usage:
    from mlb_model import build_model, predict_home_winprob
    model = build_model(start="2025-03-27", end="2026-04-14")
    p = predict_home_winprob(model, home_team="Boston Red Sox",
                             away_team="New York Yankees")
"""

import time
import math
import requests
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

STATSAPI = "https://statsapi.mlb.com/api/v1"

# ──────────────────────────────────────────────────────────────────────
# ELO PARAMETERS (conservative — calibrate after 100 games of data)
# ──────────────────────────────────────────────────────────────────────
ELO_INITIAL = 1500.0
ELO_K = 4.0              # per-game update rate
HFA_ELO = 24.0           # home-field advantage in Elo points (~3.4% win prob)
REGRESSION_TO_MEAN = 0.25  # each offseason, regress 25% toward 1500


# ──────────────────────────────────────────────────────────────────────
# DATA FETCH
# ──────────────────────────────────────────────────────────────────────

def fetch_schedule(start: str, end: str, sleep: float = 0.2) -> List[dict]:
    """Pull all regular-season games between two dates (inclusive). Paginates by week."""
    all_games = []
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    cursor = start_d
    while cursor <= end_d:
        window_end = min(cursor + timedelta(days=7), end_d)
        params = {
            "sportId": 1,
            "startDate": cursor.isoformat(),
            "endDate": window_end.isoformat(),
            "gameTypes": "R",  # regular season only
        }
        try:
            r = requests.get(f"{STATSAPI}/schedule", params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            for d in data.get("dates", []):
                for g in d.get("games", []):
                    all_games.append(g)
        except Exception as e:
            print(f"  ⚠️  schedule fetch failed {cursor}–{window_end}: {e}")
        cursor = window_end + timedelta(days=1)
        time.sleep(sleep)
    return all_games


def parse_game(g: dict) -> Optional[dict]:
    """Extract fields we care about. Returns None for scheduled/in-progress."""
    status = g.get("status", {}).get("abstractGameState")
    if status != "Final":
        return None
    teams = g.get("teams", {})
    home = teams.get("home", {})
    away = teams.get("away", {})
    h_team = home.get("team", {}).get("name")
    a_team = away.get("team", {}).get("name")
    h_score = home.get("score")
    a_score = away.get("score")
    if h_team is None or a_team is None or h_score is None or a_score is None:
        return None
    return {
        "date": g.get("gameDate", "")[:10],
        "home": h_team,
        "away": a_team,
        "home_score": h_score,
        "away_score": a_score,
        "home_win": 1 if h_score > a_score else 0,
    }


# ──────────────────────────────────────────────────────────────────────
# ELO ENGINE
# ──────────────────────────────────────────────────────────────────────

def expected_home_winprob(elo_h: float, elo_a: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(elo_h + HFA_ELO - elo_a) / 400.0))


def update_elo(elo_h: float, elo_a: float, home_win: int) -> Tuple[float, float]:
    exp_h = expected_home_winprob(elo_h, elo_a)
    delta = ELO_K * (home_win - exp_h)
    return elo_h + delta, elo_a - delta


def regress_offseason(ratings: Dict[str, float]) -> Dict[str, float]:
    """Pull each team 25% toward 1500 at season turnover."""
    return {t: r + REGRESSION_TO_MEAN * (1500.0 - r) for t, r in ratings.items()}


def build_model(start: str, end: str, verbose: bool = True) -> Dict[str, float]:
    """
    Run Elo forward through all games in window. Returns final ratings dict.
    Detects season boundaries (Nov–Feb gap) and regresses at boundary.
    """
    if verbose:
        print(f"Fetching MLB games {start} → {end}...")
    raw = fetch_schedule(start, end)
    if verbose:
        print(f"  {len(raw)} raw games returned")
    parsed = [p for p in (parse_game(g) for g in raw) if p is not None]
    parsed.sort(key=lambda g: g["date"])
    if verbose:
        print(f"  {len(parsed)} completed regular-season games")

    ratings: Dict[str, float] = {}
    prev_month = None
    for g in parsed:
        month = int(g["date"][5:7])
        # crude season-break detection: crossing Dec/Jan/Feb triggers regression
        if prev_month is not None and prev_month >= 10 and month <= 3:
            ratings = regress_offseason(ratings)
        prev_month = month

        h, a = g["home"], g["away"]
        eh = ratings.setdefault(h, ELO_INITIAL)
        ea = ratings.setdefault(a, ELO_INITIAL)
        new_h, new_a = update_elo(eh, ea, g["home_win"])
        ratings[h] = new_h
        ratings[a] = new_a

    if verbose:
        print(f"  final ratings covering {len(ratings)} teams")
    return ratings


# ──────────────────────────────────────────────────────────────────────
# PREDICTION
# ──────────────────────────────────────────────────────────────────────

def predict_home_winprob(model: Dict[str, float], home_team: str,
                          away_team: str) -> Optional[float]:
    eh = model.get(home_team)
    ea = model.get(away_team)
    if eh is None or ea is None:
        return None
    return expected_home_winprob(eh, ea)


def dump_ratings(model: Dict[str, float]) -> None:
    print(f"\n{'Team':<25s} {'Elo':>7s}")
    for t, r in sorted(model.items(), key=lambda x: -x[1]):
        print(f"{t:<25s} {r:>7.1f}")


# ──────────────────────────────────────────────────────────────────────
# SEASON SIMULATOR (Monte Carlo)
# ──────────────────────────────────────────────────────────────────────

# MLB divisions 2025+
DIVISIONS = {
    "AL East":    ["New York Yankees", "Toronto Blue Jays", "Tampa Bay Rays",
                   "Baltimore Orioles", "Boston Red Sox"],
    "AL Central": ["Cleveland Guardians", "Chicago White Sox", "Minnesota Twins",
                   "Detroit Tigers", "Kansas City Royals"],
    "AL West":    ["Houston Astros", "Seattle Mariners", "Los Angeles Angels",
                   "Texas Rangers", "Athletics"],
    "NL East":    ["Atlanta Braves", "New York Mets", "Philadelphia Phillies",
                   "Miami Marlins", "Washington Nationals"],
    "NL Central": ["St. Louis Cardinals", "Milwaukee Brewers", "Chicago Cubs",
                   "Cincinnati Reds", "Pittsburgh Pirates"],
    "NL West":    ["Los Angeles Dodgers", "San Diego Padres", "San Francisco Giants",
                   "Arizona Diamondbacks", "Colorado Rockies"],
}

AL_DIVS = ["AL East", "AL Central", "AL West"]
NL_DIVS = ["NL East", "NL Central", "NL West"]


def fetch_current_standings(season: int = None) -> Dict[str, Dict[str, int]]:
    """Return team → {'w': wins, 'l': losses} from MLB StatsAPI."""
    import datetime as _dt
    season = season or _dt.date.today().year
    r = requests.get(f"{STATSAPI}/standings",
                     params={"leagueId": "103,104", "season": season,
                             "standingsTypes": "regularSeason"},
                     timeout=20)
    r.raise_for_status()
    out = {}
    for rec in r.json().get("records", []):
        for tr in rec.get("teamRecords", []):
            name = tr.get("team", {}).get("name")
            if name:
                out[name] = {"w": tr.get("wins", 0), "l": tr.get("losses", 0),
                              "gp": tr.get("gamesPlayed", 0)}
    return out


def fetch_remaining_schedule(season: int = None,
                              start_from: str = None) -> List[Tuple[str, str]]:
    """Return list of (home, away) for all not-yet-Final games this season."""
    import datetime as _dt
    season = season or _dt.date.today().year
    start = start_from or _dt.date.today().isoformat()
    end = f"{season}-10-05"  # regular season end (approximate)
    games = fetch_schedule(start, end)
    remaining = []
    for g in games:
        status = g.get("status", {}).get("abstractGameState")
        if status == "Final":
            continue
        teams = g.get("teams", {})
        h = teams.get("home", {}).get("team", {}).get("name")
        a = teams.get("away", {}).get("team", {}).get("name")
        if h and a:
            remaining.append((h, a))
    return remaining


def _sim_game(rng, elo_h: float, elo_a: float) -> int:
    p = expected_home_winprob(elo_h, elo_a)
    return 1 if rng.random() < p else 0


def _sim_series(rng, elo_a: float, elo_b: float, wins_needed: int,
                hfa_pattern: List[bool]) -> int:
    """Sim a best-of-N series. Returns 1 if team A (listed first) wins, else 0.
    hfa_pattern[i] = True if team A has HFA in game i.
    Elo HFA already baked into expected_home_winprob by swap order."""
    a_wins = b_wins = 0
    game_idx = 0
    target = wins_needed
    while a_wins < target and b_wins < target:
        a_home = hfa_pattern[game_idx] if game_idx < len(hfa_pattern) else True
        if a_home:
            # A at home
            if _sim_game(rng, elo_a, elo_b):
                a_wins += 1
            else:
                b_wins += 1
        else:
            # B at home
            if _sim_game(rng, elo_b, elo_a):
                b_wins += 1
            else:
                a_wins += 1
        game_idx += 1
    return 1 if a_wins == target else 0


def simulate_season(
    ratings: Dict[str, float],
    standings: Dict[str, Dict[str, int]],
    remaining: List[Tuple[str, str]],
    n_sims: int = 10000,
    seed: int = 42,
) -> Dict[str, Dict[str, float]]:
    """
    Run n_sims Monte Carlo seasons.
    Returns team → {'div_win': p, 'wc': p, 'playoff': p, 'pennant': p, 'ws': p}.
    """
    import random
    rng = random.Random(seed)

    team_to_div = {t: d for d, teams in DIVISIONS.items() for t in teams}
    all_teams = list(team_to_div.keys())

    # baseline W/L from standings
    base_wl = {t: (standings.get(t, {}).get("w", 0),
                    standings.get(t, {}).get("l", 0)) for t in all_teams}

    counters = {t: {"div_win": 0, "wc": 0, "playoff": 0,
                    "pennant": 0, "ws": 0} for t in all_teams}

    for sim in range(n_sims):
        # 1. sim remaining regular-season games
        wl = {t: list(base_wl[t]) for t in all_teams}
        for home, away in remaining:
            if home not in ratings or away not in ratings:
                continue
            if _sim_game(rng, ratings[home], ratings[away]):
                wl[home][0] += 1
                wl[away][1] += 1
            else:
                wl[home][1] += 1
                wl[away][0] += 1

        # 2. determine playoff teams per league (3 div winners + 3 wildcards)
        playoffs = {"AL": [], "NL": []}
        for league, divs in (("AL", AL_DIVS), ("NL", NL_DIVS)):
            division_winners = []
            all_league_teams = []
            for dname in divs:
                teams_in_div = DIVISIONS[dname]
                # sort by wins desc, tiebreak random-ish by elo
                ranked = sorted(teams_in_div,
                                key=lambda t: (wl[t][0], ratings.get(t, 1500)),
                                reverse=True)
                division_winners.append(ranked[0])
                counters[ranked[0]]["div_win"] += 1
                all_league_teams.extend(teams_in_div)
            # wildcards = top 3 remaining by wins
            remaining_teams = [t for t in all_league_teams if t not in division_winners]
            remaining_teams.sort(key=lambda t: (wl[t][0], ratings.get(t, 1500)),
                                 reverse=True)
            wildcards = remaining_teams[:3]
            for wc in wildcards:
                counters[wc]["wc"] += 1
            # seed 1-3 = division winners by wins, 4-6 = wildcards
            division_winners.sort(key=lambda t: (wl[t][0], ratings.get(t, 1500)),
                                  reverse=True)
            seeds = division_winners + wildcards
            playoffs[league] = seeds
            for p in seeds:
                counters[p]["playoff"] += 1

        # 3. sim playoffs for each league (seeds 1-3 get byes, 4v5, 3v6 best-of-3)
        league_champs = {}
        for league in ("AL", "NL"):
            s = playoffs[league]
            # Wild Card round: 4 vs 5, 3 vs 6 (all games at higher seed)
            wc_a = s[3] if _sim_series(rng, ratings[s[3]], ratings[s[4]], 2,
                                        [True, True, True]) else s[4]
            wc_b = s[2] if _sim_series(rng, ratings[s[2]], ratings[s[5]], 2,
                                        [True, True, True]) else s[5]
            # Division Series: 1 vs wc_a, 2 vs wc_b (best-of-5)
            ds_a = s[0] if _sim_series(rng, ratings[s[0]], ratings[wc_a], 3,
                                        [True, True, False, False, True]) else wc_a
            ds_b = s[1] if _sim_series(rng, ratings[s[1]], ratings[wc_b], 3,
                                        [True, True, False, False, True]) else wc_b
            # LCS: best-of-7 (2-3-2 format, higher seed has HFA)
            if wl[ds_a][0] >= wl[ds_b][0]:
                champ = ds_a if _sim_series(rng, ratings[ds_a], ratings[ds_b], 4,
                                             [True, True, False, False, False, True, True]) else ds_b
            else:
                champ = ds_b if _sim_series(rng, ratings[ds_b], ratings[ds_a], 4,
                                             [True, True, False, False, False, True, True]) else ds_a
            league_champs[league] = champ
            counters[champ]["pennant"] += 1

        # 4. World Series: NL vs AL. HFA → team with more regular-season wins.
        al, nl = league_champs["AL"], league_champs["NL"]
        if wl[al][0] >= wl[nl][0]:
            ws_winner = al if _sim_series(rng, ratings[al], ratings[nl], 4,
                                           [True, True, False, False, False, True, True]) else nl
        else:
            ws_winner = nl if _sim_series(rng, ratings[nl], ratings[al], 4,
                                           [True, True, False, False, False, True, True]) else al
        counters[ws_winner]["ws"] += 1

    # normalize
    out = {}
    for t, c in counters.items():
        out[t] = {k: v / n_sims for k, v in c.items()}
    return out


if __name__ == "__main__":
    # Build from 2025 opening day through yesterday (inclusive).
    # Adjust dates as needed.
    today = date.today()
    model = build_model(start="2025-03-27", end=(today - timedelta(days=1)).isoformat())
    dump_ratings(model)
    # Example: predict today's hypothetical matchup
    print("\nSample prediction — Red Sox (home) vs Yankees (away):")
    p = predict_home_winprob(model, "Boston Red Sox", "New York Yankees")
    if p is not None:
        print(f"  P(BOS wins) = {p:.4f}  |  implied fair price = {p:.4f}")
    else:
        print("  team names unknown in model — check canonical names via dump_ratings()")
