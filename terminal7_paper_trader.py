"""Terminal 7 — NBA/NHL Playoffs Paper Trader (Game 1-2 only).

Structural clone of terminal6_mlb_paper_trader.py with three deltas:

  Δ1 — Game-number gate (Filter 14): regex on Kalshi event title
       "Game N: Away at Home". Reject if N > 2. Replaces an entire
       external API integration (ESPN/NBA Stats) — the data is in the
       Kalshi event title already (probed 2026-05-09).

  Δ2 — Per-series exposure cap (Filter 15): max 2 open positions per
       series_ticker (G1 + G2 = full series exposure for our window).
       Reads series_ticker from market_row (captured by WS / REST
       logger from event metadata).

  Δ3 — Sport-specific normalization (Filter applied via dispatch):
       NBA team map and NHL team map are SEPARATE — codes overlap
       between leagues (CHI = Bulls in NBA, Blackhawks in NHL; UTA =
       Jazz in NBA, Hockey Club in NHL; etc.). Sport detected from
       event_ticker prefix (KXNBAGAME / KXNHLGAME) and dispatches to
       the right team map and _normalize_*().

Liquidity floor (Filter 16): per spec §3, skip markets with
  open_interest < 100 AND volume_24h < 50. Kalshi NBA/NHL game markets
  had OI=0 / vol_24h=0 across 14 markets at probe time — INSUFFICIENT_
  LIQUIDITY is a foreseeable verdict per spec §11.

DRY-RUN BY DEFAULT. Pass --live to write to ledger.

Bankroll context (engines.json T7):
  $2K reserved (from T1 sub-bucket capital recycle). Kelly cap 5% =
  $100/position. Total exposure cap 50% = $1K. MAX_CONTRACTS=200 (vs
  T6's 500) — scaled to bankroll. Session 3 audit M-T6-1 silent-cap
  hole closed in T7 from the start: every cap hit logs [size-cap].

Validation gate (modified — see terminal7_milestone_check.py):
  T7 cannot reach n=300 this cycle (~12 G1/G2 games combined NBA+NHL,
  expect 6-10 fires). Validation is ARCHITECTURE_CONFIRMED at n≥5
  fires with no architectural failures. T6 remains the edge validator.

Usage:
    python3 ~/Documents/terminal7_paper_trader.py --once
    python3 ~/Documents/terminal7_paper_trader.py --once --live
    nohup caffeinate -is python3 ~/Documents/terminal7_paper_trader.py \\
        --interval-sec 1800 \\
        > ~/Documents/terminal7_data/paper_trader.out 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

# Allow imports from ~/Documents
sys.path.insert(0, str(Path.home() / "Documents"))

from shadow_pnl_core import ShadowLedger, _read_ledger, load_engine_bankroll  # noqa: E402

ENGINE = "T7"
DATA_DIR = Path.home() / "Documents" / "terminal7_data"
LOG_PATH = DATA_DIR / "paper_trader.log"

# --- Phase 1 parameters (per spec §5, §3) ---
DELTA_THRESHOLD = 0.03        # 3 percentage points (mirrors T6)
MIN_CONTRACTS = 5             # floor — never bet less than this if signal triggers
MAX_CONTRACTS = 200           # ceiling — scaled from T6's 500 to T7's $2K bankroll
KELLY_FRACTION = 0.5          # half-Kelly
MAX_BET_PCT_BANKROLL = 0.05   # 5% per-position cap
MAX_TOTAL_EXPOSURE_PCT = 0.50 # 50% total exposure cap
BANKROLL_USD_INITIAL = load_engine_bankroll(ENGINE)
                              # T7 bankroll — read at import time from engines.json via
                              # shadow_pnl_core.load_engine_bankroll. engines.json is the
                              # single source of truth; edit there + restart daemon to change.
                              # Origin: $2K reserve from T1 sub-bucket capital recycle.
                              # Live bankroll = INITIAL + realized P&L.
KALSHI_TAKER_FEE_RATE = 0.07
MAX_DAILY_OPENS = 8           # scaled from T6's 15 — fewer games/day in playoffs
MAX_TOTAL_OPENS = 20          # scaled from T6's 30 — runway-constrained
MAX_PER_SERIES_OPENS = 2      # NEW Filter 15 — G1 + G2 only
MIN_KALSHI_P = 0.20
MAX_KALSHI_P = 0.80
MAX_SPREAD_CENTS = 5
MIN_LEAD_MINUTES = 15
MAX_VEGAS_AGE_MIN = 60
MIN_OPEN_INTEREST = 100       # NEW Filter 16 — liquidity floor
MIN_VOLUME_24H = 50           # NEW Filter 16 — liquidity floor
DEFAULT_INTERVAL_SEC = 1800   # 30 min (mirrors T6)

# Game-number regex per spec §2.1. The Kalshi event title encodes
# "Game N: Away at Home" — one regex eliminates an entire external API.
# Sentinel: every cycle logs the count of title-parse-failed rejections.
# If count > 0 on any cycle, alert — Kalshi may have changed title format
# (Lesson #35: silent-failure sentinel against format-change kills).
GAME_NUM_RE = re.compile(r'^Game\s+(\d+)\s*:', re.IGNORECASE)
MAX_GAME_NUM = 2              # G1-G2 only per spec §1

# Module-level constants for rejection reasons referenced by the cycle log
# alert. Using a constant prevents drift between the reject string and the
# alert lookup (was a fragile dependency on truncation length).
TITLE_PARSE_FAILED_REASON = "[title-parse-failed]"
NON_GAME_CONTRACT_REASON = "non-game contract"

_STOP_REQUESTED = False


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _handle_sigint(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    log("[signal] stop requested")


# --------------------------------------------------------------------------
# Sport detection + per-sport team maps
# --------------------------------------------------------------------------

# NBA team map: 30 teams. Edge cases:
#  - Brooklyn Nets (BKN, also BRK on some books) — confirmed NETS only by 2026.
#  - Charlotte Hornets (CHA) — Bobcats→Hornets rebrand done in 2014, no overlap.
#  - LA Clippers (LAC) and LA Lakers (LAL) — distinct codes, no merge.
KALSHI_NBA = {
    "ATL": "Atlanta Hawks",
    "BOS": "Boston Celtics",
    "BKN": "Brooklyn Nets", "BRK": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHO": "Charlotte Hornets",
    "CHI": "Chicago Bulls",
    "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks",
    "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "GS": "Golden State Warriors",
    "HOU": "Houston Rockets",
    "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks",
    "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NO": "New Orleans Pelicans",
    "NYK": "New York Knicks", "NY": "New York Knicks",
    "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers",
    "PHX": "Phoenix Suns", "PHO": "Phoenix Suns",
    "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs", "SA": "San Antonio Spurs",
    "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz",
    "WAS": "Washington Wizards", "WSH": "Washington Wizards",
}

# NHL team map: 32 teams. Edge cases:
#  - Utah Hockey Club (2024 relocation from Arizona Coyotes). Dry-run will
#    confirm whether Vegas API returns "Utah" or "Utah Hockey Club" and
#    whether Kalshi uses UTA or UHC. We map BOTH UTA and UHC for safety.
#    NB: NBA uses UTA for Jazz — sport-dispatch handles disambiguation.
#  - Vegas Golden Knights (VGK) — disambiguated from "Vegas" the city in
#    Vegas API responses by full team name match.
#  - Seattle Kraken (SEA) — added 2021, fully integrated in Odds API.
#  - Many code overlaps with NBA: BOS/CHI/DAL/DET/PHI/TOR/MIN/WSH/SEA.
#    Sport-dispatch via event_ticker prefix is what keeps this safe.
KALSHI_NHL = {
    "ANA": "Anaheim Ducks",
    "ARI": "Arizona Coyotes",      # legacy — relocated to Utah in 2024.
                                    # Kept for historical settlement; should not
                                    # appear in 2026 playoffs.
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CAR": "Carolina Hurricanes",
    "CBJ": "Columbus Blue Jackets",
    "CGY": "Calgary Flames",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings", "LA": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens", "MON": "Montreal Canadiens",
    "NJD": "New Jersey Devils", "NJ": "New Jersey Devils",
    "NSH": "Nashville Predators",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SJS": "San Jose Sharks", "SJ": "San Jose Sharks",
    "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning", "TB": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Hockey Club", "UHC": "Utah Hockey Club",
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights", "VEG": "Vegas Golden Knights",
    "WPG": "Winnipeg Jets", "WIN": "Winnipeg Jets",
    "WSH": "Washington Capitals", "WAS": "Washington Capitals",
}


def detect_sport(event_ticker: str) -> Optional[str]:
    """Return 'nba', 'nhl', or None based on Kalshi event ticker prefix."""
    if not event_ticker:
        return None
    if event_ticker.startswith("KXNBAGAME-"):
        return "nba"
    if event_ticker.startswith("KXNHLGAME-"):
        return "nhl"
    return None


def _normalize_nba(name: str) -> str:
    """NBA-specific normalization. Currently identity (no rebrands like MLB
    Athletics had). Kept as separate function per spec §2.3 — sport-specific
    edge cases will leak into each other if shared."""
    if not name:
        return ""
    return name.strip()


def _normalize_nhl(name: str) -> str:
    """NHL-specific normalization.

    Edge cases this normalizer collapses:
      - Accented forms via Unicode NFKD ASCII-fold. Probed Odds API
        2026-05-10: returns 'Montréal Canadiens' with é; KALSHI_NHL emits
        'Montreal Canadiens' without. Direct string equality silently
        fails Vegas matching for every MTL game without this fold.
      - Utah franchise rebrand chain: 2024-25 was "Utah Hockey Club"
        (relocated from Arizona Coyotes). 2025-26 rebranded to "Utah
        Mammoth". Utah not in Odds API feed at probe time (likely
        eliminated this playoffs) so authoritative mapping pending —
        defensive fix maps all observed/expected forms to single canonical.
      - "Vegas Golden Knights" stays canonical (probed clean from Odds API).
    """
    if not name:
        return ""
    # ASCII-fold accents (Montréal → Montreal, etc.)
    n = unicodedata.normalize("NFKD", name).strip()
    n = "".join(c for c in n if not unicodedata.combining(c))
    # Utah rebrand chain — collapse to canonical form. Update if Odds API
    # confirms a different canonical when Utah next appears in feed.
    if n in ("Utah", "Utah HC", "Utah Hockey Club", "Utah Mammoth"):
        return "Utah Hockey Club"
    return n


def parse_kalshi_event_teams(event_ticker: str) -> Optional[Tuple[str, str, str]]:
    """KXNBAGAME-26MAY12MINSAS → ('Minnesota Timberwolves', 'San Antonio Spurs', 'nba').
    KXNHLGAME-26JUN02UTAVGK → ('Utah Hockey Club', 'Vegas Golden Knights', 'nhl').

    Format DIFFERS from T6 MLB. Probed live Kalshi 2026-05-09:
      NBA/NHL: KX{NBA|NHL}GAME-{YY}{MMM}{DD}{AWAY}{HOME}     (7-char date)
      MLB:     KXMLBGAME-{YY}{MMM}{DD}{HHMM}{AWAY}{HOME}     (11-char date+time)

    Playoff games appear without time info on Kalshi; MLB regular-season
    games carry time. Date-only prefix is exactly 7 chars (yy+mmm+dd).
    Team-pair is 4-6 chars (each team is 2-3 letters).

    Convention: away team listed first.
    """
    sport = detect_sport(event_ticker)
    if sport is None:
        return None
    if sport == "nba":
        team_map = KALSHI_NBA
        prefix = "KXNBAGAME-"
    elif sport == "nhl":
        team_map = KALSHI_NHL
        prefix = "KXNHLGAME-"
    else:
        return None
    rest = event_ticker[len(prefix):]
    # 7-char date prefix + 4-6 char team-pair = 11-13 chars minimum
    if len(rest) < 11:
        return None
    team_part = rest[7:]   # NBA/NHL: skip 7-char date-only prefix
    # Try 2/2, 2/3, 3/2, 3/3 splits — same logic as T6.
    for split in (2, 3):
        if 2 <= len(team_part) - split <= 3:
            away = team_part[:split]
            home = team_part[split:]
            if away in team_map and home in team_map:
                return team_map[away], team_map[home], sport
    return None


_TICKER_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_ticker_game_dt_utc(event_ticker: str) -> Optional[datetime]:
    """KXNBAGAME-26MAY12MINSAS / KXNHLGAME-26JUN02UTAVGK → datetime in UTC.

    Born 2026-05-10 — same fix as T6's parse_ticker_game_dt_utc, ported
    for the NBA/NHL ticker format which is date-only (no encoded time).
    See T6 fix rationale: a multi-day playoff series matched team-pair-
    only would mis-bind a Game-N ticker to Game-(N+1)'s Vegas line. NBA
    playoff series run G1-G2 separated by ~2 days; NHL similar; the
    risk is identical to MLB consecutive-day series.

    NBA/NHL playoff tickers carry only YYMMMDD. We anchor on 19:30 ET
    as the typical playoff tip and apply ±18h in the caller — covers
    afternoon to late-night start times while still rejecting next-day
    games (which are typically ~48h away in playoff schedules).
    """
    sport = detect_sport(event_ticker)
    if sport == "nba":
        prefix = "KXNBAGAME-"
    elif sport == "nhl":
        prefix = "KXNHLGAME-"
    else:
        return None
    rest = event_ticker[len(prefix):]
    if len(rest) < 7:
        return None
    try:
        yy = int(rest[0:2])
        mmm = rest[2:5].upper()
        dd = int(rest[5:7])
    except ValueError:
        return None
    if mmm not in _TICKER_MONTHS:
        return None
    try:
        local = datetime(2000 + yy, _TICKER_MONTHS[mmm], dd, 19, 30)
    except ValueError:
        return None
    return local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def synth_series_id(event_ticker: str) -> Optional[str]:
    """T7 Filter 15 input — synthetic per-playoff-series ID.

    Kalshi's events endpoint returns series_ticker = parent series name
    ("KXNBAGAME" / "KXNHLGAME") — same value for every event, useless for
    per-playoff-series correlation. Spec §2.2 assumption was wrong.

    Construction: parse team pair from event_ticker, map each team CODE
    through the SAME KALSHI_{NBA,NHL} map that drives Vegas matching, apply
    the SAME _normalize_*() that drives Vegas matching, sort, join. This
    guarantees the synthetic ID stays aligned with Vegas-match canonical
    forms by sharing one normalization source. If they diverge, BOTH
    Vegas matching and series cap break together (visible), instead of
    series cap silently breaking (hidden) — Steve's session-4 catch.

    Examples:
      KXNBAGAME-26MAY12MINSAS → "Minnesota Timberwolves|San Antonio Spurs"
      KXNBAGAME-26MAY10SASMIN → "Minnesota Timberwolves|San Antonio Spurs" (same)
      KXNHLGAME-26JUN02UTAVGK → "Utah Hockey Club|Vegas Golden Knights"
      Kalshi alias "GS" or "GSW" both → "Golden State Warriors" (collapsed)
    """
    parsed = parse_kalshi_event_teams(event_ticker)
    if not parsed:
        return None
    away_full, home_full, sport = parsed
    normalize = _normalize_nba if sport == "nba" else _normalize_nhl
    a, h = normalize(away_full), normalize(home_full)
    return "|".join(sorted([a, h]))


# --------------------------------------------------------------------------
# Vegas lines: load most recent line per game (sport-tagged)
# --------------------------------------------------------------------------

def load_latest_vegas_lines() -> Dict[str, dict]:
    """Return {game_id: most-recent line dict} from today's vegas_lines file.

    File format: ~/Documents/terminal7_data/vegas_lines_{YYYY-MM-DD}.jsonl
    The lines puller writes BOTH NBA and NHL games into the same file with a
    'sport' field on each row.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = DATA_DIR / f"vegas_lines_{today}.jsonl"
    latest: Dict[str, dict] = {}
    if not path.exists():
        log(f"  [warn] no vegas lines file at {path}")
        return latest
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                gid = r.get("game_id")
                if not gid:
                    continue
                prev = latest.get(gid)
                if prev is None or r.get("snap_ts_utc", "") > prev.get("snap_ts_utc", ""):
                    latest[gid] = r
    except OSError as e:
        log(f"  [error] could not read vegas lines: {e}")
    return latest


# --------------------------------------------------------------------------
# Kalshi snapshots: load most recent row per market (NBA + NHL)
# --------------------------------------------------------------------------

def load_latest_kalshi_markets() -> List[dict]:
    """Read all today's KXNBAGAME and KXNHLGAME snapshot files; return one
    row per ticker — the most recent."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    patterns = [
        f"kalshi_KXNBAGAME-*_{today}.jsonl",
        f"kalshi_KXNHLGAME-*_{today}.jsonl",
    ]
    files: List[Path] = []
    for p in patterns:
        files.extend(sorted(DATA_DIR.glob(p)))
    latest: Dict[str, dict] = {}
    for path in files:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = r.get("ticker")
                    if not key:
                        continue
                    prev = latest.get(key)
                    if prev is None or r.get("snap_ts_utc", "") > prev.get("snap_ts_utc", ""):
                        latest[key] = r
        except OSError:
            continue
    return list(latest.values())


# --------------------------------------------------------------------------
# Trade trigger logic
# --------------------------------------------------------------------------

def evaluate_market(market_row: dict, vegas_by_game: Dict[str, dict],
                    now_utc: datetime,
                    open_series_counts: Dict[str, int]) -> Optional[dict]:
    """Return a signal dict if the market triggers a trade, else None.

    Filters in priority order (early returns short-circuit):
      1.  event_ticker parses to (away, home, sport).
      14. NEW: event title parses to game_num via GAME_NUM_RE.
      14. NEW: game_num ≤ MAX_GAME_NUM (G1-G2 only).
      15. NEW: per-series exposure cap (≤ MAX_PER_SERIES_OPENS).
      16. NEW: liquidity floor (open_interest, volume_24h).
      ...then T6's gates: team match, vegas join, lead time, freshness,
      spread, kalshi_p band, delta threshold, sizing.
    """
    et = market_row.get("event_ticker") or ""
    ticker = market_row.get("ticker") or ""
    parse_out = parse_kalshi_event_teams(et)
    if not parse_out:
        return {"reject_reason": f"unparseable event {et}"}
    away, home, sport = parse_out

    # ------------------------- Filter 14: game number gate -----------------
    # Per spec §2.1. "Game N: Away at Home" → reject if N > 2.
    # event_title is captured by WS / REST logger from event metadata.
    #
    # H4 fix: scope the [title-parse-failed] sentinel to "Game"-prefixed
    # titles only. KXNBAGAME/KXNHLGAME tickers can also represent series-
    # winner / conference-winner / non-game contracts whose titles don't
    # contain "Game N: ..." — those should hard-skip as benign non-game
    # contracts BEFORE the sentinel fires. Otherwise the sentinel pollutes
    # every cycle and operators stop reading the alert; real format change
    # becomes silent again.
    event_title = (market_row.get("event_title") or "").strip()
    if not event_title:
        return {"reject_reason": "no event_title"}
    m = GAME_NUM_RE.match(event_title)
    if not m:
        if "game" in event_title.lower():
            # Title CONTAINS "game" but didn't match the leading-Game-N: form
            # → format change suspected. Sentinel fires.
            return {"reject_reason": TITLE_PARSE_FAILED_REASON}
        # No "game" anywhere in title → non-game contract (series winner,
        # conference winner, etc.). Benign reject; do NOT trip sentinel.
        return {"reject_reason": NON_GAME_CONTRACT_REASON}
    try:
        game_num = int(m.group(1))
    except ValueError:
        return {"reject_reason": TITLE_PARSE_FAILED_REASON}
    if game_num > MAX_GAME_NUM:
        # Bucket as one rejection category — don't embed value (Lesson #31).
        return {"reject_reason": f"game N>{MAX_GAME_NUM}"}

    # ------------------------- Filter 15: per-series exposure cap ----------
    # Per spec §3 + session-4 fix: synthetic series_id from sorted team pair
    # (Kalshi's series_ticker is parent series name, useless for per-matchup
    # correlation). synth_series_id() routes through the SAME team_map +
    # _normalize_*() that drive Vegas matching — divergence becomes visible.
    series_id = synth_series_id(et)
    if series_id is None:
        # Defensive — parse_kalshi_event_teams already returned valid above,
        # so this should be unreachable. Treat as parse failure if it happens.
        return {"reject_reason": "[series-id-derivation-failed]"}
    n_series_open = open_series_counts.get(series_id, 0)
    if n_series_open >= MAX_PER_SERIES_OPENS:
        return {"reject_reason": f"series cap {n_series_open}≥{MAX_PER_SERIES_OPENS}"}

    # ------------------------- Filter 16: liquidity floor ------------------
    # Per spec §3 and §11. NBA/NHL game markets had OI=0/vol=0 across all
    # 14 markets at probe time. Skip ultra-thin markets.
    oi = market_row.get("open_interest") or 0
    v24 = market_row.get("volume_24h") or 0
    try:
        oi = int(oi); v24 = int(v24)
    except (TypeError, ValueError):
        oi, v24 = 0, 0
    if oi < MIN_OPEN_INTEREST and v24 < MIN_VOLUME_24H:
        return {"reject_reason": f"liquidity floor (OI={oi},v24={v24})"}

    # ------------------------- Team subtitle resolution --------------------
    yes_sub = (market_row.get("yes_sub_title") or
               market_row.get("subtitle") or "").strip()
    if not yes_sub:
        return {"reject_reason": "no subtitle"}
    market_team = None
    if yes_sub.lower() in home.lower() or home.lower() in yes_sub.lower():
        market_team = "home"
    elif yes_sub.lower() in away.lower() or away.lower() in yes_sub.lower():
        market_team = "away"
    else:
        # Subtitle didn't match — try Kalshi suffix from ticker (e.g. -DET)
        suffix_match = re.search(r"-([A-Z]{2,3})$", ticker)
        if suffix_match:
            kcode = suffix_match.group(1)
            team_map = KALSHI_NBA if sport == "nba" else KALSHI_NHL
            full = team_map.get(kcode)
            if full == home:
                market_team = "home"
            elif full == away:
                market_team = "away"
    if not market_team:
        return {"reject_reason": f"could not match team for {ticker}"}

    # ------------------------- Vegas match (sport-aware normalize) ---------
    normalize = _normalize_nba if sport == "nba" else _normalize_nhl
    home_n, away_n = normalize(home), normalize(away)

    # Filter Vegas candidates by sport (puller tags each row); falls back
    # to all candidates if sport tag missing for backwards-compat.
    candidates = []
    for g in vegas_by_game.values():
        g_sport = g.get("sport")
        if g_sport and g_sport != sport:
            continue
        if (normalize(g.get("home_team")) == home_n and
                normalize(g.get("away_team")) == away_n):
            candidates.append(g)
    if not candidates:
        return {"reject_reason": "no vegas match"}

    def _commence_dt(g):
        c = g.get("commence_time_utc")
        if not c:
            return None
        try:
            return datetime.fromisoformat(c.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            return None

    # MATCH-SOONEST window logic (T6 H17 fix preserved).
    WINDOW = timedelta(hours=36)
    in_window = [g for g in candidates
                 if _commence_dt(g) and abs(_commence_dt(g) - now_utc) <= WINDOW]
    if not in_window:
        return {"reject_reason": "no vegas match (no game within 36h)"}

    # 2026-05-10 BUG FIX (mirror of T6 fix shipped same day): narrow
    # candidates by the ticker's encoded date BEFORE future/past
    # selection. Without this, a multi-game playoff series matches
    # team-pair-only and the "soonest upcoming" rule binds a Game-N
    # settled ticker to Game-(N+1)'s Vegas line — fabricating fires.
    # See T6 paper_trader.py for full rationale; same bug class.
    # Window is ±18h here (vs ±12h on T6) because NBA/NHL tickers are
    # date-only (no encoded time) and we anchor on 19:30 ET.
    ticker_dt_utc = parse_ticker_game_dt_utc(et)
    if ticker_dt_utc is not None:
        TICKER_DT_WINDOW = timedelta(hours=18)
        date_matched = [g for g in in_window
                        if _commence_dt(g) is not None
                        and abs(_commence_dt(g) - ticker_dt_utc) <= TICKER_DT_WINDOW]
        if not date_matched:
            return {"reject_reason": "no vegas match (no game within 18h of ticker date)"}
        in_window = date_matched

    future = [g for g in in_window if _commence_dt(g) > now_utc]
    if future:
        vegas = min(future, key=_commence_dt)
    else:
        vegas = max(in_window, key=_commence_dt)

    # ------------------------- Lead-time gate ------------------------------
    commence = vegas.get("commence_time_utc")
    try:
        commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        lead_min = (commence_dt - now_utc).total_seconds() / 60.0
    except (TypeError, ValueError, AttributeError):
        return {"reject_reason": "commence time unparseable"}
    if lead_min < MIN_LEAD_MINUTES:
        return {"reject_reason": f"game starts in {lead_min:.0f} min"}

    # ------------------------- Vegas freshness -----------------------------
    vegas_ts_str = vegas.get("snap_ts_utc")
    try:
        vegas_ts = datetime.fromisoformat(vegas_ts_str.replace("Z", "+00:00"))
        age_min = (now_utc - vegas_ts).total_seconds() / 60.0
    except (TypeError, ValueError, AttributeError):
        return {"reject_reason": "vegas timestamp unparseable"}
    if age_min > MAX_VEGAS_AGE_MIN:
        return {"reject_reason": f"vegas line stale ({age_min:.0f} min)"}

    # ------------------------- Kalshi book sanity --------------------------
    yes_top = market_row.get("yes_top_price_cents")
    no_top = market_row.get("no_top_price_cents")
    spread = market_row.get("spread_cents")
    if yes_top is None or no_top is None:
        return {"reject_reason": "no two-sided book"}
    if spread is None or spread > MAX_SPREAD_CENTS:
        return {"reject_reason": f"spread > {MAX_SPREAD_CENTS}c"}

    yes_ask_cents = 100 - no_top
    kalshi_implied_p = ((yes_top + yes_ask_cents) / 2.0) / 100.0
    if kalshi_implied_p < MIN_KALSHI_P or kalshi_implied_p > MAX_KALSHI_P:
        return {"reject_reason": f"kalshi p outside [{MIN_KALSHI_P},{MAX_KALSHI_P}]"}

    # ------------------------- Sharp consensus + delta ---------------------
    sharp_p_field = "consensus_home_p" if market_team == "home" else "consensus_away_p"
    sharp_p = vegas.get(sharp_p_field)
    if sharp_p is None:
        return {"reject_reason": "vegas consensus null"}

    delta = sharp_p - kalshi_implied_p

    # KL divergence telemetry (parallel to delta, mirrors T6).
    import math
    p_clip = lambda x: max(0.01, min(0.99, x))
    sp = p_clip(sharp_p)
    kp = p_clip(kalshi_implied_p)
    kl_nats = sp * math.log(sp / kp) + (1.0 - sp) * math.log((1.0 - sp) / (1.0 - kp))

    if abs(delta) < DELTA_THRESHOLD:
        return {
            "reject_reason": f"|delta| < {DELTA_THRESHOLD} (sub-threshold)",
            "ticker": ticker,
            "event_ticker": et,
            "sport": sport,
            "kalshi_implied_p": kalshi_implied_p,
            "sharp_p": sharp_p,
            "delta": delta,
            "abs_delta": abs(delta),
            "kl_nats": kl_nats,
            "lead_min": lead_min,
            "spread_cents": spread,
            "_near_miss": True,
        }

    side = "YES" if delta > 0 else "NO"
    if side == "YES":
        entry_cents = yes_ask_cents
    else:
        entry_cents = 100 - yes_top

    return {
        "ticker": ticker,
        "event_ticker": et,
        "series_id": series_id,                         # synthetic, used for Filter 15
        "series_ticker": market_row.get("series_ticker"),  # Kalshi's parent name (audit only)
        "sport": sport,
        "game_num": game_num,
        "side": side,
        "team_market": market_team,
        "team_name": home if market_team == "home" else away,
        "kalshi_implied_p": kalshi_implied_p,
        "sharp_p": sharp_p,
        "delta": delta,
        "kl_nats": kl_nats,
        "entry_price": entry_cents / 100.0,
        "spread_cents": spread,
        "lead_min": lead_min,
        "vegas_age_min": age_min,
        "yes_top_cents": yes_top,
        "no_top_cents": no_top,
        "books_used": vegas.get("books_used"),
        "commence_time_utc": commence,
        "open_interest": oi,
        "volume_24h": v24,
    }


def kalshi_taker_fee_per_contract(price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    return KALSHI_TAKER_FEE_RATE * price * (1.0 - price)


def kelly_size_contracts(side: str, entry_price: float, our_p: float,
                         bankroll: float, current_exposure_usd: float) -> Tuple[int, dict]:
    """Mirrors T6 kelly_size_contracts (binary-Kalshi-aware, fee-included).
    Closes T6 audit M-T6-1 silent-cap hole: when MAX_CONTRACTS clips,
    sizing_md flags 'capped_at_max' so caller can log [size-cap]."""
    if side == "YES":
        q = our_p
        p = entry_price
    else:
        q = 1.0 - our_p
        p = entry_price
    if p <= 0 or p >= 1:
        return 0, {"reason": "invalid price", "q": q, "p": p}

    fee_per_contract = kalshi_taker_fee_per_contract(p)
    effective_stake = p + fee_per_contract
    net_payout = 1.0 - p - fee_per_contract
    if net_payout <= 0 or effective_stake <= 0:
        return 0, {"reason": "fee swallows payout", "fee": fee_per_contract,
                   "effective_stake": effective_stake, "net_payout": net_payout}

    b = net_payout / effective_stake
    f_full = (b * q - (1.0 - q)) / b
    if f_full <= 0:
        return 0, {
            "reason": "edge dissolved by fees",
            "f_full": f_full, "b": b, "q": q, "p": p,
            "fee_per_contract": fee_per_contract,
        }

    f_used = f_full * KELLY_FRACTION
    bet_usd = bankroll * f_used

    max_bet_usd = bankroll * MAX_BET_PCT_BANKROLL
    bet_usd = min(bet_usd, max_bet_usd)

    headroom_usd = max(0.0, bankroll * MAX_TOTAL_EXPOSURE_PCT - current_exposure_usd)
    bet_usd = min(bet_usd, headroom_usd)

    contracts = int(bet_usd / effective_stake)
    if contracts < MIN_CONTRACTS:
        if bet_usd < MIN_CONTRACTS * effective_stake:
            return 0, {
                "reason": "below MIN_CONTRACTS floor",
                "f_full": f_full, "f_used": f_used,
                "bet_usd": bet_usd, "fee_per_contract": fee_per_contract,
                "contracts_implied": contracts,
            }
        contracts = MIN_CONTRACTS

    capped_at_max = contracts > MAX_CONTRACTS
    contracts = min(contracts, MAX_CONTRACTS)

    return contracts, {
        "f_full": f_full,
        "f_used": f_used,
        "b": b,
        "q": q, "p": p,
        "fee_per_contract": fee_per_contract,
        "effective_stake_per_contract": effective_stake,
        "net_payout_per_contract": net_payout,
        "bet_usd": contracts * effective_stake,
        "max_bet_cap_usd": max_bet_usd,
        "exposure_headroom_usd": headroom_usd,
        "bankroll_usd": bankroll,
        "capped_at_max": capped_at_max,   # T7 audit M-T6-1 closer from day 1
    }


def already_open_on_event(event_ticker: str) -> bool:
    """Cluster cap (T6 Filter 10): 1 position per game (event_ticker)."""
    opens_by_pid = {}
    closed_pids = set()
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        if r.get("type") == "open":
            opens_by_pid[r["position_id"]] = r
        elif r.get("type") == "close":
            closed_pids.add(r["position_id"])
    open_events = set()
    for pid, o in opens_by_pid.items():
        if pid in closed_pids:
            continue
        md = o.get("signal_metadata") or {}
        if md.get("event_ticker") == event_ticker:
            open_events.add(event_ticker)
    return event_ticker in open_events


def open_series_counts() -> Dict[str, int]:
    """T7 Filter 15 helper: {series_id: count_of_open_positions}.
    Reads the synthetic series_id stored in signal_metadata at open time.
    Enforces MAX_PER_SERIES_OPENS across G1+G2 of a single matchup."""
    opens_by_pid: Dict[str, dict] = {}
    closed_pids = set()
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        if r.get("type") == "open":
            opens_by_pid[r["position_id"]] = r
        elif r.get("type") == "close":
            closed_pids.add(r["position_id"])
    counts: Dict[str, int] = defaultdict(int)
    for pid, o in opens_by_pid.items():
        if pid in closed_pids:
            continue
        md = o.get("signal_metadata") or {}
        sid = md.get("series_id")
        if sid:
            counts[sid] += 1
    return dict(counts)


def count_open_t7_positions() -> int:
    open_pids = set()
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        if r.get("type") == "open":
            open_pids.add(r["position_id"])
        elif r.get("type") == "close":
            open_pids.discard(r["position_id"])
    return len(open_pids)


def t7_current_exposure_usd() -> float:
    opens: Dict[str, dict] = {}
    closed = set()
    for r in _read_ledger():
        if r.get("engine") != ENGINE:
            continue
        if r.get("type") == "open":
            opens[r["position_id"]] = r
        elif r.get("type") == "close":
            closed.add(r["position_id"])
    return sum(
        float(o.get("cost_usd") or 0)
        for pid, o in opens.items() if pid not in closed
    )


def t7_live_bankroll() -> float:
    """Live T7 bankroll = INITIAL + sum(realized P&L from closed T7 positions)."""
    realized = 0.0
    for r in _read_ledger():
        if r.get("engine") != ENGINE or r.get("type") != "close":
            continue
        try:
            realized += float(r.get("realized_pnl_usd") or 0)
        except (TypeError, ValueError):
            continue
    return BANKROLL_USD_INITIAL + realized


def count_today_t7_opens() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    for r in _read_ledger():
        if r.get("engine") != ENGINE or r.get("type") != "open":
            continue
        ts = r.get("ts", "")
        if ts.startswith(today):
            n += 1
    return n


def trade_once(dry_run: bool) -> dict:
    flag = Path.home() / "Documents" / "freshness_alarm.flag"
    if flag.exists() and not dry_run:
        try:
            log(f"[freshness_alarm] flag present — forcing dry-run")
            log(f"[freshness_alarm] {flag.read_text().strip()}")
        except OSError:
            log(f"[freshness_alarm] flag present — forcing dry-run")
        dry_run = True

    now = datetime.now(timezone.utc)
    vegas_lines = load_latest_vegas_lines()
    log(f"loaded vegas: {len(vegas_lines)} games with consensus")

    kalshi_markets = load_latest_kalshi_markets()
    log(f"loaded kalshi: {len(kalshi_markets)} latest market rows (NBA+NHL)")

    if not vegas_lines or not kalshi_markets:
        log("insufficient data — skipping cycle")
        return {"opened": 0, "rejected": 0}

    n_open = count_open_t7_positions()
    n_today = count_today_t7_opens()
    exposure_usd = t7_current_exposure_usd()
    bankroll = t7_live_bankroll()
    series_counts = open_series_counts()
    log(f"current T7 state: {n_open} open positions ({n_today} today), "
        f"bankroll=${bankroll:.2f} (Δ${bankroll - BANKROLL_USD_INITIAL:+.2f}), "
        f"exposure=${exposure_usd:.2f}/${bankroll * MAX_TOTAL_EXPOSURE_PCT:.0f}, "
        f"series_with_opens={len(series_counts)}")
    if n_open >= MAX_TOTAL_OPENS:
        log(f"  [cap] total opens {n_open} ≥ {MAX_TOTAL_OPENS}, skipping")
        return {"opened": 0, "rejected": 0}
    if n_today >= MAX_DAILY_OPENS:
        log(f"  [cap] daily opens {n_today} ≥ {MAX_DAILY_OPENS}, skipping")
        return {"opened": 0, "rejected": 0}

    sl = ShadowLedger() if not dry_run else None
    snap_ts = now.isoformat()

    accepted = []
    rejected_counts: Dict[str, int] = defaultdict(int)
    near_misses: List[dict] = []
    for m in kalshi_markets:
        sig = evaluate_market(m, vegas_lines, now, series_counts)
        if sig is None:
            rejected_counts["null"] += 1
            continue
        if "reject_reason" in sig:
            rejected_counts[sig["reject_reason"][:35]] += 1
            if sig.get("_near_miss"):
                near_misses.append(sig)
            continue
        accepted.append(sig)

    # Title-parse-failed sentinel: surface count separately so anomalies
    # are visible even when other rejection categories dominate the log.
    # Lookup via the module-level constant so the key never drifts (H5 fix).
    # Note: rejected_counts truncates reason strings to 35 chars; the
    # constant TITLE_PARSE_FAILED_REASON is well below that limit.
    title_fail_count = rejected_counts.get(TITLE_PARSE_FAILED_REASON, 0)
    if title_fail_count > 0:
        log(f"[ALERT] title-parse-failed sentinel triggered: n={title_fail_count} "
            f"— Kalshi event title format may have changed. Inspect titles "
            f"in latest snapshot before next cycle.")

    if near_misses:
        import statistics
        kls = [m["kl_nats"] for m in near_misses]
        deltas = [m["abs_delta"] for m in near_misses]
        log(f"near-miss telemetry: n={len(near_misses)} "
            f"|delta| median={statistics.median(deltas):.4f} max={max(deltas):.4f} | "
            f"KL median={statistics.median(kls):.5f} max={max(kls):.5f} | "
            f"would-trigger-at-KL≥0.001: {sum(1 for k in kls if k >= 0.001)} "
            f"(KL≥0.005: {sum(1 for k in kls if k >= 0.005)})")
        try:
            nm_path = DATA_DIR / "near_miss_kl.jsonl"
            nm_path.parent.mkdir(parents=True, exist_ok=True)
            with open(nm_path, "a") as f:
                for m in near_misses:
                    rec = {"cycle_ts": snap_ts, **{k: v for k, v in m.items()
                                                    if k not in ("_near_miss",)}}
                    f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log(f"  [warn] near_miss_kl persistence failed: {e}")

    if rejected_counts:
        log("rejection breakdown:")
        for reason, n in sorted(rejected_counts.items(), key=lambda x: -x[1]):
            log(f"    {reason:<40} {n}")

    log(f"accepted signals: {len(accepted)}")

    accepted.sort(key=lambda s: -abs(s["delta"]))

    opened = 0
    for sig in accepted:
        if n_open + opened >= MAX_TOTAL_OPENS:
            log(f"  [cap-mid] total opens reached, stopping")
            break
        if n_today + opened >= MAX_DAILY_OPENS:
            log(f"  [cap-mid] daily opens reached, stopping")
            break
        if already_open_on_event(sig["event_ticker"]):
            log(f"  [skip] already open on {sig['event_ticker']}")
            continue

        # Re-check series cap after each fire — running counts include
        # any opens this cycle (H1 fix: counters now increment in BOTH
        # dry-run and live, so this check fires correctly in dry-run too).
        # Distinct labels so the dry-run log visibly demonstrates Filter 15
        # firing — the operator's pre-deploy validation gate now reflects
        # live behavior accurately.
        sid = sig.get("series_id")
        if sid and series_counts.get(sid, 0) >= MAX_PER_SERIES_OPENS:
            label = "[DRY] series_cap_would_block" if dry_run else "[skip-series-cap]"
            log(f"  {label} {sig['ticker']} series_id={sid} "
                f"count={series_counts.get(sid, 0)}/{MAX_PER_SERIES_OPENS}")
            continue

        # Entropy collapse defensive gate (mirrors T6).
        try:
            from entropy_alert_helpers import should_block_for_collapse
            blocked, br = should_block_for_collapse(
                ticker=sig["ticker"], proposed_side=sig["side"], engine=ENGINE,
            )
            if blocked:
                log(f"  [entropy-block] {sig['ticker']} {br}")
                continue
        except Exception as e:
            log(f"  [warn] entropy gate failed open: {e}")

        contracts, sizing_md = kelly_size_contracts(
            side=sig["side"],
            entry_price=sig["entry_price"],
            our_p=sig["sharp_p"],
            bankroll=bankroll,
            current_exposure_usd=exposure_usd,
        )
        if contracts == 0:
            log(f"  [skip-size] {sig['ticker']} {sizing_md.get('reason', '?')}")
            continue
        if sizing_md.get("capped_at_max"):
            # T7 audit M-T6-1 closer: surface MAX_CONTRACTS clipping.
            log(f"  [size-cap] {sig['ticker']} clipped to MAX_CONTRACTS={MAX_CONTRACTS}")

        cost_usd = contracts * sig["entry_price"]
        fee_usd = contracts * sizing_md.get("fee_per_contract", 0)

        log(f"  [{'FIRE' if not dry_run else 'DRY'}] {sig['ticker']:<40} "
            f"sport={sig['sport']} G{sig['game_num']} "
            f"{sig['side']} sz={contracts:>3} entry=${sig['entry_price']:.3f} "
            f"cost=${cost_usd:.2f} fee=${fee_usd:.2f} "
            f"kalshi={sig['kalshi_implied_p']:.3f} "
            f"sharp={sig['sharp_p']:.3f} "
            f"delta={sig['delta']:+.3f} "
            f"kl={sig.get('kl_nats', 0):.4f} "
            f"f_full={sizing_md.get('f_full', 0):.3f}")

        # H1 fix: counters update in BOTH dry-run and live so subsequent
        # signals in this cycle respect the running cap. Dry-run validation
        # gate must mirror live behavior; otherwise pre-deploy validation
        # lies about Filter 15 (the reviewer's concern). Ledger writes
        # remain gated on live mode.
        exposure_usd += contracts * sizing_md.get(
            "effective_stake_per_contract", sig["entry_price"]
        )
        if sid:
            series_counts[sid] = series_counts.get(sid, 0) + 1

        if not dry_run and sl is not None:
            md = {k: v for k, v in sig.items()}
            md["snap_ts"] = snap_ts
            md["sizing"] = sizing_md
            sl.open(
                engine=ENGINE,
                venue="kalshi",
                ticker=sig["ticker"],
                side=sig["side"],
                size=contracts,
                price=sig["entry_price"],
                signal_metadata=md,
                fee_usd=fee_usd,
            )
        opened += 1

    log(f"cycle done: opened={opened} accepted={len(accepted)} "
        f"rejected={sum(rejected_counts.values())} "
        f"title_parse_failed={title_fail_count}")
    return {"opened": opened, "accepted": len(accepted),
            "rejected": sum(rejected_counts.values()),
            "title_parse_failed": title_fail_count}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="Single cycle, then exit.")
    ap.add_argument("--live", action="store_true",
                    help="Write to ledger. Default is DRY-RUN.")
    ap.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC,
                    help="Cycle cadence in seconds (default 1800 = 30 min).")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dry_run = not args.live
    log(f"T7 NBA/NHL Paper Trader starting; live={args.live} once={args.once} "
        f"interval={args.interval_sec}s")
    log(f"  config: delta≥{DELTA_THRESHOLD} kelly={KELLY_FRACTION:.1f} "
        f"per-bet-cap={MAX_BET_PCT_BANKROLL:.0%} "
        f"total-exposure-cap={MAX_TOTAL_EXPOSURE_PCT:.0%} "
        f"bankroll_initial=${BANKROLL_USD_INITIAL} (live = initial + realized P&L) "
        f"taker_fee_rate={KALSHI_TAKER_FEE_RATE} "
        f"max_daily={MAX_DAILY_OPENS} max_total={MAX_TOTAL_OPENS} "
        f"max_per_series={MAX_PER_SERIES_OPENS} "
        f"max_game_num={MAX_GAME_NUM} "
        f"liquidity_floor=OI<{MIN_OPEN_INTEREST}∧v24<{MIN_VOLUME_24H}")

    if args.once:
        trade_once(dry_run)
        return 0

    while not _STOP_REQUESTED:
        try:
            trade_once(dry_run)
        except Exception as e:
            log(f"[error] cycle raised: {e}")
        slept = 0
        while slept < args.interval_sec and not _STOP_REQUESTED:
            time.sleep(1)
            slept += 1

    log("T7 NBA/NHL Paper Trader stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
