"""
KALSHI THESIS FACTORY — automated thesis-generation pipeline for Engine 2.

The problem this solves:
  kalshi_catalyst.py (Engine 2) executes hand-researched theses. That's
  a bottleneck. Each thesis requires a human to (1) find a candidate
  market, (2) calibrate a probability view, (3) write exact entry/stop/
  target rules. Generating 1-2 theses per week caps Engine 2's coverage
  long before capital is the constraint.

The factory turns that process into a daily, systematic scan:

  1. Paginate the full Kalshi market universe (REST, same pattern as
     diag_kalshi_v2.py).
  2. Apply base filters (liquidity, DTR window, two-sided book, exclude
     parlays / live sports / scoped-sports).
  3. Route each surviving candidate through sub-engine scorers. Each
     sub-engine encodes a *distinct* edge hypothesis:
        - calendar_fade: short-dated "will X happen by Y" contracts
          where implied YES probability overstates base rate of things
          actually happening on schedule.
        - tail_probability: long-dated improbable-YES events where
          time decay works for the NO buyer.
        - resolution_window_arb: detect non-monotonic implied
          probability across a family of contracts sharing an event
          ticker (longer DTR should have >= YES probability; when it
          doesn't, there's a structural leg to buy).
  4. Produce two outputs per run:
        - thesis_candidates_<timestamp>.json — machine-readable list
          of candidate CatalystThesis dicts (drop-in compatible with
          kalshi_catalyst.py's THESES registry after human review).
        - thesis_review_<timestamp>.md — human-readable top-N summary
          grouped by sub-engine, so Steve can skim in 2 minutes and
          approve/reject.

This is RESEARCH scaffolding, not an executor. The factory never
submits orders and never mutates Engine 2's runtime. A human approves
theses before they enter kalshi_catalyst.py.

Feedback loop:
  kalshi_thesis_tracker.py (companion — build after this) reads
  closed-position data from catalyst fills, tags by sub-engine, and
  measures per-sub-engine edge over time. That's how we'll decide
  which sub-engines get scaled and which get killed.

Usage:
  python3 kalshi_thesis_factory.py
  python3 kalshi_thesis_factory.py --min-vol 10000 --top-n 20

Output lands in ~/Documents/ alongside the other Kalshi artifacts.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests


# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Parlay tickers — multi-leg composite markets. Skip (no clean thesis).
PARLAY_PREFIXES = ("KXMVE", "KXMB", "KXMVECROSSCATEGORY", "KXMVESPORTS")

# Sports prefixes — these are fast-resolving binary sports outcomes which
# don't match the thesis-mispricing edge hypothesis. Engine 1 handles
# sports liquidity. Engine 2 doesn't want them.
SPORTS_PREFIXES = (
    "KXNFL", "KXNBA", "KXMLB", "KXNHL", "KXMLS", "KXPREM", "KXCHAMP",
    "KXPGATOUR", "KXATP", "KXWTA", "KXMARMAD", "KXUFC", "KXBOX",
    "KXNCAAF", "KXCFB", "KXCBB", "KXNASCAR", "KXF1",
    "KXCRICKET", "KXTENNIS", "KXSOCCER",
    # International / esports leagues (added from v1 audit — fills fade
    # lists with game-level outcomes that have nothing to do with the
    # "retail overweights speculation" edge hypothesis).
    "KXAFCCL", "KXSUPERLIG", "KXLALIGA", "KXBSL", "KXSERIE",
    "KXBUND", "KXLIGUE1", "KXIPL", "KXIPLGAME",
    "KXCS2", "KXLOL", "KXVALORANT", "KXDOTA",
    "KXRUGBY", "KXAFL", "KXCRL",
    # Added from v2 audit pass:
    "KXEPL", "KXFOMEN", "KXFOWOMEN",        # English Premier League, French Open M/W
    "KXARSENAL", "KXLIVERPOOL", "KXMANU", "KXMANCITY", "KXCHELSEA",
    "KXTEAMSIN", "KXTEAMSINNBAF",           # "teams in NBA Finals"-style futures
    "KXUSOPEN", "KXAUSOPEN", "KXWIMBLEDON", "KXROLANDGARROS",
    "KXCHAMPIONSLEAGUE", "KXEUROPA",
    "KXSUPERBOWL", "KXWORLDSERIES", "KXSTANLEYCUP",
    # Patch 2026-04-24: leakage fixes from factory review
    "KXPGATOP",                             # catches KXPGATOP5, KXPGATOP10, KXPGATOP20, etc.
    "KXCOPADOBRASIL", "KXCOPA",             # Brazilian Copa + generic Copa family
    "KXZURICHCLASSIC",                      # by-tournament PGA sub-markets
)

# Any ticker with "GAME" embedded after the prefix is a per-match sports
# outcome (e.g. KXAFCCLGAME-..., KXSUPERLIGGAME-...). Cheap safety net
# for leagues we haven't hand-listed above.
SPORTS_SUBSTRINGS = ("GAME",)

# Title keywords that strongly imply a sports market even when the
# ticker prefix slips through. Soccer / tennis / basketball futures
# frequently use non-obvious prefixes.
SPORTS_TITLE_KEYWORDS = (
    "french open", "wimbledon", "us open", "australian open",
    "premier league", "la liga", "bundesliga", "serie a", "ligue 1",
    "champions league", "europa", "stanley cup", "super bowl", "world series",
    "nba finals", "nhl finals", "nfl playoffs", "mlb playoffs",
    "trophy this season", "win the cup", "win the title",
    # Patch 2026-04-24: leakage fixes
    "zurich classic", "copa do brasil",
    "rotten tomatoes score",
)

# Crypto / FX / commodity daily & weekly price strikes. These are
# continuous-price binary options where implied probability reflects a
# forward-price distribution, NOT a retail speculation premium. The
# fade prior is invalid here — exclude from both fade scorers.
PRICE_STRIKE_PREFIXES = (
    "KXBTCD", "KXBTCW", "KXBTCM",          # BTC daily / weekly / monthly
    "KXETHD", "KXETHW", "KXETHM",
    "KXSOLD", "KXSOLW",
    "KXDOGED", "KXXRPD", "KXADAD",
    "KXCRYPTO",                             # generic crypto strike bucket
    "KXFX", "KXEURUSD", "KXUSDJPY",
    "KXGAS",                                # AAA gas price index
    "KXCPI", "KXPPI", "KXJOBSW",            # macro/BLS numeric strikes
    "KXAAAGAS",                             # AAA national avg gas price
    # Patch 2026-04-24: leakage fixes
    "KXRT",                                  # Rotten Tomatoes score buckets (0-100)
    "KXCCAPRICE",                            # California CARB carbon auction settlement price
    "KXPCE", "KXGDP", "KXPAYROLLS", "KXUNRATE",  # other macro strikes (T3b/3c territory)
)

# Trailing strike pattern: "-T<digits>(.<digits>)?" at the end of the
# ticker. Catches markets that got through the prefix net (e.g. custom
# strike baskets Kalshi adds week-to-week).
PRICE_STRIKE_REGEX = re.compile(r"-T\d+(\.\d+)?$")

# "Pick the winner" title patterns. These resolve on outcome selection
# (who wins a race / contest), not on a binary event firing — the
# fade-vs-base-rate prior doesn't apply. Exclude from calendar_fade.
PICK_WINNER_TITLE_PATTERNS = (
    re.compile(r"^\s*Who\b", re.IGNORECASE),
    re.compile(r"\bwho will win\b", re.IGNORECASE),
    re.compile(r"\bwho becomes\b", re.IGNORECASE),
    re.compile(r"\bwhich\b", re.IGNORECASE),
    re.compile(r"^\s*What will be\b", re.IGNORECASE),       # "top AI model" / "top song" style
    re.compile(r"^\s*What will the .* be\b", re.IGNORECASE),
    # "Will X be the Y nominee/candidate/pick/champion/MVP/winner..."
    # is structurally a pick-the-winner across members of an event.
    re.compile(r"\bbe the .*\b(nominee|candidate|winner|pick|choice|mvp|champion|"
               r"selection|representative|democratic nominee|republican nominee)\b",
               re.IGNORECASE),
    # Chart/ranking — "#1 on the Billboard", "top song of the week"
    re.compile(r"\bbe\s+#\s*\d+\b", re.IGNORECASE),
    re.compile(r"\btop\s+(song|artist|album|movie|show|film|ai|model|podcast|book|team)\b",
               re.IGNORECASE),
    # Cup/title/championship finals futures that slipped past the sports filter
    re.compile(r"\bwin the .*\b(primary|nomination|cup|title|final|championship|award|"
               r"trophy|prize|oscar|emmy|grammy)\b", re.IGNORECASE),
    # Patch 2026-04-24: Spotify/Apple/YouTube chart leaders are pick-winner
    re.compile(r"\bmost\s+(monthly|weekly|daily|total|annual|overall)?\s*"
               r"\w*\s*(listeners|viewers|streams|streamed|sales|downloads|"
               r"subscribers|followers|plays)\b", re.IGNORECASE),
    re.compile(r"^\s*Artist\b.*\bmost\b", re.IGNORECASE),
    # "Will X finish top N" — PGA / golf leaderboard style sub-markets
    re.compile(r"\bfinish\s+top\s+\d+\b", re.IGNORECASE),
)

# Default filter thresholds. Can be overridden via CLI.
DEFAULT_MIN_VOL_24H = 5_000.0      # liquidity floor (catalyst doesn't need deep books)
DEFAULT_MIN_DTR_DAYS = 2.0         # ignore ultra-short — too little room to be wrong
DEFAULT_MAX_DTR_DAYS = 400.0       # ignore very-long — decay math breaks down
DEFAULT_TOP_N = 25                 # top per sub-engine in the review doc

# Edge thresholds: minimum probability mispricing the scorer will accept.
# Measured in probability/dollar space (Kalshi prices = implied probabilities).
MIN_EDGE_CALFADE = 0.08            # 8¢ — half the typical spread of the HFI theses
MIN_EDGE_TAIL = 0.10               # 10¢ — tail bets need more cushion to survive vol

# Price band guardrails. We don't fade markets already near the extremes —
# the edge is in the meaty middle where retail has opinions.
CALFADE_YES_PROB_MIN = 0.25
CALFADE_YES_PROB_MAX = 0.85
TAIL_YES_PROB_MIN = 0.15
TAIL_YES_PROB_MAX = 0.50

# Conversion factor: our naive prior on our_probability given implied YES.
# For calendar-fade we believe implied YES overstates by ~50%. For tail
# we believe more aggressively — long-dated improbable events rarely
# fire. These are starting priors; tracker feedback refines them.
CALFADE_PRIOR_MULT = 0.5
TAIL_PRIOR_MULT = 0.35

# DTR buckets — used for routing and sizing. Shorter DTR = smaller size.
DTR_BAND_CALFADE = (3.0, 90.0)
# 2026-04-26: tail band capped at 90 days (was 400). Long-dated tail bets
# create a structural validation problem — at DTR=252 you wait 8+ months
# per realized data point, can't run a calibration cycle in any reasonable
# time. Restricting to ≤90 days keeps the thesis scope (improbable-YES
# decay) but ensures monthly-ish position turnover.
DTR_BAND_TAIL = (30.0, 90.0)

# Sizing defaults per sub-engine (in USD per thesis). Conservative; human
# can bump on review. These respect MAX_THESIS_SIZE_FRACTION=0.25 from
# kalshi_catalyst.py ($100 cap on a $400 pool).
SIZE_DEFAULT_CALFADE = 75.0
SIZE_DEFAULT_TAIL = 50.0

# Stop/target geometry: for a NO entry at E (in dollars), where E is
# implied probability of NO outcome, we want:
#   target: E + TARGET_GAIN (capped at 0.92 — no room to run beyond)
#   stop:   E - STOP_LOSS   (floored at 0.10 — avoid stopping into dust)
CALFADE_TARGET_GAIN = 0.13
CALFADE_STOP_LOSS = 0.18
TAIL_TARGET_GAIN = 0.18
TAIL_STOP_LOSS = 0.20

# Output locations
DOC_DIR = os.path.dirname(os.path.abspath(__file__))


# ══════════════════════════════════════════════════════════════════════
# Dataclasses
# ══════════════════════════════════════════════════════════════════════

@dataclass
class RawMarket:
    """Flattened subset of Kalshi's /markets response we actually use."""
    ticker: str
    event_ticker: str
    title: str
    category: str
    subtitle: str
    vol_24h: float
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    tick_step: float
    dtr: float                       # days to close
    close_time_iso: str
    open_interest: float


@dataclass
class ThesisCandidate:
    """Drop-in compatible with kalshi_catalyst.CatalystThesis fields.

    The catalyst module builds CatalystThesis dataclasses at import time
    from Python literals; the factory writes JSON here and a human
    transcribes approved ones into kalshi_catalyst.py. The JSON output
    matches the exact field names to make that transcription mechanical.
    """
    # Identity
    id: str
    sub_engine: str                  # NEW field (factory-only): which scorer fired
    score: float                     # NEW field (factory-only): ranking key
    # Thesis fields (mirror CatalystThesis)
    description: str
    calendar_event: str
    target_ticker_prefixes: List[str]
    side: str
    our_probability: float
    max_entry_price: float
    target_price: float
    stop_price: float
    max_position_usd: float
    correlation_group: str
    valid_until: str                 # ISO string (CatalystThesis uses datetime — convert on import)
    # Dynamic-exit params (mirror CatalystThesis defaults — can be
    # overridden per-candidate if a sub-engine wants tighter/looser rules)
    partial_take_fraction: float = 0.65
    partial_take_size: float = 0.5
    breakeven_lock_fraction: float = 0.40
    breakeven_stop_buffer: float = 0.05
    # Factory metadata (for review doc, not exported to catalyst)
    implied_yes_prob: float = 0.0
    dtr_days: float = 0.0
    vol_24h: float = 0.0
    market_title: str = ""
    edge_dollars: float = 0.0


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def fp_to_float(v) -> float:
    """Kalshi returns fixed-point strings; convert safely."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def is_parlay(ticker: str) -> bool:
    return any(ticker.startswith(p) for p in PARLAY_PREFIXES)


def is_sports(ticker: str, title: str = "") -> bool:
    if any(ticker.startswith(p) for p in SPORTS_PREFIXES):
        return True
    # Substring net: per-match outcomes often embed "GAME" between the
    # league slug and the date. Conservative — we'd rather exclude too
    # much than fade an NBA final.
    if any(s in ticker for s in SPORTS_SUBSTRINGS):
        return True
    if title:
        t = title.lower()
        if any(k in t for k in SPORTS_TITLE_KEYWORDS):
            return True
    return False


def is_price_strike(ticker: str) -> bool:
    """True if the ticker represents a continuous-price binary strike
    (BTC/ETH/gas/CPI/etc.) rather than a discrete-event market."""
    if any(ticker.startswith(p) for p in PRICE_STRIKE_PREFIXES):
        return True
    if PRICE_STRIKE_REGEX.search(ticker):
        return True
    return False


def is_pick_winner(title: str) -> bool:
    """True if title matches a 'who wins' pattern (outcome selection,
    not event firing)."""
    if not title:
        return False
    for pat in PICK_WINNER_TITLE_PATTERNS:
        if pat.search(title):
            return True
    return False


def parse_close_time(close_str: str) -> Tuple[float, str]:
    """Return (dtr_days, iso_string) from a Kalshi close_time string."""
    if not close_str:
        return (0.0, "")
    try:
        dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        dtr = (dt - now).total_seconds() / 86400.0
        return (dtr, dt.isoformat())
    except Exception:
        return (0.0, close_str)


def safe_event_ticker(m: dict) -> str:
    """Fallback event_ticker inference when the API omits one."""
    et = m.get("event_ticker") or ""
    if et:
        return et
    # Kalshi tickers are typically EVENT-DATE-OUTCOME. Strip the last dash
    # segment as a rough event-ticker proxy.
    t = m.get("ticker", "")
    if "-" in t:
        return "-".join(t.split("-")[:-1])
    return t


def paginate_markets(min_vol: float):
    """Stream Kalshi's open markets. Mirror diag_kalshi_v2 but with more fields.

    We fetch everything and filter in Python rather than trusting server-side
    filters — the /markets endpoint's volume filter is flaky across paginator
    pages.

    Handles 429 rate-limit with exponential backoff and inter-page throttle.
    Kalshi's /markets universe is ~40-60k markets, so we span ~40-60 pages at
    limit=1000. A 500ms inter-page sleep keeps us under the rate ceiling.
    """
    cursor = None
    page = 0
    INTER_PAGE_SLEEP_SEC = 0.5
    MAX_RETRIES = 6

    while True:
        params = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor

        # Retry loop: backoff on 429 / transient errors
        r = None
        for attempt in range(MAX_RETRIES):
            try:
                r = requests.get(f"{BASE}/markets", params=params, timeout=30)
            except requests.RequestException as e:
                wait = 2 ** attempt * 2
                print(f"  [net-err] page {page} attempt {attempt+1}: {e}. "
                      f"sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue

            if r.status_code == 429:
                # Respect Retry-After header if present, else exponential
                wait = int(r.headers.get("Retry-After", "0")) or (2 ** attempt * 5)
                print(f"  [429] page {page} attempt {attempt+1}. "
                      f"sleeping {wait}s then retrying...", file=sys.stderr)
                time.sleep(wait)
                continue

            if r.status_code == 200:
                break

            # Non-200, non-429: raise
            r.raise_for_status()
        else:
            # Ran out of retries
            print(f"  [fatal] page {page}: exhausted retries", file=sys.stderr)
            if r:
                r.raise_for_status()
            return

        data = r.json()
        batch = data.get("markets", [])
        yield from batch
        cursor = data.get("cursor")
        page += 1
        if page % 5 == 0:
            print(f"  [fetch] paged {page * 1000} raw markets...", file=sys.stderr)
        if not cursor or not batch:
            return
        time.sleep(INTER_PAGE_SLEEP_SEC)


def iter_offline_from_json(path: str):
    """Offline replacement for paginate_markets. Reads a diag-format
    candidates JSON (written by diag_kalshi_v2.py) and yields rows in
    the same shape as Kalshi's /markets response so the pipeline can
    run end-to-end without network.

    Uses only the subset of fields that exist in the diag output; the
    rest are synthesized from yes_bid/yes_ask (no_ask = 1 - yes_bid).
    Good enough for schema + scoring validation, NOT for production.
    """
    with open(path) as f:
        rows = json.load(f)
    now = datetime.now(timezone.utc)
    for r in rows:
        yb = r.get("yes_bid", 0.0)
        ya = r.get("yes_ask", 0.0)
        dtr = r.get("dtr") or 0.0
        # Synthesize close_time from dtr
        try:
            from datetime import timedelta
            close = now + timedelta(days=float(dtr))
            close_iso = close.isoformat()
        except Exception:
            close_iso = ""
        yield {
            "ticker": r.get("ticker", ""),
            "event_ticker": r.get("event_ticker", ""),
            "title": r.get("title", ""),
            "category": "",
            "subtitle": "",
            "volume_24h_fp": r.get("vol_24h", 0),
            "yes_bid_dollars": yb,
            "yes_ask_dollars": ya,
            "no_bid_dollars": max(0.0, 1.0 - ya) if ya > 0 else 0.0,
            "no_ask_dollars": max(0.0, 1.0 - yb) if yb > 0 else 0.0,
            "price_ranges": [{"step": r.get("tick_step", 0.01)}],
            "close_time": close_iso,
            "open_interest": 0,
        }


def flatten(m: dict) -> Optional[RawMarket]:
    """Convert a raw Kalshi market dict into our RawMarket, or None if junk."""
    ticker = m.get("ticker", "")
    title = m.get("title", "")
    if not ticker or is_parlay(ticker) or is_sports(ticker, title) or is_price_strike(ticker):
        return None
    vol = fp_to_float(m.get("volume_24h_fp"))
    dtr, iso = parse_close_time(m.get("close_time", ""))
    yb = fp_to_float(m.get("yes_bid_dollars"))
    ya = fp_to_float(m.get("yes_ask_dollars"))
    nb = fp_to_float(m.get("no_bid_dollars"))
    na = fp_to_float(m.get("no_ask_dollars"))
    # Require a two-sided YES book (we need a mid to score)
    if yb <= 0 or ya <= 0 or ya <= yb:
        return None
    tick_step = 0.01
    pr = m.get("price_ranges") or []
    if pr and isinstance(pr, list) and pr[0].get("step"):
        tick_step = fp_to_float(pr[0]["step"])
    return RawMarket(
        ticker=ticker,
        event_ticker=safe_event_ticker(m),
        title=m.get("title", ""),
        category=m.get("category", ""),
        subtitle=m.get("subtitle", ""),
        vol_24h=vol,
        yes_bid=yb,
        yes_ask=ya,
        no_bid=nb,
        no_ask=na,
        tick_step=tick_step,
        dtr=dtr,
        close_time_iso=iso,
        open_interest=fp_to_float(m.get("open_interest")),
    )


# ══════════════════════════════════════════════════════════════════════
# Sub-engine scorers
#
# Each returns None if the market doesn't qualify, or a ThesisCandidate
# with score set. Scores are relative within a sub-engine — higher is
# better. Cross-sub-engine comparison isn't meaningful.
# ══════════════════════════════════════════════════════════════════════

def _bounded(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def detect_outcome_series(markets: List[RawMarket]) -> set:
    """Identify event_tickers that are outcome-selection (pick-the-winner)
    rather than time-series of the same event.

    Rule: if an event_ticker has >=2 members whose DTRs all fall within
    a 0.5d window AND whose implied YES probabilities sum to ~100%
    (±10% tolerance), it's an outcome series. Individual legs of such
    events are not valid calendar-fade / tail-fade candidates regardless
    of their title phrasing — the scoring prior doesn't apply.

    Returns a set of event_ticker strings to exclude.
    """
    by_event: Dict[str, List[RawMarket]] = defaultdict(list)
    for m in markets:
        if m.event_ticker:
            by_event[m.event_ticker].append(m)
    outcome_series = set()
    for ev, members in by_event.items():
        if len(members) < 2:
            continue
        dtrs = [x.dtr for x in members]
        dtr_range = max(dtrs) - min(dtrs)
        sum_implied = sum((x.yes_bid + x.yes_ask) / 2.0 for x in members)
        # Small DTR spread + probabilities sum to ~100% = outcome series
        if dtr_range < 0.5 and 0.85 <= sum_implied <= 1.15:
            outcome_series.add(ev)
        # Even without same DTR, 3+ members with probabilities summing
        # to ~100% is almost always an outcome series (e.g. Survivor).
        elif len(members) >= 3 and 0.85 <= sum_implied <= 1.15:
            outcome_series.add(ev)
    return outcome_series


def score_calendar_fade(m: RawMarket, outcome_series: set) -> Optional[ThesisCandidate]:
    """Short-dated "will X happen by Y" fade.

    Edge hypothesis: Kalshi's retail flow overweights news/speculation
    relative to base rates. When implied YES probability is meaningfully
    above its base rate with short DTR, the NO side has positive EV
    because "things rarely happen on schedule."

    Sizing geometry uses the NO side — we enter on no_ask, target higher
    NO prices (as YES collapses toward 0), stop if NO drops too far.
    """
    lo, hi = DTR_BAND_CALFADE
    if m.dtr < lo or m.dtr > hi:
        return None
    if m.vol_24h < DEFAULT_MIN_VOL_24H:
        return None
    # Pick-the-winner markets don't match the fade-vs-base-rate prior
    if is_pick_winner(m.title):
        return None
    # Structural outcome-series detection (catches pick-winner markets
    # whose titles slip through the regex net — e.g., "Will X be #1").
    if m.event_ticker in outcome_series:
        return None
    # Implied YES mid as the "market's belief"
    implied_yes = (m.yes_bid + m.yes_ask) / 2.0
    if implied_yes < CALFADE_YES_PROB_MIN or implied_yes > CALFADE_YES_PROB_MAX:
        return None
    # Our prior: fade by CALFADE_PRIOR_MULT, floored/capped
    our_prob = _bounded(implied_yes * CALFADE_PRIOR_MULT, 0.02, 0.30)
    edge = implied_yes - our_prob  # in probability dollars
    if edge < MIN_EDGE_CALFADE:
        return None
    # Entry: NO ask = 1 - yes_bid. Use no_ask if live; else derive.
    no_ask = m.no_ask if m.no_ask > 0 else (1.0 - m.yes_bid)
    if no_ask <= 0 or no_ask >= 1:
        return None
    max_entry = _bounded(no_ask + 0.03, 0.05, 0.95)
    target = _bounded(no_ask + CALFADE_TARGET_GAIN, no_ask + 0.05, 0.92)
    stop = _bounded(no_ask - CALFADE_STOP_LOSS, 0.05, max_entry - 0.05)
    # Scoring: reward edge, liquidity, short-DTR convergence
    liquidity = math.log1p(m.vol_24h / 10_000.0)
    dtr_factor = 1.0 / math.sqrt(max(m.dtr, 1.0))
    score = edge * liquidity * dtr_factor
    return ThesisCandidate(
        id=f"calfade__{m.ticker.lower()}",
        sub_engine="calendar_fade",
        score=score,
        description=(
            f"Calendar fade: market at {implied_yes:.0%} YES with {m.dtr:.0f} DTR. "
            f"Base-rate prior suggests ~{our_prob:.0%}. Edge {edge:.3f} in probability "
            f"dollars. NO entry at ${no_ask:.3f}."
        ),
        calendar_event=f"{m.title} resolves by {m.close_time_iso[:10]}",
        target_ticker_prefixes=[m.ticker],   # EXACT ticker (v2 lesson)
        side="NO",
        our_probability=our_prob,
        max_entry_price=round(max_entry, 3),
        target_price=round(target, 3),
        stop_price=round(stop, 3),
        max_position_usd=SIZE_DEFAULT_CALFADE,
        correlation_group=m.event_ticker or "calfade_orphan",
        valid_until=m.close_time_iso,
        implied_yes_prob=implied_yes,
        dtr_days=m.dtr,
        vol_24h=m.vol_24h,
        market_title=m.title,
        edge_dollars=edge,
    )


def score_tail_probability(m: RawMarket, outcome_series: set) -> Optional[ThesisCandidate]:
    """Long-dated improbable-YES fade.

    Edge hypothesis: long-dated contracts pricing low-probability events
    at 15-50% YES tend to decay toward 0 as the deadline approaches
    without the event firing. Retail overpays for optionality. NO buyer
    collects the decay.

    Distinct from calendar_fade in (1) longer DTR band and (2) tighter
    probability band — we're targeting the retail "lottery ticket"
    market, not the "might actually happen" market.
    """
    lo, hi = DTR_BAND_TAIL
    if m.dtr < lo or m.dtr > hi:
        return None
    if m.vol_24h < DEFAULT_MIN_VOL_24H:
        return None
    if is_pick_winner(m.title):
        return None
    if m.event_ticker in outcome_series:
        return None
    implied_yes = (m.yes_bid + m.yes_ask) / 2.0
    if implied_yes < TAIL_YES_PROB_MIN or implied_yes > TAIL_YES_PROB_MAX:
        return None
    our_prob = _bounded(implied_yes * TAIL_PRIOR_MULT, 0.01, 0.25)
    edge = implied_yes - our_prob
    if edge < MIN_EDGE_TAIL:
        return None
    no_ask = m.no_ask if m.no_ask > 0 else (1.0 - m.yes_bid)
    if no_ask <= 0 or no_ask >= 1:
        return None
    max_entry = _bounded(no_ask + 0.03, 0.05, 0.95)
    target = _bounded(no_ask + TAIL_TARGET_GAIN, no_ask + 0.05, 0.94)
    stop = _bounded(no_ask - TAIL_STOP_LOSS, 0.05, max_entry - 0.05)
    liquidity = math.log1p(m.vol_24h / 10_000.0)
    # 2026-04-26: switched from log1p(dtr/30) (rewarded long DTR) to
    # 1/sqrt(dtr) (rewards short DTR). Combined with DTR_BAND_TAIL cap of
    # 90d, this kills the engine's previous bias toward 250+ DTR positions
    # that took 8+ months to realize. Keeps the same probabilistic edge
    # but selects for time-to-evidence.
    dtr_factor = 1.0 / math.sqrt(max(m.dtr, 1.0))
    score = edge * liquidity * dtr_factor
    return ThesisCandidate(
        id=f"tail__{m.ticker.lower()}",
        sub_engine="tail_probability",
        score=score,
        description=(
            f"Tail fade: {implied_yes:.0%} YES on improbable long-dated event "
            f"({m.dtr:.0f} DTR). Decay edge. Our prob {our_prob:.0%}, edge "
            f"{edge:.3f}. NO entry at ${no_ask:.3f}."
        ),
        calendar_event=f"{m.title} resolves by {m.close_time_iso[:10]}",
        target_ticker_prefixes=[m.ticker],
        side="NO",
        our_probability=our_prob,
        max_entry_price=round(max_entry, 3),
        target_price=round(target, 3),
        stop_price=round(stop, 3),
        max_position_usd=SIZE_DEFAULT_TAIL,
        correlation_group=m.event_ticker or "tail_orphan",
        valid_until=m.close_time_iso,
        implied_yes_prob=implied_yes,
        dtr_days=m.dtr,
        vol_24h=m.vol_24h,
        market_title=m.title,
        edge_dollars=edge,
    )


def detect_resolution_window_arb(markets: List[RawMarket]) -> List[dict]:
    """Find non-monotonic implied-probability curves across an event family.

    If an event has N resolution dates (e.g., KXUSAIRANAGREEMENT-27-26MAY,
    -26JUN, -26JUL, -26AUG, -26SEP), the implied YES probability should
    be monotonically non-decreasing as DTR increases — more time = more
    chance of YES firing.

    Non-monotonicity means the market has a structurally cheap leg.

    This returns a list of detected violations as dicts, NOT ThesisCandidates.
    Resolving one into an actionable trade requires choosing which leg
    to trade and a manual calibration step. The factory just flags.
    """
    by_event: Dict[str, List[RawMarket]] = defaultdict(list)
    for m in markets:
        if m.event_ticker:
            by_event[m.event_ticker].append(m)

    findings = []
    for event, members in by_event.items():
        if len(members) < 2:
            continue
        # SKIP outcome-series (mutually exclusive legs of same event):
        # if all members share essentially the same DTR, the legs are
        # "who wins" outcomes that sum to ~100%, not a time-series of
        # the same event's resolution probability. These are the
        # pick-the-winner markets the fade scorers already skip — they
        # just happen to live under one event_ticker.
        dtrs = [x.dtr for x in members]
        dtr_range = max(dtrs) - min(dtrs)
        if dtr_range < 0.5:          # all legs close within 12 hours
            continue
        # Also skip if implied probabilities sum to ~100% (outcome series
        # that happen to share an event ticker but not exactly a DTR).
        sum_implied = sum((x.yes_bid + x.yes_ask) / 2.0 for x in members)
        if 0.90 <= sum_implied <= 1.10 and len(members) >= 2:
            continue
        # Sort by DTR ascending
        sorted_m = sorted(members, key=lambda x: x.dtr)
        violations = []
        for i in range(len(sorted_m) - 1):
            a, b = sorted_m[i], sorted_m[i + 1]
            # Only compare legs that are actually separated in time.
            if b.dtr - a.dtr < 0.5:
                continue
            imp_a = (a.yes_bid + a.yes_ask) / 2.0
            imp_b = (b.yes_bid + b.yes_ask) / 2.0
            # Tolerance: 1.5 ticks of noise
            tol = 1.5 * max(a.tick_step, b.tick_step)
            if imp_a > imp_b + tol:
                violations.append({
                    "shorter": {"ticker": a.ticker, "dtr": round(a.dtr, 1), "implied_yes": round(imp_a, 3)},
                    "longer":  {"ticker": b.ticker, "dtr": round(b.dtr, 1), "implied_yes": round(imp_b, 3)},
                    "gap": round(imp_a - imp_b, 3),
                })
        if violations:
            findings.append({"event_ticker": event, "violations": violations,
                             "member_count": len(members)})
    # Sort findings by largest gap first
    findings.sort(key=lambda f: -max(v["gap"] for v in f["violations"]))
    return findings


# ══════════════════════════════════════════════════════════════════════
# Pipeline orchestrator
# ══════════════════════════════════════════════════════════════════════

def run_factory(min_vol: float, top_n: int, out_dir: str,
                offline_from: Optional[str] = None) -> Tuple[List[ThesisCandidate], List[dict], dict]:
    """Paginate, filter, score, rank. Returns (candidates, arb_findings, stats).

    If offline_from is a path to a diag-format candidates JSON, use that
    as the universe (no network). Use only for testing."""
    t0 = time.time()
    raw: List[RawMarket] = []
    seen = 0
    filtered_parlay = 0
    filtered_sports = 0
    filtered_strike = 0
    filtered_vol = 0
    filtered_book = 0
    filtered_dtr = 0
    if offline_from:
        print(f"[factory] OFFLINE mode — reading universe from {offline_from}", file=sys.stderr)
        source = iter_offline_from_json(offline_from)
    else:
        print(f"[factory] paginating /markets (min_vol=${min_vol:,.0f})...", file=sys.stderr)
        source = paginate_markets(min_vol)
    for m in source:
        seen += 1
        t = m.get("ticker", "")
        if is_parlay(t):
            filtered_parlay += 1
            continue
        if is_sports(t):
            filtered_sports += 1
            continue
        if is_price_strike(t):
            filtered_strike += 1
            continue
        rm = flatten(m)
        if rm is None:
            filtered_book += 1
            continue
        if rm.vol_24h < min_vol:
            filtered_vol += 1
            continue
        if rm.dtr < DEFAULT_MIN_DTR_DAYS or rm.dtr > DEFAULT_MAX_DTR_DAYS:
            filtered_dtr += 1
            continue
        raw.append(rm)

    print(f"[factory] scan done in {time.time() - t0:.1f}s. "
          f"raw={seen} parlay={filtered_parlay} sports={filtered_sports} "
          f"strike={filtered_strike} bad_book={filtered_book} low_vol={filtered_vol} "
          f"bad_dtr={filtered_dtr} survivors={len(raw)}", file=sys.stderr)

    # Precompute which event_tickers are outcome-selection (pick-winner)
    # series — individual legs in those shouldn't go through fade scorers.
    outcome_series = detect_outcome_series(raw)
    print(f"[factory] outcome-series event_tickers detected: {len(outcome_series)}",
          file=sys.stderr)

    candidates: List[ThesisCandidate] = []
    for m in raw:
        c = score_calendar_fade(m, outcome_series)
        if c:
            candidates.append(c)
        c = score_tail_probability(m, outcome_series)
        if c:
            candidates.append(c)

    # Rank per sub-engine, keep top_n each
    by_engine: Dict[str, List[ThesisCandidate]] = defaultdict(list)
    for c in candidates:
        by_engine[c.sub_engine].append(c)
    kept: List[ThesisCandidate] = []
    for eng, lst in by_engine.items():
        lst.sort(key=lambda x: -x.score)
        kept.extend(lst[:top_n])

    arb = detect_resolution_window_arb(raw)

    stats = {
        "scan_seconds": round(time.time() - t0, 2),
        "raw_markets_seen": seen,
        "filtered_parlay": filtered_parlay,
        "filtered_sports": filtered_sports,
        "filtered_price_strike": filtered_strike,
        "filtered_bad_book": filtered_book,
        "filtered_low_vol": filtered_vol,
        "filtered_bad_dtr": filtered_dtr,
        "survivors": len(raw),
        "outcome_series_events": len(outcome_series),
        "candidates_total": len(candidates),
        "candidates_kept": len(kept),
        "by_sub_engine": {k: len(v) for k, v in by_engine.items()},
        "arb_findings": len(arb),
    }
    return kept, arb, stats


# ══════════════════════════════════════════════════════════════════════
# Output writers
# ══════════════════════════════════════════════════════════════════════

def write_json(candidates: List[ThesisCandidate], arb: List[dict], stats: dict, path: str):
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "thesis_candidates": [asdict(c) for c in candidates],
        "resolution_window_arb_findings": arb,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"[factory] wrote {path}", file=sys.stderr)


def write_review_md(candidates: List[ThesisCandidate], arb: List[dict], stats: dict, path: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: List[str] = []
    lines.append(f"# Kalshi Thesis Factory — Review ({now})")
    lines.append("")
    lines.append("Ranked thesis candidates from automated scan. Human approves before")
    lines.append("any thesis enters `kalshi_catalyst.py`. Approval checklist per thesis:")
    lines.append("")
    lines.append("1. Does the market title match the factory's auto-description?")
    lines.append("2. Is our_probability defensible against a 30-second news check?")
    lines.append("3. Are entry/target/stop bounds tight enough vs. the book?")
    lines.append("4. Is the correlation_group correct, or should it roll up further?")
    lines.append("")
    lines.append("## Scan stats")
    lines.append("")
    lines.append(f"- scan time: {stats['scan_seconds']}s")
    lines.append(f"- raw markets seen: {stats['raw_markets_seen']:,}")
    lines.append(f"- after filters: {stats['survivors']:,}")
    lines.append(f"- total candidates (pre-rank): {stats['candidates_total']}")
    lines.append(f"- kept after top-N per engine: {stats['candidates_kept']}")
    lines.append(f"- resolution-window arb findings: {stats['arb_findings']}")
    lines.append("")

    by_engine: Dict[str, List[ThesisCandidate]] = defaultdict(list)
    for c in candidates:
        by_engine[c.sub_engine].append(c)

    for engine, lst in sorted(by_engine.items()):
        lines.append(f"## Sub-engine: `{engine}` ({len(lst)} candidates)")
        lines.append("")
        lines.append("| # | Ticker | Implied YES | DTR | Vol/day | Edge | Entry (NO) | Target | Stop | Score |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for i, c in enumerate(lst, 1):
            lines.append(
                f"| {i} | `{c.target_ticker_prefixes[0]}` | {c.implied_yes_prob:.0%} | "
                f"{c.dtr_days:.0f}d | ${c.vol_24h:,.0f} | {c.edge_dollars:.3f} | "
                f"{c.max_entry_price:.3f} | {c.target_price:.3f} | {c.stop_price:.3f} | "
                f"{c.score:.3f} |"
            )
        lines.append("")
        lines.append("### Details")
        lines.append("")
        for i, c in enumerate(lst, 1):
            lines.append(f"**#{i} — {c.market_title or c.id}**")
            lines.append("")
            lines.append(f"- id: `{c.id}`")
            lines.append(f"- ticker: `{c.target_ticker_prefixes[0]}`")
            lines.append(f"- side: **{c.side}** @ entry ≤ ${c.max_entry_price:.3f}")
            lines.append(f"- target: ${c.target_price:.3f}   stop: ${c.stop_price:.3f}")
            lines.append(f"- our_probability: {c.our_probability:.0%}  (implied YES: {c.implied_yes_prob:.0%})")
            lines.append(f"- correlation_group: `{c.correlation_group}`")
            lines.append(f"- size default: ${c.max_position_usd:.0f}")
            lines.append(f"- valid_until: {c.valid_until}")
            lines.append(f"- rationale: {c.description}")
            lines.append("")

    if arb:
        lines.append("## Resolution-window arbitrage findings")
        lines.append("")
        lines.append("Non-monotonic implied YES probability across a shared event ticker.")
        lines.append("Longer DTR should imply higher-or-equal YES probability — violations")
        lines.append("flag a structurally mispriced leg. Requires manual calibration.")
        lines.append("")
        for i, f in enumerate(arb[:15], 1):
            lines.append(f"### #{i} — `{f['event_ticker']}` ({f['member_count']} legs)")
            lines.append("")
            for v in f["violations"]:
                lines.append(
                    f"- `{v['shorter']['ticker']}` ({v['shorter']['dtr']}d @ "
                    f"{v['shorter']['implied_yes']:.0%} YES) > `{v['longer']['ticker']}` "
                    f"({v['longer']['dtr']}d @ {v['longer']['implied_yes']:.0%} YES)  "
                    f"gap: {v['gap']:.3f}"
                )
            lines.append("")
    else:
        lines.append("## Resolution-window arbitrage findings")
        lines.append("")
        lines.append("_None detected this scan._")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_Generated by `kalshi_thesis_factory.py`._")

    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(f"[factory] wrote {path}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Kalshi thesis factory — automated Engine 2 candidate generation")
    ap.add_argument("--min-vol", type=float, default=DEFAULT_MIN_VOL_24H,
                    help=f"Min 24h volume in USD (default {DEFAULT_MIN_VOL_24H})")
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                    help=f"Top candidates per sub-engine (default {DEFAULT_TOP_N})")
    ap.add_argument("--out-dir", default=DOC_DIR,
                    help=f"Output directory (default {DOC_DIR})")
    ap.add_argument("--offline-from", default=None,
                    help="Read universe from a diag-format JSON instead of the API (testing only)")
    args = ap.parse_args()

    candidates, arb, stats = run_factory(args.min_vol, args.top_n, args.out_dir,
                                         offline_from=args.offline_from)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(args.out_dir, f"thesis_candidates_{ts}.json")
    md_path = os.path.join(args.out_dir, f"thesis_review_{ts}.md")
    write_json(candidates, arb, stats, json_path)
    write_review_md(candidates, arb, stats, md_path)

    # Write "latest" convenience copies. If a prior run left behind a
    # symlink that we can't delete (sandbox mount permissions), fall
    # back to writing a non-timestamped copy under a different name
    # so the user isn't chasing a broken symlink.
    import shutil
    latest_json = os.path.join(args.out_dir, "thesis_candidates_latest.json")
    latest_md = os.path.join(args.out_dir, "thesis_review_latest.md")
    for src, dst in ((json_path, latest_json), (md_path, latest_md)):
        if os.path.islink(dst):
            try:
                os.remove(dst)
            except OSError:
                # Can't remove — write alongside with .new suffix
                alt = dst + ".new"
                shutil.copy(src, alt)
                print(f"[factory] WARN: could not remove stale symlink at {dst}; "
                      f"wrote to {alt} instead", file=sys.stderr)
                continue
        try:
            shutil.copy(src, dst)
        except OSError as e:
            print(f"[factory] WARN: could not update {dst}: {e}", file=sys.stderr)

    # Terminal summary
    print("")
    print("=" * 80)
    print(f"THESIS FACTORY — scan complete in {stats['scan_seconds']}s")
    print("=" * 80)
    print(f"  Survivors after filters:   {stats['survivors']:,}")
    print(f"  Candidates kept:           {stats['candidates_kept']}  "
          f"(by engine: {stats['by_sub_engine']})")
    print(f"  Resolution-window arb:     {stats['arb_findings']} findings")
    print("")
    print(f"  JSON:     {json_path}")
    print(f"  Review:   {md_path}")
    print(f"  Latest:   {latest_json}")
    print(f"  Latest:   {latest_md}")
    print("")


if __name__ == "__main__":
    main()
