"""
KALSHI CATALYST (Engine 2) — directional trading on calendared binary events.

Paired with kalshi_shadow_v4.py (Engine 1: passive market making). This engine
takes the opposite structural bet: instead of capturing spread on slow two-sided
flow, it positions ahead of calendared catalysts where we believe Kalshi's
pricing disagrees with reality-based probability.

ARCHITECTURE
------------
Thesis-based. Each CatalystThesis specifies:
  - A calendar event or ongoing situation
  - Target contracts (Kalshi ticker patterns)
  - Our probability view of the outcome
  - The side we want (YES or NO)
  - Entry / target / stop prices
  - Max position size and correlation group

Execution: TAKER — crosses the spread on entry, unlike Engine 1's passive MM.
Shadow mode (default): simulates taker fills against live book without
submitting real orders. Same virtualization pattern as Engine 1.
Live mode (CATALYST_LIVE_MODE=1): submits real orders via REST. Do NOT enable
until shadow has validated the thesis-to-P&L pipeline over multiple events.

CAPITAL POOL
------------
Engine 2 shares the Phase-ladder cap structure with Engine 1. In shadow mode
both are virtual so capital "sharing" is just a reporting convention. When
going live, the combined exposure across both engines must respect the
phase-ladder global cap.

FIRST THESIS
------------
HFI Middle East oil inventory cliff (2026-Q2). Physics-driven supply math:
SPR can draw ~3M bpd, Gulf shut-ins removed ~11M bpd. In-transit buffer
evaporates mid-April. Market pricing quick-ceasefire/normalization probability
is optimistic relative to the physical inventory cliff.

Target: BUY NO on Hormuz-normalization / Iran-deal contracts that price
resolution as >35% probable. Exit when NO reaches 80¢ or thesis invalidated.
"""

import base64
import csv
import json
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

try:
    import websocket
except ImportError:
    raise SystemExit("Missing dependency. Run:\n  pip install websocket-client --break-system-packages")

# Reuse auth + parsing from the shadow MM module. Single source of truth for
# Kalshi API shape — if Kalshi changes headers / level format, only one file
# needs updating.
from kalshi_shadow_v4 import (
    load_private_key,
    sign_request,
    _parse_level,
    _price_key,
    compute_fee,
    FEE_RATE_TAKER,
    WS_URL,
)


# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

# Live mode gate: off by default. Never flip this on until shadow P&L has
# validated the thesis pipeline across multiple events.
CATALYST_LIVE_MODE = os.environ.get("CATALYST_LIVE_MODE", "0") == "1"

# Capital pool (virtual in shadow mode). Phase 1 Engine 2 allocation is
# ~40% of the $1000 phase cap — Engine 1 gets the other 60%.
CATALYST_GLOBAL_CAP_USD = float(os.environ.get("CATALYST_GLOBAL_CAP_USD", "400.0"))

# Per-thesis sizing cap — no single bet risks more than this fraction of
# the catalyst capital pool. Prevents one bad thesis from consuming the book.
MAX_THESIS_SIZE_FRACTION = 0.25    # 25% of $400 = $100 max per thesis

# Correlation cap — across all theses in the same `correlation_group`, combined
# exposure cannot exceed this fraction. Prevents stacking correlated bets.
MAX_CORRELATION_GROUP_FRACTION = 0.50

# Daily loss circuit breaker. If total session realized drops below this,
# halt new entries until manual reset.
DAILY_LOSS_BREAKER_USD = -40.0      # 10% of $400 pool

# Entry discipline: require a minimum edge vs market price before entering.
# 5¢ = 500 bps of implied-probability mispricing.
MIN_ENTRY_EDGE_DOLLARS = 0.05

# Post-stop cooldown: after a position closes on a stop-loss, block new entries
# on the same (thesis, ticker) pair for this many minutes. Without this, the
# bot re-enters immediately on the next book update where conditions are still
# met, producing a stop-loss → re-enter → stop-loss loop that accumulates
# taker fees faster than any edge can earn.
POST_STOP_COOLDOWN_MIN = 60

# Consecutive-stops auto-disable: if a thesis stops out N times in a row
# (across any of its tickers) without a target hit, deactivate the thesis for
# the session. The market is telling us our probability view is wrong.
CONSECUTIVE_STOPS_DISABLE_THESIS = 3

# Logs
CATALYST_FILLS_LOG = os.path.join(os.path.dirname(__file__), "kalshi_catalyst_fills.csv")
CATALYST_POS_LOG = os.path.join(os.path.dirname(__file__), "kalshi_catalyst_positions.csv")


# ══════════════════════════════════════════════════════════════════════
# Dataclasses
# ══════════════════════════════════════════════════════════════════════

@dataclass
class CatalystThesis:
    """A calendared binary-outcome bet with pre-committed execution rules.

    Semantics:
      - `our_probability`: our belief that the event resolves YES (0.0–1.0)
      - `side`: which side we buy. "YES" if we think market underprices event;
                "NO" if market overprices.
      - `max_entry_price`: only enter if the SIDE price is at or below this.
                           For side="NO" on a 40%-implied event, NO price is
                           0.60 and we might set max_entry_price=0.65 to allow
                           some slack.
      - `target_price`: exit when side price reaches this (take profit)
      - `stop_price`: exit when side price drops to this (cut loss)
      - `correlation_group`: theses in the same group are capped together
                             (e.g., all Middle East oil theses correlate on
                             the same underlying catalyst).

    Dynamic exit logic (added 2026-04-20):
      - `partial_take_fraction`: when price advances this fraction of the way
                                 from entry -> target, exit `partial_take_size`
                                 of the position. Default 0.65 / 0.5.
      - `breakeven_lock_fraction`: when price advances this fraction of the
                                   way from entry -> target, ratchet the stop
                                   up to `entry + breakeven_stop_buffer`.
                                   Default 0.40 / $0.02.
      These capture transient favorable moves before mean-reversion without
      prematurely stopping out on noise. Position-level state lives on
      CatalystPosition so different positions under the same thesis can
      independently ratchet.
    """
    id: str
    description: str
    calendar_event: str
    target_ticker_prefixes: List[str]   # match any contract starting with these
    side: str                            # "YES" or "NO"
    our_probability: float
    max_entry_price: float
    target_price: float
    stop_price: float
    max_position_usd: float
    correlation_group: str
    valid_until: datetime
    # Runtime state (populated as positions open):
    active: bool = True
    closed_reason: str = ""
    # Dynamic exit params — defaults are deliberately conservative. Override
    # per-thesis if needed. Set any to None to disable that feature.
    partial_take_fraction: float = 0.65    # 65% of entry->target distance
    partial_take_size: float = 0.5         # exit half on partial trigger
    breakeven_lock_fraction: float = 0.40  # 40% of entry->target distance
    # Ratchet buffer must exceed round-trip taker fees to actually lock a
    # gain. Kalshi taker = 7% * contracts * p * (1-p), so worst-case round
    # trip at p=0.5 is ~$0.035/contract. $0.05 buffer covers that plus a
    # small floor so a ratcheted stop-out books a win rather than a wash.
    breakeven_stop_buffer: float = 0.05


@dataclass
class CatalystPosition:
    """An open position tied to a specific thesis and contract."""
    thesis_id: str
    ticker: str
    side: str                  # YES or NO
    size: float                # contracts currently open (reduced on partial)
    entry_price: float         # dollars
    entry_ts: float            # unix ts
    current_price: float = 0.0
    realized_pnl: float = 0.0  # net of fees, cumulative (partials + final close)
    fees_paid: float = 0.0
    closed: bool = False
    close_reason: str = ""
    close_price: float = 0.0
    close_ts: float = 0.0
    # Dynamic-exit state (added 2026-04-20):
    original_size: float = 0.0        # preserved at entry; survives partials
    entry_fee: float = 0.0            # fee charged on entry; allocated pro-rata across exits
    partial_taken: bool = False       # True once partial take-profit has fired
    effective_stop: float = 0.0       # per-position stop; ratchets up on breakeven-lock


@dataclass
class ContractState:
    """Local mirror of a single Kalshi contract's order book, for the contracts
    our active theses care about. Simpler than MarketShadow — we only need
    top-of-book to decide taker fills."""
    ticker: str
    tick: float = 0.01
    yes_bid: float = 0.0       # highest yes bid price ($)
    yes_ask: float = 0.0       # lowest yes ask price ($)
    yes_bid_size: int = 0
    yes_ask_size: int = 0
    no_bid: float = 0.0        # highest no bid price ($)
    no_ask: float = 0.0        # lowest no ask price ($)
    no_bid_size: int = 0
    no_ask_size: int = 0
    # Raw level dicts (price → size) for book reconstruction
    yes_levels: Dict[float, float] = field(default_factory=dict)
    no_levels: Dict[float, float] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════
# Thesis registry
# ══════════════════════════════════════════════════════════════════════

# ── Thesis library (calibrated against candidates_v2.json snapshot 2026-04-20) ──
# Each thesis specifies our_probability (our belief the event resolves YES) and
# the side we'd BUY. Prices and DTRs in comments are from the most recent diag.
# Re-calibrate when market prices drift materially from the entry levels.

# ══════════════════════════════════════════════════════════════════════
# Correlation group: mideast_oil_crisis
# HFI thesis — physical inventory cliff makes near-term resolution unlikely.
# Term structure on Hormuz shows market prices progressive normalization;
# we believe the NEAR-term contracts overprice resolution probability.
# ══════════════════════════════════════════════════════════════════════

HFI_HORMUZ_MAY01 = CatalystThesis(
    id="hfi_hormuz_may01",
    description=(
        "Near-term Hormuz normalization (by 2026-05-01) is structurally implausible "
        "given physical inventory math: SPR ~3M bpd draw can't plug ~11M bpd Gulf "
        "shut-in gap, buffer evaporating, resolution timeline measured in months "
        "not weeks. Market prices 24% YES at 11 DTR."
    ),
    calendar_event="Hormuz 7d MA transit returns to baseline by 2026-05-01",
    target_ticker_prefixes=["KXHORMUZNORM-26MAR17-B260501"],
    side="NO",
    our_probability=0.10,           # we think ~10% chance
    max_entry_price=0.80,           # NO is currently ~$0.76, we enter <=$0.80
    target_price=0.92,              # exit NO at $0.92
    stop_price=0.55,                # exit if NO drops to $0.55 (thesis broken)
    max_position_usd=100.0,
    correlation_group="mideast_oil_crisis",
    valid_until=datetime(2026, 5, 1, 23, 59, tzinfo=timezone.utc),
)

HFI_HORMUZ_MAY15 = CatalystThesis(
    id="hfi_hormuz_may15",
    description=(
        "Hormuz normalization by 2026-05-15 (25 DTR) still premature — physical "
        "resolution takes 60-90 days from event peak, not 3-4 weeks. Market at "
        "42% YES; we believe 20-25%. Moderate edge relative to MAY01 variant."
    ),
    calendar_event="Hormuz 7d MA transit returns to baseline by 2026-05-15",
    target_ticker_prefixes=["KXHORMUZNORM-26MAR17-B260515"],
    side="NO",
    our_probability=0.22,
    max_entry_price=0.62,           # NO currently ~$0.57
    target_price=0.78,
    stop_price=0.40,
    max_position_usd=75.0,
    correlation_group="mideast_oil_crisis",
    valid_until=datetime(2026, 5, 15, 23, 59, tzinfo=timezone.utc),
)

HFI_IRAN_MAY = CatalystThesis(
    id="hfi_iran_may",
    description=(
        "US-Iran nuclear agreement by end of May 2026 (11 DTR) — market at 25% "
        "YES. Full diplomatic agreement in 11 days is near-impossible given "
        "typical multi-week negotiation cycles and concurrent Gulf tensions. "
        "Our view: 5-8%."
    ),
    calendar_event="US-Iran nuclear agreement signed by 2026-05-31",
    target_ticker_prefixes=["KXUSAIRANAGREEMENT-27-26MAY"],
    side="NO",
    our_probability=0.07,
    max_entry_price=0.80,           # NO currently ~$0.73
    target_price=0.92,
    stop_price=0.55,
    max_position_usd=100.0,
    correlation_group="mideast_oil_crisis",
    valid_until=datetime(2026, 5, 31, 23, 59, tzinfo=timezone.utc),
)

HFI_IRAN_JUN = CatalystThesis(
    id="hfi_iran_jun",
    description=(
        "US-Iran agreement by end of June (42 DTR) — market at 45% YES is "
        "optimistic. Full agreement in 6 weeks against backdrop of active "
        "Gulf tensions is possible but not 50/50. Our view: 20-25%."
    ),
    calendar_event="US-Iran nuclear agreement signed by 2026-06-30",
    target_ticker_prefixes=["KXUSAIRANAGREEMENT-27-26JUN"],
    side="NO",
    our_probability=0.22,
    max_entry_price=0.62,           # NO currently ~$0.54
    target_price=0.78,
    stop_price=0.40,
    max_position_usd=75.0,
    correlation_group="mideast_oil_crisis",
    valid_until=datetime(2026, 6, 30, 23, 59, tzinfo=timezone.utc),
)

# ══════════════════════════════════════════════════════════════════════
# Correlation group: political_calendar_fade
# Short-dated political binaries where retail mispricing is driven by news
# speculation rather than base rates. Fading these tends to be profitable
# when no hard catalyst is calendared.
# ══════════════════════════════════════════════════════════════════════

KASH_PATEL_MAY01 = CatalystThesis(
    id="kash_patel_may01",
    description=(
        "Kash Patel leaving FBI Director by 2026-05-01 (10 DTR) priced at 23% YES. "
        "Base rate for senior appointee exit within a specific 10-day window is "
        "~5-8% absent public resignation announcement. No such announcement. "
        "Market prices speculation premium we can fade."
    ),
    calendar_event="Kash Patel no longer FBI Director on 2026-05-01",
    target_ticker_prefixes=["KXKASHOUT-26APR-MAY01"],
    side="NO",
    our_probability=0.08,
    max_entry_price=0.82,           # NO currently ~$0.77
    target_price=0.94,
    stop_price=0.55,
    max_position_usd=100.0,
    correlation_group="political_calendar_fade",
    valid_until=datetime(2026, 5, 1, 23, 59, tzinfo=timezone.utc),
)

# ══════════════════════════════════════════════════════════════════════
# Correlation group: meme_retail_premium
# Speculative markets where retail enthusiasm creates consistent mispricing
# toward YES. Higher volatility — smaller size.
# ══════════════════════════════════════════════════════════════════════

UAP_FILES_FADE = CatalystThesis(
    id="uap_files_fade",
    description=(
        "'Trump releases new UFO files before 2027' priced at 86% YES is "
        "meme-retail extreme. Base rate for any politician delivering on a "
        "specific transparency agenda within a fixed timeline is 40-60% at "
        "best. Current NO is $0.12-$0.16 vs fair ~$0.45-0.55. Huge nominal "
        "edge but meme volatility — size conservatively."
    ),
    calendar_event="Trump releases new UFO files before 2027-01-01",
    target_ticker_prefixes=["KXUAPFILES-27"],
    side="NO",
    our_probability=0.50,
    max_entry_price=0.22,           # NO currently ~$0.12-0.16, enter <=$0.22
    target_price=0.40,              # conservative target given meme volatility
    stop_price=0.05,                # stop if NO drops to $0.05 (market flips)
    max_position_usd=50.0,          # half-size due to meme risk
    correlation_group="meme_retail_premium",
    valid_until=datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc),
)

THESES: List[CatalystThesis] = [
    HFI_HORMUZ_MAY01,
    HFI_HORMUZ_MAY15,
    HFI_IRAN_MAY,
    HFI_IRAN_JUN,
    KASH_PATEL_MAY01,
    UAP_FILES_FADE,
]


# ══════════════════════════════════════════════════════════════════════
# Globals
# ══════════════════════════════════════════════════════════════════════

CONTRACTS: Dict[str, ContractState] = {}   # ticker → book state
POSITIONS: List[CatalystPosition] = []     # all positions, open and closed
_STOP = False
_LOCK = threading.Lock()
_START = time.time()
_ACTIVE_WS = None


def handle_stop(signum, frame):
    global _STOP
    _STOP = True
    print("\n[catalyst] shutdown requested, closing WS...", flush=True)


signal.signal(signal.SIGINT, handle_stop)
signal.signal(signal.SIGTERM, handle_stop)


# ══════════════════════════════════════════════════════════════════════
# Risk / sizing helpers
# ══════════════════════════════════════════════════════════════════════

def _total_catalyst_exposure_usd() -> float:
    """Sum current market value of all open positions."""
    total = 0.0
    for p in POSITIONS:
        if p.closed:
            continue
        total += p.size * max(p.current_price, 0.001)
    return total


def _correlation_group_exposure_usd(group: str) -> float:
    total = 0.0
    for p in POSITIONS:
        if p.closed:
            continue
        t = _thesis_by_id(p.thesis_id)
        if t and t.correlation_group == group:
            total += p.size * max(p.current_price, 0.001)
    return total


def _thesis_by_id(tid: str) -> Optional[CatalystThesis]:
    for t in THESES:
        if t.id == tid:
            return t
    return None


def _session_realized_pnl() -> float:
    """Sum of realized P&L across all positions this session — including
    partial-take-profit gains on positions that remain open. Without this,
    the daily breaker would ignore realized partial gains that offset
    unrelated losses and trip too eagerly."""
    return sum(p.realized_pnl for p in POSITIONS)


def _compute_entry_size(thesis: CatalystThesis) -> Tuple[float, str]:
    """Return the allowable entry size in USD for this thesis, respecting all
    caps simultaneously. Returns (size_usd, reason_if_blocked).
    A returned size of 0 means entry is not allowed for any reason.

    Replaces the earlier can_enter + partial-retry logic, which had a bug:
    when we adjusted to a partial size under thesis_size_cap, we didn't
    re-validate the other caps and could breach them. This function sizes
    to the TIGHTEST cap in a single pass — no re-validation gap.
    """
    # Hard blocks first
    if not thesis.active:
        return 0.0, "thesis_inactive"
    if datetime.now(timezone.utc) >= thesis.valid_until:
        return 0.0, "thesis_expired"
    if _session_realized_pnl() < DAILY_LOSS_BREAKER_USD:
        return 0.0, "daily_loss_breaker"

    # Compute remaining room under each cap
    thesis_used = sum(
        p.size * max(p.current_price, 0.001)
        for p in POSITIONS if not p.closed and p.thesis_id == thesis.id
    )
    thesis_room = max(0.0, thesis.max_position_usd - thesis_used)

    group_cap = MAX_CORRELATION_GROUP_FRACTION * CATALYST_GLOBAL_CAP_USD
    group_used = _correlation_group_exposure_usd(thesis.correlation_group)
    group_room = max(0.0, group_cap - group_used)

    global_room = max(0.0, CATALYST_GLOBAL_CAP_USD - _total_catalyst_exposure_usd())
    single_bet_cap = MAX_THESIS_SIZE_FRACTION * CATALYST_GLOBAL_CAP_USD

    # Size to the tightest constraint
    size_usd = min(
        thesis.max_position_usd,
        thesis_room,
        group_room,
        global_room,
        single_bet_cap,
    )

    # Minimum viable size — below this, fees dominate
    if size_usd < 5.0:
        # Identify the binding constraint for telemetry
        reasons = []
        if thesis_room < 5.0: reasons.append("thesis_size_cap")
        if group_room < 5.0: reasons.append("correlation_cap")
        if global_room < 5.0: reasons.append("global_cap")
        return 0.0, (reasons[0] if reasons else "under_min_size")

    return size_usd, "ok"


# ══════════════════════════════════════════════════════════════════════
# Book update handlers (simplified from kalshi_shadow_v4)
# ══════════════════════════════════════════════════════════════════════

def _recompute_top_of_book(c: ContractState):
    """Recompute best bid/ask on both YES and NO sides from stored levels."""
    yes_active = {p: sz for p, sz in c.yes_levels.items() if sz > 0}
    no_active = {p: sz for p, sz in c.no_levels.items() if sz > 0}
    if yes_active:
        c.yes_bid = max(yes_active.keys())
        c.yes_bid_size = int(yes_active[c.yes_bid])
        # yes ask = 1 - no bid (complement pricing)
        if no_active:
            top_no_bid = max(no_active.keys())
            c.yes_ask = round(1.0 - top_no_bid, 6)
            c.yes_ask_size = int(no_active[top_no_bid])
        else:
            c.yes_ask = 0.0
            c.yes_ask_size = 0
    else:
        c.yes_bid = 0.0
        c.yes_bid_size = 0
    if no_active:
        c.no_bid = max(no_active.keys())
        c.no_bid_size = int(no_active[c.no_bid])
        if yes_active:
            top_yes_bid = max(yes_active.keys())
            c.no_ask = round(1.0 - top_yes_bid, 6)
            c.no_ask_size = int(yes_active[top_yes_bid])
        else:
            c.no_ask = 0.0
            c.no_ask_size = 0
    else:
        c.no_bid = 0.0
        c.no_bid_size = 0


def _apply_snapshot(c: ContractState, snap: dict):
    c.yes_levels = {}
    c.no_levels = {}
    for lvl in snap.get("yes_dollars_fp") or snap.get("yes") or []:
        parsed = _parse_level(lvl)
        if parsed and parsed[1] > 0:
            c.yes_levels[_price_key(parsed[0], c.tick)] = parsed[1]
    for lvl in snap.get("no_dollars_fp") or snap.get("no") or []:
        parsed = _parse_level(lvl)
        if parsed and parsed[1] > 0:
            c.no_levels[_price_key(parsed[0], c.tick)] = parsed[1]
    _recompute_top_of_book(c)


def _apply_delta(c: ContractState, delta: dict):
    price = delta.get("price_dollars") or delta.get("price") or delta.get("p")
    dsize = None
    for k in ("delta_fp", "delta", "d", "quantity_delta", "size_delta"):
        if k in delta:
            dsize = delta[k]
            break
    side = (delta.get("side") or delta.get("s") or "").lower()
    if price is None or dsize is None:
        return
    try:
        p = float(price)
        d = float(dsize)
    except (ValueError, TypeError):
        return
    key = _price_key(p, c.tick)
    target = c.yes_levels if side == "yes" else c.no_levels if side == "no" else None
    if target is None:
        return
    new_sz = target.get(key, 0.0) + d
    if new_sz <= 0:
        target.pop(key, None)
    else:
        target[key] = new_sz
    _recompute_top_of_book(c)


# ══════════════════════════════════════════════════════════════════════
# Shadow execution — thesis evaluation + simulated taker fills
# ══════════════════════════════════════════════════════════════════════

def _current_side_price(c: ContractState, side: str) -> Tuple[float, int]:
    """Return the (ask_price, ask_size) for the side we want to BUY.
    - Buying YES: we take the YES ask (someone else's YES offer)
    - Buying NO: we take the NO ask
    """
    if side == "YES":
        return c.yes_ask, c.yes_ask_size
    return c.no_ask, c.no_ask_size


def _mark_side_price(c: ContractState, side: str) -> float:
    """Current mid-market price on our side (for marking unrealized P&L).
    Uses bid for long positions — we'd sell at the bid."""
    if side == "YES":
        if c.yes_bid > 0 and c.yes_ask > 0:
            return (c.yes_bid + c.yes_ask) / 2.0
        return c.yes_bid or c.yes_ask or 0.0
    if c.no_bid > 0 and c.no_ask > 0:
        return (c.no_bid + c.no_ask) / 2.0
    return c.no_bid or c.no_ask or 0.0


def _ticker_matches_thesis(ticker: str, thesis: CatalystThesis) -> bool:
    return any(ticker.startswith(p) for p in thesis.target_ticker_prefixes)


def _recently_stopped(thesis_id: str, ticker: str) -> bool:
    """True if the (thesis, ticker) pair had a stop-loss exit within the
    cooldown window. Prevents rapid re-entry loops after a stop — if the
    book state that triggered the stop hasn't cleared, re-entering will
    just trigger another stop."""
    cutoff = time.time() - POST_STOP_COOLDOWN_MIN * 60
    for p in POSITIONS:
        if (p.closed and p.close_reason == "stop"
                and p.thesis_id == thesis_id and p.ticker == ticker
                and p.close_ts >= cutoff):
            return True
    return False


def _consecutive_stops_for_thesis(thesis_id: str) -> int:
    """Count consecutive stop-loss exits for a thesis, newest first.
    Resets on any target hit or currently-open position."""
    count = 0
    # Walk positions in reverse chronological order (entry_ts desc)
    matching = sorted(
        [p for p in POSITIONS if p.thesis_id == thesis_id],
        key=lambda x: -x.entry_ts,
    )
    for p in matching:
        if not p.closed:
            return 0   # open position breaks the streak
        if p.close_reason == "stop":
            count += 1
        else:
            break
    return count


def _evaluate_theses(c: ContractState):
    """For each active thesis matching this contract, check entry conditions.
    Called on every book update for tracked contracts."""
    for thesis in THESES:
        if not thesis.active:
            continue
        if not _ticker_matches_thesis(c.ticker, thesis):
            continue
        # Skip if we already have an open position in this contract for this thesis.
        if any(p.thesis_id == thesis.id and p.ticker == c.ticker and not p.closed
               for p in POSITIONS):
            continue
        # Skip if we were recently stopped out on this (thesis, ticker) pair.
        if _recently_stopped(thesis.id, c.ticker):
            continue
        # Auto-disable thesis after N consecutive stop-outs — our probability
        # view is empirically wrong and the market is telling us so.
        if _consecutive_stops_for_thesis(thesis.id) >= CONSECUTIVE_STOPS_DISABLE_THESIS:
            if thesis.active:
                thesis.active = False
                thesis.closed_reason = f"auto_disabled_after_{CONSECUTIVE_STOPS_DISABLE_THESIS}_stops"
                print(f"  ⛔ THESIS AUTO-DISABLED  {thesis.id}  "
                      f"(hit {CONSECUTIVE_STOPS_DISABLE_THESIS} consecutive stops — "
                      f"probability view is wrong)", flush=True)
            continue

        ask_px, ask_sz = _current_side_price(c, thesis.side)
        if ask_px <= 0 or ask_sz <= 0:
            continue   # no offer available
        # Entry filter: price must be at or below max_entry_price
        if ask_px > thesis.max_entry_price:
            continue
        # Edge filter: we need the price to differ from our probability by at least
        # MIN_ENTRY_EDGE_DOLLARS. For side=NO our expected fair price is (1-p); for
        # side=YES our expected fair is p. Buying BELOW fair is the edge.
        fair = (1.0 - thesis.our_probability) if thesis.side == "NO" else thesis.our_probability
        if fair - ask_px < MIN_ENTRY_EDGE_DOLLARS:
            continue

        # Size-to-tightest-cap. Single pass — no partial-entry validation gap.
        size_usd, reason = _compute_entry_size(thesis)
        if size_usd <= 0:
            continue

        # Size is also limited by offered depth — can't buy more than what's posted
        max_by_depth_contracts = ask_sz
        max_by_capital_contracts = size_usd / max(ask_px, 0.001)
        fill_contracts = min(max_by_depth_contracts, max_by_capital_contracts)
        if fill_contracts < 1.0:
            continue

        _shadow_enter(thesis, c, ask_px, fill_contracts)


def _shadow_enter(thesis: CatalystThesis, c: ContractState, price: float, size: float):
    """Simulate a taker entry — crosses the spread at the current ask."""
    fee = compute_fee(size, price, is_taker=True)
    pos = CatalystPosition(
        thesis_id=thesis.id,
        ticker=c.ticker,
        side=thesis.side,
        size=size,
        entry_price=price,
        entry_ts=time.time(),
        current_price=price,
        fees_paid=fee,
        original_size=size,
        entry_fee=fee,                       # preserved for pro-rata allocation
        effective_stop=thesis.stop_price,    # starts at thesis stop; ratchets up
    )
    POSITIONS.append(pos)
    with open(CATALYST_FILLS_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "ENTRY", thesis.id, c.ticker, thesis.side,
            f"{size:.2f}", f"{price:.4f}", f"{fee:.4f}", "",
        ])
    print(f"  🎯 CATALYST ENTRY  {c.ticker[:32]:<34s} {thesis.side} {size:.1f}@${price:.4f}  "
          f"fee=${fee:.3f}  [{thesis.id}]", flush=True)


def _check_exit_conditions(pos: CatalystPosition, c: ContractState):
    """Check whether an open position should act on:
      - daily-breaker force-close
      - breakeven stop ratchet (no exit — just moves effective_stop up)
      - partial take-profit (reduce size, keep rest running)
      - full target / stop / expiry exit

    Order matters: ratchet first so a price that triggers both ratchet
    and partial-take books the ratchet before the partial reduces the
    thinking-size basis. Stop check uses pos.effective_stop (which may
    have ratcheted above thesis.stop_price)."""
    thesis = _thesis_by_id(pos.thesis_id)
    if thesis is None:
        return

    # Current exit price: we'd sell at the bid for our side
    exit_bid = c.yes_bid if pos.side == "YES" else c.no_bid
    if exit_bid <= 0:
        return
    pos.current_price = exit_bid

    # Breaker preempts all other logic
    if _session_realized_pnl() < DAILY_LOSS_BREAKER_USD:
        _shadow_exit(pos, c, exit_bid, "daily_breaker")
        return

    # Expiry preempts as well (time is up regardless of price)
    if datetime.now(timezone.utc) >= thesis.valid_until:
        _shadow_exit(pos, c, exit_bid, "expired")
        return

    # Compute advance fraction (entry -> target distance), only meaningful
    # when the position is in the favorable direction.
    target_distance = thesis.target_price - pos.entry_price
    current_distance = exit_bid - pos.entry_price
    advance_fraction = (current_distance / target_distance) if target_distance > 0 else 0.0

    # Float-precision epsilon: threshold comparisons on computed ratios can
    # fall 1-2 ULPs short of clean fractions (e.g. (0.73-0.60)/(0.80-0.60)
    # evaluates to 0.6499999...). A 1e-9 slack matches a threshold set to
    # one in a billion, which is never a meaningful parameter value.
    EPS = 1e-9

    # 1) Breakeven stop ratchet — only moves stop UP, never back.
    if (thesis.breakeven_lock_fraction is not None
            and advance_fraction + EPS >= thesis.breakeven_lock_fraction):
        candidate = pos.entry_price + thesis.breakeven_stop_buffer
        # Don't let the ratchet overshoot the take-profit target
        candidate = min(candidate, thesis.target_price - 0.02)
        if candidate > pos.effective_stop:
            old = pos.effective_stop
            pos.effective_stop = candidate
            print(f"  🔒 STOP RATCHET   {c.ticker[:32]:<34s} {pos.side} "
                  f"stop ${old:.3f} -> ${candidate:.3f} "
                  f"(advance={advance_fraction:.0%})  [{pos.thesis_id}]", flush=True)

    # 2) Partial take-profit — once per position.
    if (not pos.partial_taken
            and thesis.partial_take_fraction is not None
            and advance_fraction + EPS >= thesis.partial_take_fraction):
        _partial_exit(pos, c, exit_bid, thesis.partial_take_size)
        # Partial exit does NOT close the position — remaining size continues
        # toward target (and the ratcheted effective_stop).
        # Skip further checks this tick to avoid double-actioning.
        return

    # 3) Full-exit checks against effective stop (may have ratcheted)
    if exit_bid >= thesis.target_price:
        _shadow_exit(pos, c, exit_bid, "target")
        return
    if exit_bid <= pos.effective_stop:
        reason = "stop_ratcheted" if pos.effective_stop > thesis.stop_price else "stop"
        _shadow_exit(pos, c, exit_bid, reason)
        return


def _partial_exit(pos: CatalystPosition, c: ContractState,
                  price: float, size_fraction: float):
    """Exit a fraction of the position; the position stays open with the
    remainder. Books realized P&L on the exited portion (including the
    pro-rata share of the entry fee) and marks partial_taken=True so this
    doesn't fire again for this position."""
    exit_size = pos.size * size_fraction
    remaining = pos.size - exit_size
    exit_fee = compute_fee(exit_size, price, is_taker=True)
    # Allocate entry fee pro-rata: this chunk's share of the entry cost
    # is (exit_size / original_size) of the original entry fee.
    entry_fee_share = pos.entry_fee * (exit_size / pos.original_size) \
        if pos.original_size > 0 else 0.0
    pnl_chunk = (price - pos.entry_price) * exit_size - exit_fee - entry_fee_share
    pos.realized_pnl += pnl_chunk
    pos.fees_paid += exit_fee
    pos.size = remaining
    pos.partial_taken = True
    with open(CATALYST_FILLS_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "PARTIAL_TAKE", pos.thesis_id, c.ticker, pos.side,
            f"{exit_size:.2f}", f"{price:.4f}", f"{exit_fee:.4f}", f"{pnl_chunk:+.4f}",
        ])
    print(f"  💰 PARTIAL TAKE   {c.ticker[:32]:<34s} {pos.side} "
          f"{exit_size:.1f}@${price:.4f}  pnl=${pnl_chunk:+.3f}  "
          f"remaining={remaining:.1f}  [{pos.thesis_id}]", flush=True)


def _shadow_exit(pos: CatalystPosition, c: ContractState, price: float, reason: str):
    """Simulate a taker exit on the position's REMAINING size.

    Books the final chunk's P&L (including its pro-rata share of the entry
    fee) and ADDS to pos.realized_pnl — which may already hold P&L from a
    prior partial take-profit. After this call pos.closed=True and the
    reported pnl is the position's lifetime net."""
    final_size = pos.size
    exit_fee = compute_fee(final_size, price, is_taker=True)
    # Entry-fee share for the remaining size. If the position never
    # partialed, original_size == final_size and this is the full entry fee.
    entry_fee_share = pos.entry_fee * (final_size / pos.original_size) \
        if pos.original_size > 0 else pos.entry_fee
    pnl_chunk = (price - pos.entry_price) * final_size - exit_fee - entry_fee_share
    pos.realized_pnl += pnl_chunk
    pos.fees_paid += exit_fee
    pos.close_price = price
    pos.close_ts = time.time()
    pos.close_reason = reason
    pos.closed = True
    pos.size = 0.0  # zero out so position disappears from "open" iterations
    with open(CATALYST_FILLS_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            f"EXIT_{reason.upper()}", pos.thesis_id, c.ticker, pos.side,
            f"{final_size:.2f}", f"{price:.4f}", f"{exit_fee:.4f}",
            f"{pnl_chunk:+.4f}",
        ])
    # Lifetime P&L (sum of all partials + this final chunk) — what matters
    # for attribution and the breaker.
    emoji = "🟢" if pos.realized_pnl > 0 else "🔴"
    note = f"(incl. ${(pos.realized_pnl - pnl_chunk):+.3f} partial)" \
        if pos.partial_taken else ""
    print(f"  {emoji} CATALYST EXIT   {c.ticker[:32]:<34s} {pos.side} "
          f"{final_size:.1f}@${price:.4f}  chunk_pnl=${pnl_chunk:+.3f}  "
          f"lifetime=${pos.realized_pnl:+.3f} {note}  [{reason}]", flush=True)


def _mark_all_positions():
    """Update current_price + unrealized P&L for all open positions. Called
    periodically so telemetry reflects live book state."""
    for pos in POSITIONS:
        if pos.closed:
            continue
        c = CONTRACTS.get(pos.ticker)
        if c is None:
            continue
        pos.current_price = _mark_side_price(c, pos.side)


# ══════════════════════════════════════════════════════════════════════
# Universe: which contracts do we need to subscribe to?
# ══════════════════════════════════════════════════════════════════════

def _validate_theses_or_die():
    """Startup validation. Raise SystemExit with a clear message on any
    invalid thesis configuration. Runs before WS connects — catches config
    bugs before they can cost money in live mode or waste cycles in shadow.

    Checks:
      - Unique thesis IDs
      - Probability in (0, 1) exclusive
      - Side is 'YES' or 'NO'
      - Entry / target / stop prices all in [0, 1]
      - Direction sanity: target > entry > stop for a long position
      - Max position usd is positive and <= single_bet cap
      - valid_until is in the future
      - At least one target_ticker_prefix per thesis
    """
    errors: List[str] = []
    seen_ids = set()
    now = datetime.now(timezone.utc)
    single_bet_cap = MAX_THESIS_SIZE_FRACTION * CATALYST_GLOBAL_CAP_USD

    for t in THESES:
        if t.id in seen_ids:
            errors.append(f"duplicate thesis id: {t.id}")
        seen_ids.add(t.id)

        if t.side not in ("YES", "NO"):
            errors.append(f"{t.id}: side must be YES or NO (got {t.side!r})")

        if not (0.0 < t.our_probability < 1.0):
            errors.append(f"{t.id}: our_probability must be in (0,1), got {t.our_probability}")

        for name, val in (("max_entry_price", t.max_entry_price),
                          ("target_price", t.target_price),
                          ("stop_price", t.stop_price)):
            if not (0.0 <= val <= 1.0):
                errors.append(f"{t.id}: {name} must be in [0,1], got {val}")

        # Long-position direction sanity: stop < entry < target
        if t.stop_price >= t.max_entry_price:
            errors.append(f"{t.id}: stop_price ({t.stop_price}) must be < max_entry_price "
                          f"({t.max_entry_price}) — stop above entry triggers immediately")
        if t.target_price <= t.max_entry_price:
            errors.append(f"{t.id}: target_price ({t.target_price}) must be > max_entry_price "
                          f"({t.max_entry_price}) — target below entry is unreachable")

        # Edge sanity: (fair - max_entry_price) must be >= MIN_ENTRY_EDGE_DOLLARS
        fair = (1.0 - t.our_probability) if t.side == "NO" else t.our_probability
        if fair - t.max_entry_price < MIN_ENTRY_EDGE_DOLLARS:
            errors.append(f"{t.id}: edge {(fair - t.max_entry_price):.3f} < "
                          f"MIN_ENTRY_EDGE_DOLLARS ({MIN_ENTRY_EDGE_DOLLARS}) — no entry "
                          f"will ever fire at these settings")

        if t.max_position_usd <= 0:
            errors.append(f"{t.id}: max_position_usd must be > 0")
        if t.max_position_usd > single_bet_cap:
            errors.append(f"{t.id}: max_position_usd ({t.max_position_usd}) > "
                          f"single_bet cap ({single_bet_cap}) — will always be clipped")

        if t.valid_until <= now:
            errors.append(f"{t.id}: valid_until ({t.valid_until.isoformat()}) has already passed")

        if not t.target_ticker_prefixes:
            errors.append(f"{t.id}: target_ticker_prefixes is empty")

        # Dynamic-exit param sanity
        if t.partial_take_fraction is not None:
            if not (0.0 < t.partial_take_fraction < 1.0):
                errors.append(f"{t.id}: partial_take_fraction must be in (0,1), "
                              f"got {t.partial_take_fraction}")
            if not (0.0 < t.partial_take_size <= 1.0):
                errors.append(f"{t.id}: partial_take_size must be in (0,1], "
                              f"got {t.partial_take_size}")
        if t.breakeven_lock_fraction is not None:
            if not (0.0 < t.breakeven_lock_fraction < 1.0):
                errors.append(f"{t.id}: breakeven_lock_fraction must be in (0,1), "
                              f"got {t.breakeven_lock_fraction}")
            if t.breakeven_stop_buffer < 0:
                errors.append(f"{t.id}: breakeven_stop_buffer must be >= 0, "
                              f"got {t.breakeven_stop_buffer}")
        # If both fractions are set, ratchet should trigger BEFORE partial —
        # breakeven_lock_fraction < partial_take_fraction. Otherwise a price
        # move that hits partial would skip the ratchet on that tick.
        if (t.breakeven_lock_fraction is not None
                and t.partial_take_fraction is not None
                and t.breakeven_lock_fraction >= t.partial_take_fraction):
            errors.append(f"{t.id}: breakeven_lock_fraction ({t.breakeven_lock_fraction}) "
                          f">= partial_take_fraction ({t.partial_take_fraction}) — "
                          f"ratchet must trigger before partial-take")

    if errors:
        print("\n⛔ THESIS CONFIG ERRORS — refusing to start:\n", flush=True)
        for e in errors:
            print(f"  - {e}", flush=True)
        print(flush=True)
        raise SystemExit("Fix thesis configuration and re-launch.")


def _fetch_target_tickers_with_prices() -> Dict[str, dict]:
    """Resolve ticker-prefix patterns into concrete open contracts via Kalshi REST.
    Returns {ticker: market_dict_with_prices} so the caller can print a pre-launch
    snapshot of current book prices + computed edges.
    """
    try:
        import requests
    except ImportError:
        raise SystemExit("requests required; run: pip install requests --break-system-packages")
    all_prefixes = set()
    for t in THESES:
        if t.active:
            all_prefixes.update(t.target_ticker_prefixes)
    if not all_prefixes:
        return {}
    base = "https://api.elections.kalshi.com/trade-api/v2"
    matched: Dict[str, dict] = {}
    cursor = None
    while True:
        params = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{base}/markets", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for m in data.get("markets", []):
            ticker = m.get("ticker", "")
            if any(ticker.startswith(p) for p in all_prefixes):
                matched[ticker] = m
        cursor = data.get("cursor")
        if not cursor or not data.get("markets"):
            break
    return matched


def _print_thesis_snapshot(matched: Dict[str, dict]):
    """Print a pre-launch snapshot: for each active thesis, show matched
    contracts with their current prices + computed edge. Would-enter-now
    markers so we can see if entries are likely imminent or waiting."""
    print("\n" + "=" * 90)
    print("THESIS SNAPSHOT — current market state + computed edges")
    print("=" * 90)
    for thesis in THESES:
        if not thesis.active:
            continue
        fair = (1.0 - thesis.our_probability) if thesis.side == "NO" else thesis.our_probability
        matched_for_thesis = {
            tk: m for tk, m in matched.items()
            if any(tk.startswith(p) for p in thesis.target_ticker_prefixes)
        }
        print(f"\n• {thesis.id}  [{thesis.correlation_group}]")
        print(f"    side={thesis.side}  our_p={thesis.our_probability:.2f}  "
              f"fair={fair:.2f}  entry≤${thesis.max_entry_price:.2f}  "
              f"target=${thesis.target_price:.2f}  stop=${thesis.stop_price:.2f}  "
              f"max=${thesis.max_position_usd:.0f}")
        if not matched_for_thesis:
            print(f"    ⚠ NO MATCHING OPEN CONTRACTS — thesis will never fire")
            continue
        for tk, m in matched_for_thesis.items():
            yb = float(m.get("yes_bid_dollars") or 0)
            ya = float(m.get("yes_ask_dollars") or 0)
            if yb <= 0 or ya <= 0:
                print(f"    {tk:<42s} no book")
                continue
            if thesis.side == "NO":
                our_ask = round(1.0 - yb, 4)    # price to BUY NO
                our_bid = round(1.0 - ya, 4)    # price to SELL NO
            else:
                our_ask = ya
                our_bid = yb
            edge = fair - our_ask
            would_enter = (our_ask <= thesis.max_entry_price
                           and edge >= MIN_ENTRY_EDGE_DOLLARS)
            marker = "🎯 WOULD ENTER" if would_enter else "       wait    "
            print(f"    {marker} {tk:<38s} ask=${our_ask:.3f} bid=${our_bid:.3f} "
                  f"edge={edge:+.3f}")


# ══════════════════════════════════════════════════════════════════════
# WebSocket handlers
# ══════════════════════════════════════════════════════════════════════

def _on_message(ws, raw):
    try:
        msg = json.loads(raw)
    except Exception:
        return
    msg_type = msg.get("type")
    msg_data = msg.get("msg") or {}
    ticker = msg_data.get("market_ticker") or msg_data.get("ticker")
    if not ticker or ticker not in CONTRACTS:
        return

    with _LOCK:
        c = CONTRACTS[ticker]
        if msg_type == "orderbook_snapshot":
            _apply_snapshot(c, msg_data)
            _evaluate_theses(c)
            # Also check all open positions on this contract for exit triggers
            for pos in [p for p in POSITIONS if not p.closed and p.ticker == ticker]:
                _check_exit_conditions(pos, c)
        elif msg_type == "orderbook_delta":
            _apply_delta(c, msg_data)
            _evaluate_theses(c)
            for pos in [p for p in POSITIONS if not p.closed and p.ticker == ticker]:
                _check_exit_conditions(pos, c)
        elif msg_type == "trade":
            # We don't need trade events for catalyst execution since we're
            # taker-based — book state is sufficient.
            pass


def _on_error(ws, err):
    print(f"  [catalyst ws error] {err}", flush=True)


def _on_close(ws, code, msg):
    print(f"  [catalyst ws closed] code={code} msg={msg}", flush=True)


def _on_open(ws):
    tickers = list(CONTRACTS.keys())
    print(f"  [catalyst ws open] subscribing to {len(tickers)} target contracts...", flush=True)
    sub = {"id": 1, "cmd": "subscribe",
           "params": {"channels": ["orderbook_delta"], "market_tickers": tickers}}
    ws.send(json.dumps(sub))


def _ws_run():
    global _ACTIVE_WS
    private_key = load_private_key()

    def subprotocol_or_header():
        from urllib.parse import urlparse
        parsed = urlparse(WS_URL)
        hdrs = sign_request(private_key, "GET", parsed.path)
        return [f"{k}: {v}" for k, v in hdrs.items()]

    while not _STOP:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                header=subprotocol_or_header(),
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            _ACTIVE_WS = ws
            ws.run_forever(ping_interval=25, ping_timeout=10)
            _ACTIVE_WS = None
        except Exception as e:
            print(f"  [catalyst ws exception] {e}", flush=True)
        if not _STOP:
            print("  [catalyst ws] reconnecting in 3s...", flush=True)
            time.sleep(3)


# ══════════════════════════════════════════════════════════════════════
# Logging + telemetry
# ══════════════════════════════════════════════════════════════════════

def _ensure_logs():
    if not os.path.exists(CATALYST_FILLS_LOG):
        with open(CATALYST_FILLS_LOG, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "action", "thesis_id", "ticker", "side",
                "size", "price", "fee", "pnl",
            ])


def _print_summary():
    now = time.time()
    elapsed_min = (now - _START) / 60.0
    with _LOCK:
        open_pos = [p for p in POSITIONS if not p.closed]
        closed_pos = [p for p in POSITIONS if p.closed]
        total_fees = sum(p.fees_paid for p in POSITIONS)
        realized = sum(p.realized_pnl for p in closed_pos)
        unrealized = sum((p.current_price - p.entry_price) * p.size for p in open_pos)
        exposure = _total_catalyst_exposure_usd()

    print(f"\n[catalyst] t+{elapsed_min:.0f}m  "
          f"open={len(open_pos)}  closed={len(closed_pos)}  "
          f"exposure=${exposure:.2f}/${CATALYST_GLOBAL_CAP_USD:.0f}  "
          f"realized=${realized:+.2f}  unrealized=${unrealized:+.2f}  "
          f"fees=${total_fees:.3f}", flush=True)
    if open_pos:
        for p in open_pos:
            age_min = (now - p.entry_ts) / 60.0
            upnl = (p.current_price - p.entry_price) * p.size
            print(f"         {p.ticker[:34]:<36s} {p.side} {p.size:.1f}@${p.entry_price:.4f} "
                  f"→ ${p.current_price:.4f}  upnl=${upnl:+.3f}  age={age_min:.0f}m  "
                  f"[{p.thesis_id}]", flush=True)


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main():
    if not KEY_ID or not PRIVATE_KEY_PATH:
        raise SystemExit("Missing env vars. Run: source ~/Documents/kalshi_secrets.env")
    if CATALYST_LIVE_MODE:
        raise SystemExit(
            "CATALYST_LIVE_MODE is enabled. Live trading is deliberately unsupported "
            "in MVP — shadow validation first. Unset the env var to run."
        )

    _ensure_logs()

    # Fail fast on bad thesis config BEFORE any capital is at risk.
    _validate_theses_or_die()

    print("=" * 90)
    print(f"KALSHI CATALYST (Engine 2) — SHADOW MODE — "
          f"{datetime.now().isoformat(timespec='seconds')}")
    print("=" * 90)
    print(f"Capital pool: ${CATALYST_GLOBAL_CAP_USD:.0f}  "
          f"(max_per_thesis=${CATALYST_GLOBAL_CAP_USD * MAX_THESIS_SIZE_FRACTION:.0f}  "
          f"corr_group=${CATALYST_GLOBAL_CAP_USD * MAX_CORRELATION_GROUP_FRACTION:.0f}  "
          f"daily_loss_breaker=${DAILY_LOSS_BREAKER_USD:.0f})")
    print(f"Safety rails: post_stop_cooldown={POST_STOP_COOLDOWN_MIN}min  "
          f"auto_disable_after={CONSECUTIVE_STOPS_DISABLE_THESIS}_consecutive_stops  "
          f"min_entry_edge=${MIN_ENTRY_EDGE_DOLLARS:.2f}")
    print(f"Active theses: {sum(1 for t in THESES if t.active)}/{len(THESES)}")

    print("\n[catalyst] resolving target tickers from Kalshi REST...")
    matched = _fetch_target_tickers_with_prices()
    if not matched:
        print("[catalyst] WARNING: no open contracts matched any thesis prefix.")
        print("[catalyst] Theses target:")
        for t in THESES:
            if t.active:
                print(f"  {t.id}: {t.target_ticker_prefixes}")
        print("[catalyst] Either prefixes are wrong, or these markets are not currently "
              "open on Kalshi. Update thesis definitions and restart.")
        raise SystemExit(1)

    print(f"[catalyst] matched {len(matched)} contracts")

    # Per-thesis snapshot with current prices and edge — makes pre-launch state
    # observable, and flags theses with no matching contracts before WS starts.
    _print_thesis_snapshot(matched)

    # Warn if any active thesis has zero matching contracts
    silent_theses = []
    for t in THESES:
        if not t.active:
            continue
        has_match = any(
            any(tk.startswith(p) for p in t.target_ticker_prefixes)
            for tk in matched
        )
        if not has_match:
            silent_theses.append(t.id)
    if silent_theses:
        print(f"\n⚠ {len(silent_theses)} active thesis/theses have no matching open contracts:")
        for tid in silent_theses:
            print(f"  - {tid}")
        print("These theses will never fire. Fix target_ticker_prefixes or deactivate.")

    for tk in matched:
        CONTRACTS[tk] = ContractState(ticker=tk)

    print("\n" + "=" * 90)
    print("STARTING WS — catalyst engine live in shadow mode")
    print("=" * 90)

    ws_thread = threading.Thread(target=_ws_run, daemon=True)
    ws_thread.start()

    last_summary = time.time()
    while not _STOP:
        time.sleep(5)
        with _LOCK:
            _mark_all_positions()
        if time.time() - last_summary >= 60:
            last_summary = time.time()
            _print_summary()

    print("\n[catalyst] FINAL SUMMARY")
    _print_summary()


if __name__ == "__main__":
    main()
