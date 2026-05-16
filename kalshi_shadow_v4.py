"""
KALSHI SHADOW v5.1 — market-making simulator with market-driven gates
                     and two-sided flow discovery filter (Path 1).

The bot maintains virtual quotes on top of the live Kalshi book, tracking
what would fill against its quotes without actually submitting orders.
Output is a realistic P&L simulation including fees, inventory skew, and
liquidation staging.

ARCHITECTURE
------------
 • RSA-signed WebSocket subscription to the Kalshi market-data stream.
 • Candidate universe: top-N by tail-weighted spread, with fee-coverage
   pre-filter and periodic re-discovery.
 • Quoting: Avellaneda-Stoikov inventory skew + inside-spread placement.
 • Sizing: phase-driven global + per-market ceilings, scaled down by
   real-time book depth (see _effective_per_market_cap).
 • Gating: five real-time signals derived from the WS feed (no prefix
   tables, no time-of-day rules). Gates are evaluated both on book
   updates (pre-quote) AND pre-fill inside handle_trade, so rapid trade
   bursts cannot stack inventory faster than the signal reacts:
       1. Warmup / liveness   — market has been observed trading
       2. Book quality        — spread band, depth, two-sided
       3. Rolling volatility  — mid stdev over VOL_WINDOW_SEC
       4. Price velocity      — |Δmid| over VELOCITY_WINDOW_SEC
       5. Trade imbalance     — yes/no taker-volume ratio
 • Accumulation rate limit: MAX_BUY_ACCUMULATION_CONTRACTS per
   BUY_ACCUMULATION_WINDOW_SEC per market — a hard cap on inventory
   growth rate, independent of the gate signals.
 • Exit policy: urgency-tiered liquidation.
   - URGENT (gate exits, blacklist, discovery drop, net-growth): 3-stage
     cascade — passive maker → synthetic cross → hold to resolution.
   - NON-URGENT (stale_no_sells, turnover): passive maker indefinitely,
     never cross the spread. Hold to resolution if spread tightens.
   This prevents taker-fee cascades on slow-flow markets where forced
   cross-exits realize near-zero price moves at real fee cost.
 • Universe filter: discovery excludes markets with chronically one-sided
   observed taker flow (> 65% imbalance). Passive MM requires two-sided
   flow; retail-conviction markets (political binaries at mid>0.75 or <0.25)
   frequently have dominant one-way taker flow and no symmetric exit liquidity.
 • Concurrency cap: MAX_OPEN_MARKETS forces round-trip completion before
   opening new fronts. Orthogonal to the global $ cap.
 • Post-regime cooldown: markets that forced gate-driven exits are blocked
   from new BUY fills for REGIME_COOLDOWN_MIN to let info events decay.
 • Risk: global inventory cap, structural-break blacklist, turnover gate,
   net-growth detector, continuous fee-coverage check.

CONFIG
------
 • Phase ladder (SHADOW_PHASE env var): Phase 1 = $1k global / $200 per-mkt
   max; Phase 2 = $2k / $400; Phase 3 = $3.5k / $700.
 • Individual overrides: GLOBAL_INV_CAP_USD, MAX_PER_MARKET_USD.

IO
--
Reads candidates from kalshi_candidates_v2.json at startup; re-fetches from
the Kalshi REST API every DISCOVERY_INTERVAL_MIN minutes.
Writes fills / quotes / rewards to kalshi_shadow_*.csv (analyzer-compatible).
"""

import base64
import csv
import json
import math
import os
import signal
import threading
import time
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError:
    raise SystemExit("Missing dependency. Run:\n  pip install cryptography --break-system-packages")

try:
    import websocket
except ImportError:
    raise SystemExit("Missing dependency. Run:\n  pip install websocket-client --break-system-packages")


# ══════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════

# ── Kalshi API ─────────────────────────────────────────────────────
KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
API_BASE = os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
ET = zoneinfo.ZoneInfo("America/New_York")   # for timestamp rendering

# ── Logs + persistence ─────────────────────────────────────────────
CANDIDATES_PATH = os.path.join(os.path.dirname(__file__), "kalshi_candidates_v2.json")
TRADES_LOG = os.path.join(os.path.dirname(__file__), "kalshi_shadow_fills.csv")
QUOTES_LOG = os.path.join(os.path.dirname(__file__), "kalshi_shadow_quotes.csv")
REWARDS_LOG = os.path.join(os.path.dirname(__file__), "kalshi_shadow_rewards.csv")

# ── Universe construction ──────────────────────────────────────────
TOP_N_MARKETS = int(os.environ.get("SHADOW_TOP_N", "50"))
MIN_SPREAD_TICKS = 3
# DTR floor at discovery time. Low value lets intraday game markets (cricket,
# NBA games, UFC, NHL, etc.) enter the universe — jump risk on those is handled
# by the real-time gate architecture, not by excluding them from discovery.
MIN_DAYS_TO_RESOLUTION = 0.1           # 2.4 hours
DISCOVERY_INTERVAL_MIN = int(os.environ.get("DISCOVERY_INTERVAL_MIN", "120"))
DISCOVERY_ENABLED = os.environ.get("DISCOVERY_ENABLED", "1") != "0"
RANKING_HYSTERESIS_SIZE = 75           # markets below this rank are dropped on discovery
CORE_UNIVERSE_SURVIVAL_CYCLES = 2      # cycles to pin a market to core (discovery-immune)

# Tail-favoring rank: extremes get a small bonus (fee drag is structurally
# lowest at tails). v5.1: reduced from 2.0 — strong tail bias pushed the
# universe toward one-sided-flow political-binary markets where passive MM
# has no symmetric exit liquidity. Flow-balance filter in discovery handles
# this more surgically now.
MID_TAIL_LOW_MAX = 0.25
MID_TAIL_HIGH_MIN = 0.75
TAIL_BONUS_FACTOR = 1.0

# ── Capital / sizing (phase ladder for $10k compounding) ───────────
# Per-market cap is the CEILING. Actual exposure is depth-scaled
# (see _effective_per_market_cap). Advance by env var only.
#
#   Phase 1: $1,000 global / $200 per-market (gate telemetry validation)
#   Phase 2: $2,000 global / $400 per-market (realized PnL positive, no blowups)
#   Phase 3: $3,500 global / $700 per-market (consistent fee-positive sessions)
#
# Launch: SHADOW_PHASE=2 python3 kalshi_shadow_v4.py
# Override: GLOBAL_INV_CAP_USD=1500 MAX_PER_MARKET_USD=300 python3 ...
SHADOW_PHASE = os.environ.get("SHADOW_PHASE", "1")
_PHASE_CAPS: Dict[str, Tuple[float, float]] = {
    "1": (1000.0, 200.0),
    "2": (2000.0, 400.0),
    "3": (3500.0, 700.0),
}
_phase_global, _phase_per_mkt = _PHASE_CAPS.get(SHADOW_PHASE, _PHASE_CAPS["1"])
GLOBAL_INVENTORY_CAP_USD = float(os.environ.get("GLOBAL_INV_CAP_USD", _phase_global))
MAX_PER_MARKET_USD = float(os.environ.get("MAX_PER_MARKET_USD", _phase_per_mkt))

# Depth-driven sizing: a book with REFERENCE_DEPTH_CONTRACTS on both sides
# earns the full ceiling; thinner books scale proportionally.
REFERENCE_DEPTH_CONTRACTS = 100

QUOTE_SIZE_CONTRACTS = 100
GAMMA = 0.30                           # A-S risk aversion (skew per unit inventory)
SPREAD_WIDEN_MAX_PCT = 0.25            # extra spread widening at full inventory

# ── Kalshi fee model ───────────────────────────────────────────────
# Fee = rate × contracts × P × (1-P) per side. Maker fills = cheaper;
# taker (crossing) fills = expensive. Fee peaks at P=0.5.
FEE_RATE_MAKER = 0.0175
FEE_RATE_TAKER = 0.07

# ── Real-time gates (v5.0) ─────────────────────────────────────────
# Warmup: the market must have been observed trading at least this many
# times (all-time counter). Gate 0 for every market.
MIN_TRADES_FOR_WARMUP = 1

# Book quality — detect structurally broken books (huge gaps from failed
# price discovery). Absolute-dollar ceiling is mid-independent AND tick-
# independent: a $0.25 spread is broken regardless of market structure.
# Tick-denominated logic breaks on Kalshi's "tapered_deci_cent" markets
# where tick varies with price level ($0.001 at tails, $0.01 mid-range).
# Fee-coverage filter handles price-aware profitability separately.
MAX_SPREAD_DOLLARS = 0.25
# (No MIN_DEPTH_EACH_SIDE gate: depth-driven sizing in _effective_per_market_cap
#  already scales position to book depth. MIN_BID_DEPTH_FOR_ENTRY — a separate
#  adverse-selection filter for exit liquidity — lives in handle_trade.)

# Volatility: rolling stdev of mid over window
VOL_WINDOW_SEC = 600
MAX_ROLLING_VOL = 0.05                 # 5¢ stdev ceiling

# Velocity: |Δmid| over short window (regime-change detector)
#
# v5.9 (2026-04-21): relativize to current spread. The prior absolute
# threshold ($0.03/30s) was tick-regime-blind — far too loose on
# penny-tick wide-spread markets (e.g. 3¢ = 3 ticks out of a 90-tick
# spread = noise) and far too tight on cent-tick narrow-spread markets
# (e.g. 3¢ = full spread crossing on a 3-tick market). Run-to-run log
# comparison across identical config showed fast_move flipping from
# ~750/5min to ~3000/5min as the universe's active-market mix shifted —
# classic miscalibrated-yardstick fingerprint.
#
# New formulation: threshold = max(MAX_VELOCITY_FLOOR, FRAC × spread),
# capped by MAX_VELOCITY. Spread-relative handles both regimes; the
# floor catches dislocations in ultra-tight books; the ceiling is a
# pure safety net so a pathological wide book can't disable the gate.
VELOCITY_WINDOW_SEC = 30
MAX_VELOCITY_FLOOR = float(os.environ.get("MAX_VELOCITY_FLOOR", "0.005"))
VELOCITY_FRAC_SPREAD = float(os.environ.get("VELOCITY_FRAC_SPREAD", "0.5"))
MAX_VELOCITY = float(os.environ.get("MAX_VELOCITY", "0.05"))  # safety ceiling

# Imbalance: yes-taker vs no-taker volume ratio
IMBALANCE_WINDOW_SEC = 15              # short window so the signal reacts to bursts
MAX_IMBALANCE = 0.80                   # 80% one-sided flow = info asymmetry
MIN_IMBALANCE_SAMPLES = 5              # fail-open below this sample count

# Per-market buy accumulation rate limit: independent of gate signals, a hard
# cap on how fast we can grow inventory in any single market. Prevents burst
# fills from stacking faster than the gate can react.
MAX_BUY_ACCUMULATION_CONTRACTS = 20
BUY_ACCUMULATION_WINDOW_SEC = 60

# ── Hold-or-exit gate (when real-time conditions fail with inventory) ──
# Calibrated for mixed multi-day + intraday universe. Intraday game markets
# (cricket, NBA totals, UFC) need shorter exit horizons than the old
# multi-day political-futures defaults — the final 2 hours before resolution
# is max-volatility territory for game markets (scoring swings, OT, etc.).
DTR_THRESHOLD_FOR_OVERNIGHT_HOLD = 0.1  # days (2.4h) — reject holds within this much of resolution
PRICE_DEVIATION_EXIT_PCT = 0.15         # |cost_basis - mid| / cost_basis
OVERSIZED_POSITION_MULTIPLE = 2.0       # inv > Nx cap → force exit
CLOSE_TIME_EXIT_HOURS = 2.0             # exit within 2h of resolution (avoid final-hour chaos)

# ── Path-1 filters (v5.1): universe composition and concurrency ─────
# Two-sided flow filter: excludes markets where taker flow is chronically
# one-sided (retail conviction markets). Uses observed taker volume tracked
# cumulatively since subscription. Markets with insufficient observation
# pass through; next discovery cycle filters them once data accumulates.
MAX_OBSERVED_IMBALANCE = 0.65
MIN_OBSERVED_VOLUME_FOR_FILTER = 50     # contracts — below this, no filtering

# Concurrency cap: forces round-trip completion before opening new fronts.
# Orthogonal to global $ cap (which limits notional, not market count).
#
# Default 12 was calibrated for the original (TOP_N_MARKETS=50, Phase 1
# $1k) config where 12 markets at ~$85 avg depth-scaled exposure ≈ the
# $1k global cap. At larger universes (N=150+) or higher phases, 12
# becomes the binding constraint and should scale up proportionally:
#   ~ int(TOP_N_MARKETS * 0.25) at Phase 1 (25% of universe active)
#   ~ int(TOP_N_MARKETS * 0.40) at Phase 2 (more capital to deploy)
#   ~ int(TOP_N_MARKETS * 0.55) at Phase 3
# Override via SHADOW_MAX_OPEN env var. Raising past ~40 risks Python
# single-thread message backpressure on busy market regimes; test before
# pushing higher.
MAX_OPEN_MARKETS = int(os.environ.get("SHADOW_MAX_OPEN", "12"))

# Post-regime-exit cooldown: after a market forces a gate-driven exit
# (imbalance / velocity / vol), block new BUY fills for this long on that
# market. Info events typically decay in 30-90min; this prevents re-exposure
# to the same catalyst before the signal clears.
REGIME_COOLDOWN_MIN = 60

# ── Liquidation staging ────────────────────────────────────────────
LIQ_STAGE_1_DURATION_MIN = 10           # URGENT: passive maker (best_bid + 1 tick)
LIQ_STAGE_2_DURATION_MIN = 10           # URGENT: cross (best_bid, taker fee)
# After stage 2 → stage 3: hold to resolution, no further quotes.

# Slow-liq ceilings: a non-urgent liquidation cannot run forever. Escalates
# to URGENT cascade under either condition:
#   (a) within CLOSE_TIME_EXIT_HOURS of resolution — avoid the resolution gap
#   (b) held for > MAX_SLOW_LIQ_HOURS — absolute safety ceiling regardless of DTR
MAX_SLOW_LIQ_HOURS = 4.0

# ── Risk / safety rails ────────────────────────────────────────────
TURNOVER_WINDOW_MIN = 60                # window for turnover gate
TURNOVER_MIN_SELLS_PER_WINDOW = 2       # fewer sells than this in window → liquidate
NET_GROWTH_WINDOW_MIN = 30              # window for net-growth detector
NET_GROWTH_MAX_CONTRACTS = 50           # net buys - sells > this → liquidate
STRUCTURAL_BREAK_THRESHOLD = 3          # stage-2 entries before permanent blacklist
MIN_BID_DEPTH_FOR_ENTRY = 20            # min bid-queue to consume for exit

# v5.2 P&L-based blacklist. The v5.1c stage-1-passive routing made stage-2
# entries rare — which means STRUCTURAL_BREAK_THRESHOLD effectively never
# trips and bad markets keep bleeding silently. Diagnosed on 2026-04-21:
# IMPEACH-28-JAN01 burned -$5.70 across 22 fills before any blacklist
# would have caught it.
#
# This rail tracks cumulative realized P&L per market. After PNL_BLACKLIST_MIN_SELLS
# sells, if realized P&L is below PNL_BLACKLIST_LOSS_USD, the market is
# permanently disabled — cap_multiplier=0, urgent unwind on remaining inv.
PNL_BLACKLIST_MIN_SELLS = int(os.environ.get("PNL_BLACKLIST_MIN_SELLS", "5"))
PNL_BLACKLIST_LOSS_USD = float(os.environ.get("PNL_BLACKLIST_LOSS_USD", "-0.50"))

# v5.3 STUCK-STAGE-3 blacklist. Positions that enter liquidation stage 3
# (spread too tight for inside-maker, holding to resolution) have zero
# sells and can't hit the P&L blacklist. They sit as dead capital,
# silently draining the opportunity surface by occupying slots. Observed
# 2026-04-21: 9 markets sitting in stage 3 for hours, 427 re-print events.
#
# Fix: after STUCK_STAGE3_BLACKLIST_MIN minutes in stage 3, permanently
# disable the market (cap_multiplier=0) — prevents re-entry while letting
# the held inventory resolve naturally to close.
STUCK_STAGE3_BLACKLIST_MIN = int(os.environ.get("STUCK_STAGE3_BLACKLIST_MIN", "60"))
# Throttle repeated LIQ-STAGE-3 prints to once per N minutes per market.
# Spread flicker (tight↔wide) causes stage oscillation 1↔3 which otherwise
# re-prints on every transition. Silent reality: market is still stuck.
LIQ_STAGE3_PRINT_THROTTLE_MIN = int(os.environ.get("LIQ_STAGE3_PRINT_THROTTLE_MIN", "30"))

# ══════════════════════════════════════════════════════════════════════
# v5.4 — FEEDER ARCHITECTURE
#
# Engine 1 is repositioned as a signal generator + execution layer for
# Engine 2, not an alpha generator on its own. The edge in passive MM
# on Kalshi retail is compressed; real value comes from feeding
# Engine 2 with real-time flow/velocity/imbalance signals so catalyst
# entries can be augmented with microstructure confirmation.
# ══════════════════════════════════════════════════════════════════════

# Signal export — append-only JSONL that Engine 2 (or any downstream)
# can tail for real-time microstructure events.
SIGNALS_EXPORT_PATH = os.path.join(os.path.dirname(__file__),
                                    "kalshi_shadow_signals.jsonl")
SIGNAL_EXPORT_ENABLED = os.environ.get("SIGNAL_EXPORT_ENABLED", "1") == "1"

# Signal thresholds — what counts as a noteworthy event worth emitting.
# Tuned to be SELECTIVE — raw book updates would flood the file.
FLOW_BURST_MIN_CONTRACTS = float(os.environ.get("FLOW_BURST_MIN_CONTRACTS", "50"))
FLOW_BURST_WINDOW_SEC = int(os.environ.get("FLOW_BURST_WINDOW_SEC", "60"))
VELOCITY_SPIKE_MIN = float(os.environ.get("VELOCITY_SPIKE_MIN", "0.05"))
IMBALANCE_SUSTAINED_THRESHOLD = float(os.environ.get("IMBALANCE_SUSTAINED_THRESHOLD", "0.80"))
IMBALANCE_SUSTAINED_WINDOW_SEC = int(os.environ.get("IMBALANCE_SUSTAINED_WINDOW_SEC", "120"))
# Throttle: don't emit the same signal type on the same ticker more
# often than this. Prevents a single sustained event from filling the
# log with duplicates.
SIGNAL_EXPORT_THROTTLE_SEC = int(os.environ.get("SIGNAL_EXPORT_THROTTLE_SEC", "60"))

# v5.9c — Flow-signal publisher. Per-market real-time microstructure
# snapshot that Engine 2 consumes as a confirmation layer (not a
# standalone trigger). Published every FLOW_SIGNALS_PUBLISH_SEC via
# atomic tmp+rename so Engine 2 always reads a consistent file.
#
# Scoring (per market):
#   imbalance_score ∈ [0,1]   = |taker_imbalance| over IMBALANCE_WINDOW_SEC
#   velocity_score  ∈ [0,1]   = |Δmid|/threshold  over VELOCITY_WINDOW_SEC
#                               where threshold = v5.9 spread-relative gate
#   fill_bias       ∈ [-1,1]  = signed Δmid / max(spread, tick)  over
#                               FLOW_BIAS_WINDOW_SEC (price-direction signal,
#                               INDEPENDENT from imbalance so combining the
#                               two adds information)
#   flow_score      ∈ [0,1]   = W_IMB*imb + W_VEL*vel + W_BIAS*|fill_bias|
#   regime_state: pressure_up/down when flow_score > threshold AND sign of
#                 fill_bias; else stable.
#   pressure_direction: yes/no/neutral from sign of fill_bias with epsilon.
#
# Publish-only — Engine 1 does NOT trade off these values. The scoring is
# tunable via env without code edits to allow Engine 2 integration to
# calibrate the regime threshold against observed confirmation hit rate.
FLOW_SIGNALS_PATH = os.path.join(os.path.dirname(__file__),
                                  "engine1_flow_signals.json")
FLOW_SIGNALS_ENABLED = os.environ.get("FLOW_SIGNALS_ENABLED", "1") == "1"
FLOW_SIGNALS_PUBLISH_SEC = float(os.environ.get("FLOW_SIGNALS_PUBLISH_SEC", "2"))
# v5.9c.1: tightened to 0.7 to emit fewer but stronger signals for Engine 2.
FLOW_SIGNALS_THRESHOLD = float(os.environ.get("FLOW_SIGNALS_THRESHOLD", "0.7"))
FLOW_BIAS_WINDOW_SEC = int(os.environ.get("FLOW_BIAS_WINDOW_SEC", "15"))
# v5.9c.1: wider neutral band (0.08) — more resistance to micro-flicker
# at the pressure_direction boundary.
FLOW_BIAS_NEUTRAL_EPSILON = float(os.environ.get("FLOW_BIAS_NEUTRAL_EPSILON", "0.08"))
# v5.9c.1: persistence requirement for regime classification.
# regime_state publishes pressure_up/down only when the raw classification
# agrees for ≥ this many consecutive publishes. Cuts false-positive flips
# at the cost of one-publish (2s default) latency on genuine regime onsets.
FLOW_REGIME_MIN_CONSEC = int(os.environ.get("FLOW_REGIME_MIN_CONSEC", "2"))
# Max raw-classification history per market. Must be ≥ FLOW_REGIME_MIN_CONSEC.
FLOW_REGIME_HISTORY_LEN = 3
# Weights for flow_score. Spec: 0.4/0.3/0.3. Exposed for tuning;
# runtime validates sum ≈ 1.0 (warns but does not hard-fail).
FLOW_W_IMBALANCE = float(os.environ.get("FLOW_W_IMBALANCE", "0.4"))
FLOW_W_VELOCITY = float(os.environ.get("FLOW_W_VELOCITY", "0.3"))
FLOW_W_BIAS = float(os.environ.get("FLOW_W_BIAS", "0.3"))

# v5.4 — Min-edge-after-fees gate. Before posting a quote, require
# the EXPECTED net P&L per fill to clear a floor. Prevents quoting
# markets where the break-even math is marginal even before adverse
# selection. Units: dollars per contract expected net.
MIN_NET_EDGE_PER_FILL = float(os.environ.get("MIN_NET_EDGE_PER_FILL", "0.005"))

# v5.7 — MIN_NET_EDGE entry-quality gate. Tighter than v5.4: evaluates
# expected net edge per BUY fill using a conservative exit estimate
# (mid - 1 tick), both-leg maker fees, and explicit slippage reserve.
# Rejects candidate as "low_edge" when expected profit per contract
# doesn't clear MIN_NET_EDGE. Formula:
#   edge = (mid - tick - entry) - fee(entry) - fee(exit) - slippage
# Entry-side only — SELL quotes exist to unwind inventory, so they
# bypass this gate.
MIN_NET_EDGE = float(os.environ.get("MIN_NET_EDGE", "0.01"))
MIN_NET_EDGE_SLIPPAGE = float(os.environ.get("MIN_NET_EDGE_SLIPPAGE", "0.005"))

# v5.9f — post-subscription universe admission. v5.9d forensics showed
# 17/42 subscribed markets never achieved ever_valid=True: discovery
# admitted markets based on a stale Gamma REST snapshot whose
# bestBid/bestAsk had gone one-sided by WS subscribe time. Runtime drops
# those markets after a grace window so they stop polluting no_book
# rejection counts and stop taking evaluation cycles.
#
# MARKET_ADMISSION_GRACE_SEC: seconds from subscription before a market
#   that hasn't achieved a valid book once is dropped from active quoting.
#   30s tolerates slow-loading books without stranding broken ones.
# MARKET_ADMISSION_ENABLED: 0 disables the drop (env-wired rollback).
MARKET_ADMISSION_ENABLED = os.environ.get("MARKET_ADMISSION_ENABLED", "1") == "1"
MARKET_ADMISSION_GRACE_SEC = float(os.environ.get("MARKET_ADMISSION_GRACE_SEC", "30"))

# v5.9k — master quote kill switch. When disabled, compute_quotes()
# returns immediately after clearing any existing quote prices and
# the engine stops generating new positions (no fills, no fees). The
# rest of the engine continues running: WS subscription, orderbook
# state maintenance, admission logic, flow-signal publisher, and all
# telemetry. "Sensor mode" — Engine 1 as a Kalshi-data source without
# the MM strategy layer.
#
# Use SHADOW_QUOTE_ENABLED=0 to flip Engine 1 into sensor-only mode.
# Reversible — set back to 1 and restart to restore quoting. The v5.8
# inventory discipline fields are preserved so any existing inventory
# still unwinds normally (defense against crashing mid-position).
SHADOW_QUOTE_ENABLED = os.environ.get("SHADOW_QUOTE_ENABLED", "1") == "1"

# v5.9g — stuck-invalid admission revocation. The v5.9f logic only
# catches "never-valid" markets; a market that achieves ever_valid once
# and then transitions to locked / crossed / empty for the rest of the
# run is silent to admission but produces continuous no_book rejects.
# Observed in the v5.9f soak: KXMOVVAREDISTRICT locked at t+15s and
# generated ~235 no_book rejects per minute for the rest of the run.
# Revoke admission for markets stuck invalid > MARKET_STUCK_INVALID_SEC.
MARKET_STUCK_INVALID_SEC = float(os.environ.get("MARKET_STUCK_INVALID_SEC", "60"))

# v5.8 — Inventory / exit discipline. Days of session logs show the
# pattern: small wins + stuck inventory + one bad exit = flat. These
# knobs force harvest discipline:
#   MAX_INV_AGE_MIN: inventory held longer than this enters SLOW
#     liquidation (non-urgent passive maker). Stops new buys, biases
#     toward exit. 20 min default.
#   PROFIT_LOCK_THRESHOLD_USD: once realized P&L on a market clears
#     this floor AND inventory is still open, lock in the gain by
#     entering SLOW liquidation — don't let regime change flip a
#     winner to flat.
#   INV_TRAP_STRIKES_TO_DEMOTE: count URGENT liquidation entries
#     per-market. After N strikes, reduce cap_multiplier to
#     INV_TRAP_DEMOTE_MULT. Repeated forced-exits = structural loser.
#   NO_SELLS_WARN_MIN / NO_SELLS_ALERT_MIN: telemetry thresholds for
#     the minute-summary inventory-age line.
MAX_INV_AGE_MIN = float(os.environ.get("MAX_INV_AGE_MIN", "20"))
PROFIT_LOCK_THRESHOLD_USD = float(os.environ.get("PROFIT_LOCK_THRESHOLD_USD", "0.20"))
INV_TRAP_STRIKES_TO_DEMOTE = int(os.environ.get("INV_TRAP_STRIKES_TO_DEMOTE", "2"))
INV_TRAP_DEMOTE_MULT = float(os.environ.get("INV_TRAP_DEMOTE_MULT", "0.3"))
NO_SELLS_WARN_MIN = float(os.environ.get("NO_SELLS_WARN_MIN", "10"))
NO_SELLS_ALERT_MIN = float(os.environ.get("NO_SELLS_ALERT_MIN", "20"))

# v5.4 — Opportunity-cost deprioritization. After PNL_PER_SELL_MIN_SELLS
# sells, if (cumulative realized P&L / n sells) is below this floor,
# reduce cap_multiplier to PNL_PER_SELL_DEPRIO_MULT (default 0.3 = 30%
# of normal capacity). Soft demotion, not hard blacklist — the market
# can still trade at smaller size, just doesn't hog a slot.
PNL_PER_SELL_MIN = float(os.environ.get("PNL_PER_SELL_MIN", "0.005"))
PNL_PER_SELL_MIN_SELLS = int(os.environ.get("PNL_PER_SELL_MIN_SELLS", "10"))
PNL_PER_SELL_DEPRIO_MULT = float(os.environ.get("PNL_PER_SELL_DEPRIO_MULT", "0.3"))

# ── Fee-coverage (break-even + safety buffer) ──────────────────────
# Break-even spread (ticks) = (2 × FEE_RATE_MAKER × P × (1-P) / tick) + 2.
# Buffer adds margin for mid drift and spread compression during hold.
# 1.25× on dual-taker pessimistic break-even ≈ 1.5× true margin given
# stage-1 maker exits dominate in practice.
FEE_COVERAGE_BUFFER = 1.25


# ══════════════════════════════════════════════════════════════════════
# Pure helpers (fee math, candidate scoring)
# ══════════════════════════════════════════════════════════════════════

def compute_fee(contracts: float, price: float, is_taker: bool) -> float:
    """Kalshi per-side fee: rate × contracts × P × (1-P)."""
    rate = FEE_RATE_TAKER if is_taker else FEE_RATE_MAKER
    return rate * contracts * price * (1 - price)


# ══════════════════════════════════════════════════════════════════════
# v5.4 feeder — signal export for Engine 2 consumption
# ══════════════════════════════════════════════════════════════════════

def _emit_signal(s: "MarketShadow", signal_type: str,
                 magnitude: float, direction: str = "",
                 extra: Optional[dict] = None):
    """Append a microstructure signal to the JSONL export file for
    downstream consumption (Engine 2). Throttled per (ticker, type)
    via s.last_signal_export_ts to prevent flood from sustained events.
    """
    if not SIGNAL_EXPORT_ENABLED:
        return
    now = time.time()
    last = s.last_signal_export_ts.get(signal_type, 0.0)
    if now - last < SIGNAL_EXPORT_THROTTLE_SEC:
        return
    s.last_signal_export_ts[signal_type] = now
    row = {
        "ts": now,
        "ticker": s.ticker,
        "type": signal_type,
        "magnitude": round(magnitude, 4),
        "direction": direction,
        "best_bid": round(s.best_bid, 4),
        "best_ask": round(s.best_ask, 4),
        "inventory": float(s.inventory),
    }
    if extra:
        row.update(extra)
    try:
        with open(SIGNALS_EXPORT_PATH, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        # Signal export failure must never break the main loop
        print(f"  [signal-export] {e}", flush=True)


def _check_flow_burst(s: "MarketShadow"):
    """Detect a sudden burst of taker volume — a real-time signal that
    someone believes something is about to happen. Emits 'flow_burst'
    with magnitude in contracts and direction (buy/sell)."""
    if not SIGNAL_EXPORT_ENABLED:
        return
    now = time.time()
    window_start = now - FLOW_BURST_WINDOW_SEC
    # Use recent_fills which tracks (ts, size, side)
    recent_buys = sum(sz for (t, sz, side) in s.recent_fills
                      if side == 'B' and t >= window_start)
    recent_sells = sum(sz for (t, sz, side) in s.recent_fills
                       if side == 'S' and t >= window_start)
    if recent_buys >= FLOW_BURST_MIN_CONTRACTS:
        _emit_signal(s, "flow_burst", magnitude=recent_buys, direction="buy",
                     extra={"window_sec": FLOW_BURST_WINDOW_SEC})
    if recent_sells >= FLOW_BURST_MIN_CONTRACTS:
        _emit_signal(s, "flow_burst", magnitude=recent_sells, direction="sell",
                     extra={"window_sec": FLOW_BURST_WINDOW_SEC})


def _check_velocity_spike(s: "MarketShadow", velocity: float):
    """Emit 'velocity_spike' when absolute price velocity exceeds threshold.
    Caller passes current velocity computed via _price_velocity."""
    if not SIGNAL_EXPORT_ENABLED:
        return
    if velocity >= VELOCITY_SPIKE_MIN:
        _emit_signal(s, "velocity_spike", magnitude=velocity,
                     extra={"window_sec": VELOCITY_WINDOW_SEC})


def _check_imbalance_sustained(s: "MarketShadow", imbalance: float,
                               direction: str):
    """Emit 'imbalance_sustained' when book imbalance exceeds threshold
    with a specific side dominating. Caller passes current imbalance
    ratio (0-1) and direction ('bid'-heavy or 'ask'-heavy)."""
    if not SIGNAL_EXPORT_ENABLED:
        return
    if abs(imbalance) >= IMBALANCE_SUSTAINED_THRESHOLD:
        _emit_signal(s, "imbalance_sustained", magnitude=imbalance,
                     direction=direction,
                     extra={"window_sec": IMBALANCE_SUSTAINED_WINDOW_SEC})


def min_profitable_ticks(mid: float, tick: float) -> int:
    """Minimum spread (in ticks) required for a fee-positive round-trip,
    including FEE_COVERAGE_BUFFER margin."""
    if mid <= 0 or mid >= 1 or tick <= 0:
        return 999999   # invalid market — impossible to qualify
    single_rt = (2 * FEE_RATE_MAKER * mid * (1 - mid) / tick) + 2
    return math.ceil(single_rt * FEE_COVERAGE_BUFFER)


# v5.9h — low_edge near-miss instrumentation. For every `low_edge` gate
# rejection, record the edge value + book state so the distribution can
# be examined end-of-minute. Answers: are rejections clustered just
# below MIN_NET_EDGE (real edge, tune threshold) or deep below (no
# edge on this universe, threshold won't help).
_LOW_EDGE_NEAR_MISS_CAP = int(os.environ.get("LOW_EDGE_NEAR_MISS_CAP", "2000"))
_LOW_EDGE_NEAR_MISS: List[Tuple[int, str, float, float, float, float]] = []


def _record_low_edge_near_miss(s: "MarketShadow", edge: float, mid: float,
                                spread: float, tick: float) -> None:
    """Push (ts_ms, ticker, edge, mid, spread, tick) into the near-miss
    buffer. Capped to prevent unbounded growth."""
    ts_ms = int(time.time() * 1000)
    _LOW_EDGE_NEAR_MISS.append((ts_ms, s.ticker, edge, mid, spread, tick))
    if len(_LOW_EDGE_NEAR_MISS) > _LOW_EDGE_NEAR_MISS_CAP:
        del _LOW_EDGE_NEAR_MISS[:len(_LOW_EDGE_NEAR_MISS) - _LOW_EDGE_NEAR_MISS_CAP]


# v5.9i market-family classifier. Heuristic mapping from Kalshi ticker
# prefix / substring to a market family. Used by the focused analysis
# emitter to answer: is low_edge concentrated in a few families worth
# keeping for Engine 1, or broadly distributed (signal-feed only)?
def _classify_family(ticker: str) -> str:
    t = (ticker or "").upper()
    if t.startswith("KXHIGH"):
        return "weather"
    if any(s in t for s in ("ITF", "NFL", "NBA", "LALIGA", "EFL",
                             "COPPA", "HEISMAN", "MARMAD", "SOCCER",
                             "NBAT", "NBAS", "NBAC", "NBASERIES",
                             "NBACOY", "NFLD", "MLB", "NHL", "UFC")):
        return "sports"
    if any(s in t for s in ("TRUMP", "VOTEHUB", "KASHOUT", "IRAN",
                             "TARIFF", "DHS", "MOVNJ", "MOVVA",
                             "VOTE", "ELECT", "CONGRESS", "SHUTDOWN",
                             "PRIMARY", "SENATE")):
        return "politics"
    if any(s in t for s in ("WTI", "CRYPTO", "AAAGAS", "GAS",
                             "OIL", "BTC", "ETH")):
        return "commodities"
    if any(s in t for s in ("RT-", "MOVIE", "RATING", "BOXOFFICE",
                             "OSCAR", "EMMY")):
        return "entertainment"
    return "misc"


def _emit_low_edge_family_analysis() -> None:
    """v5.9i focused analysis of the low_edge near-miss buffer. Three
    sections: (1) top 20 markets by rejection frequency, (2) per-market
    median metrics, (3) split by market family with central-tendency
    stats. Called once at t+10min and again at final summary. Safe to
    invoke with empty buffer."""
    import statistics
    if not _LOW_EDGE_NEAR_MISS:
        print("[analysis/low_edge] buffer empty — no low_edge rejections yet",
              flush=True)
        return

    # Group by ticker.
    by_ticker: Dict[str, List[Tuple[int, str, float, float, float, float]]] = {}
    for rec in _LOW_EDGE_NEAR_MISS:
        by_ticker.setdefault(rec[1], []).append(rec)

    # Section 1: top 20 by frequency with per-market medians.
    ranked = sorted(by_ticker.items(), key=lambda kv: -len(kv[1]))[:20]
    print(f"\n{'=' * 90}")
    print(f"[analysis/low_edge] TOP-20 BY FREQUENCY "
          f"(total buffer entries: {len(_LOW_EDGE_NEAR_MISS)})")
    print("=" * 90)
    print(f"  {'ticker':<34s}  {'count':>6s}  {'med_edge':>10s}  "
          f"{'med_spread':>10s}  {'med_mid':>7s}  {'tick':>7s}  {'family':<12s}")
    for ticker, recs in ranked:
        edges = [r[2] for r in recs]
        mids = [r[3] for r in recs]
        spreads = [r[4] for r in recs]
        ticks = [r[5] for r in recs]
        fam = _classify_family(ticker)
        print(f"  {ticker[:34]:<34s}  {len(recs):>6d}  "
              f"{statistics.median(edges):>+10.5f}  "
              f"{statistics.median(spreads):>10.4f}  "
              f"{statistics.median(mids):>7.3f}  "
              f"{statistics.median(ticks):>7.4f}  {fam:<12s}")

    # Section 2: per-family aggregation.
    by_family: Dict[str, List[Tuple[int, str, float, float, float, float]]] = {}
    for rec in _LOW_EDGE_NEAR_MISS:
        fam = _classify_family(rec[1])
        by_family.setdefault(fam, []).append(rec)

    print(f"\n{'=' * 90}")
    print("[analysis/low_edge] PER-FAMILY BREAKDOWN")
    print("=" * 90)
    print(f"  {'family':<14s}  {'count':>7s}  {'uniq_tkrs':>9s}  "
          f"{'med_edge':>10s}  {'p25':>10s}  {'p75':>10s}  "
          f"{'med_spread':>10s}  {'med_mid':>7s}")

    fam_rows = sorted(by_family.items(), key=lambda kv: -len(kv[1]))
    for fam, recs in fam_rows:
        edges = sorted(r[2] for r in recs)
        mids = [r[3] for r in recs]
        spreads = [r[4] for r in recs]
        n = len(edges)
        p25 = edges[max(0, int(n * 0.25))]
        p75 = edges[min(n - 1, int(n * 0.75))]
        uniq = len({r[1] for r in recs})
        print(f"  {fam:<14s}  {n:>7d}  {uniq:>9d}  "
              f"{statistics.median(edges):>+10.5f}  {p25:>+10.5f}  {p75:>+10.5f}  "
              f"{statistics.median(spreads):>10.4f}  "
              f"{statistics.median(mids):>7.3f}")
    print("=" * 90 + "\n", flush=True)


def _low_edge_distribution() -> Dict[str, int]:
    """v5.9h: bucket the current near-miss buffer by edge-minus-threshold.
    Returns a dict keyed by bucket label."""
    buckets = {
        "pos_below_thr": 0,   # [0, MIN_NET_EDGE)  — unlocks with lower threshold
        "near_neg":      0,   # [-0.001, 0)        — very close
        "small_neg":     0,   # [-0.003, -0.001)
        "med_neg":       0,   # [-0.01, -0.003)
        "far_neg":       0,   # < -0.01
    }
    for (_ts, _tk, edge, _m, _s, _t) in _LOW_EDGE_NEAR_MISS:
        if 0 <= edge < MIN_NET_EDGE:
            buckets["pos_below_thr"] += 1
        elif -0.001 <= edge < 0:
            buckets["near_neg"] += 1
        elif -0.003 <= edge < -0.001:
            buckets["small_neg"] += 1
        elif -0.01 <= edge < -0.003:
            buckets["med_neg"] += 1
        elif edge < -0.01:
            buckets["far_neg"] += 1
    return buckets


def _expected_buy_edge(entry_price: float, mid: float, tick: float) -> float:
    """v5.7: expected net edge per BUY contract assuming maker fill at
    entry_price and a conservative maker exit at (mid - 1 tick).
    Subtracts both-leg maker fees and the slippage reserve.
    Returns dollars per contract. Used by the MIN_NET_EDGE gate."""
    if entry_price <= 0 or mid <= 0 or tick <= 0:
        return -1.0
    exit_est = max(tick, mid - tick)
    fee_entry = FEE_RATE_MAKER * entry_price * (1 - entry_price)
    fee_exit = FEE_RATE_MAKER * exit_est * (1 - exit_est)
    return (exit_est - entry_price) - fee_entry - fee_exit - MIN_NET_EDGE_SLIPPAGE


# ══════════════════════════════════════════════════════════════════════
# v5.9c — Flow-signal publisher for Engine 2 consumption.
# Three-signal microstructure snapshot per market. Publish-only — Engine 1
# does NOT alter behavior based on these values. Engine 2 reads the JSON
# file as a directional-confirmation layer on top of its catalyst theses.
# ══════════════════════════════════════════════════════════════════════


def _signed_mid_drift(s: "MarketShadow", window_sec: float) -> float:
    """Signed Δmid over window_sec. Positive = price rose (yes-side under
    buy pressure); negative = price fell (no-side pressure). Returns 0.0
    with <2 samples. Distinct from _price_velocity (which is unsigned)."""
    hist = _prune_mid_history(s, window_sec)
    if len(hist) < 2:
        return 0.0
    return hist[-1][1] - hist[0][1]


def _compute_flow_metrics(s: "MarketShadow") -> Dict[str, Any]:
    """Per-market microstructure snapshot. All scores bounded:
      imbalance_score ∈ [0, 1]   (magnitude of taker imbalance)
      velocity_score  ∈ [0, 1]   (|vel| normalized by v5.9 gate threshold)
      fill_bias       ∈ [-1, 1]  (signed mid-drift normalized by spread)
      flow_score      ∈ [0, 1]   (weighted combiner of the three magnitudes)
    Does not consult any runtime gate state — purely a derived snapshot
    of current book + trade-flow windows. Safe to call on warming or
    fully-idle markets (returns all-zero scores)."""
    spread = (s.best_ask - s.best_bid) if (s.best_bid > 0 and s.best_ask > 0
                                           and s.best_ask > s.best_bid) else 0.0

    # imbalance_score: magnitude of signed taker imbalance in [0, 1].
    raw_imb = _trade_imbalance(s)
    imbalance_score = min(1.0, abs(raw_imb))

    # velocity_score: normalize |Δmid| against the v5.9 fast_move threshold
    # so a score of 1.0 means "right at the gate". Caps at 1.0 for moves
    # that would already trip fast_move (not a downstream concern here —
    # we're just reporting magnitude).
    vel = _price_velocity(s)
    vel_threshold = max(MAX_VELOCITY_FLOOR, VELOCITY_FRAC_SPREAD * spread)
    vel_threshold = min(vel_threshold, MAX_VELOCITY)
    velocity_score = 0.0 if vel_threshold <= 0 else min(1.0, vel / vel_threshold)

    # fill_bias: signed mid-drift normalized by spread (fallback: tick).
    # Positive = yes-side price rising. Captures price-DIRECTION rather
    # than magnitude, which makes it orthogonal enough to imbalance_score
    # that combining them adds information.
    drift = _signed_mid_drift(s, FLOW_BIAS_WINDOW_SEC)
    denom = spread if spread > 0 else s.tick
    fill_bias = 0.0 if denom <= 0 else max(-1.0, min(1.0, drift / denom))

    # flow_score: weighted magnitude combiner. |fill_bias| so the sum
    # stays in [0, 1] regardless of direction.
    flow_score = (FLOW_W_IMBALANCE * imbalance_score
                  + FLOW_W_VELOCITY * velocity_score
                  + FLOW_W_BIAS * abs(fill_bias))
    flow_score = max(0.0, min(1.0, flow_score))

    # Raw regime classification — strength-AND-direction per spec.
    if flow_score > FLOW_SIGNALS_THRESHOLD and fill_bias > 0:
        raw_regime = "pressure_up"
    elif flow_score > FLOW_SIGNALS_THRESHOLD and fill_bias < 0:
        raw_regime = "pressure_down"
    else:
        raw_regime = "stable"

    # v5.9c.1 persistence: publish a non-stable regime only when the raw
    # classification has agreed for FLOW_REGIME_MIN_CONSEC consecutive
    # publishes. Single-publish flickers stay reported as "stable" even
    # though the raw inputs would have crossed the threshold.
    s.recent_regime_raw.append(raw_regime)
    if len(s.recent_regime_raw) > FLOW_REGIME_HISTORY_LEN:
        del s.recent_regime_raw[:len(s.recent_regime_raw) - FLOW_REGIME_HISTORY_LEN]

    if raw_regime == "stable":
        regime_state = "stable"
    else:
        need = FLOW_REGIME_MIN_CONSEC
        hist = s.recent_regime_raw
        if len(hist) >= need and all(r == raw_regime for r in hist[-need:]):
            regime_state = raw_regime
        else:
            regime_state = "stable"

    # pressure_direction: report directional lean with a neutral band
    # (v5.9c.1 widened to 0.08) so micro-flicker doesn't flip it on every tick.
    if fill_bias > FLOW_BIAS_NEUTRAL_EPSILON:
        pressure_direction = "yes"
    elif fill_bias < -FLOW_BIAS_NEUTRAL_EPSILON:
        pressure_direction = "no"
    else:
        pressure_direction = "neutral"

    # v5.9c.1: directional_conviction = signed strength × magnitude.
    # Ranges in [-1, 1]. Engine 2 can use this as a continuous confidence
    # signal orthogonal to the discrete regime_state (e.g. a high-conviction
    # read during the first publish of a regime transition, before
    # persistence has confirmed the regime_state flip).
    directional_conviction = round(fill_bias * flow_score, 4)

    return {
        "ticker": s.ticker,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "imbalance_score": round(imbalance_score, 4),
        "velocity_score": round(velocity_score, 4),
        "fill_bias": round(fill_bias, 4),
        "flow_score": round(flow_score, 4),
        "regime_state": regime_state,
        "pressure_direction": pressure_direction,
        "directional_conviction": directional_conviction,
    }


# v5.9c.1: module-level regime tally. Refreshed every publish; read by
# the minute summary to report how many markets are in each regime state.
_FLOW_REGIME_COUNTS: Dict[str, int] = {"pressure_up": 0, "pressure_down": 0,
                                        "stable": 0}


def _publish_flow_signals() -> None:
    """Write per-market flow snapshot to FLOW_SIGNALS_PATH atomically.
    tmp+rename guarantees Engine 2 never reads a partially-written file.
    No-op when FLOW_SIGNALS_ENABLED is False."""
    if not FLOW_SIGNALS_ENABLED:
        return
    now_ms = int(time.time() * 1000)
    payload = {
        "version": "5.9c.1",
        "generated_at_ms": now_ms,
        "threshold": FLOW_SIGNALS_THRESHOLD,
        "weights": {
            "imbalance": FLOW_W_IMBALANCE,
            "velocity": FLOW_W_VELOCITY,
            "fill_bias": FLOW_W_BIAS,
        },
        "markets": {},
    }
    # v5.9c.1: reset & tally counts per publish.
    counts = {"pressure_up": 0, "pressure_down": 0, "stable": 0}
    for s in STATES.values():
        try:
            m = _compute_flow_metrics(s)
            payload["markets"][s.ticker] = m
            counts[m.get("regime_state", "stable")] = counts.get(
                m.get("regime_state", "stable"), 0) + 1
        except Exception as e:
            # Never let one malformed market state break the whole publish.
            payload["markets"][s.ticker] = {
                "ticker": s.ticker,
                "error": f"{type(e).__name__}: {e}",
            }
    # Publish counts to module-level state for the minute-summary reader.
    _FLOW_REGIME_COUNTS.clear()
    _FLOW_REGIME_COUNTS.update(counts)
    tmp = FLOW_SIGNALS_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, FLOW_SIGNALS_PATH)
    except Exception as e:
        print(f"[flow_signals] publish failed: {type(e).__name__}: {e}",
              flush=True)


# ══════════════════════════════════════════════════════════════════════
# Globals (process-wide state)
# ══════════════════════════════════════════════════════════════════════

_STOP = False
_LOCK = threading.Lock()
_START = time.time()

_UNIVERSE_BLACKLIST: set = set()    # tickers banned for this session (structural break)
_CORE_UNIVERSE: set = set()         # tickers pinned to core universe
_ACTIVE_WS = None                   # current WebSocket reference (for discovery reconnects)


def handle_stop(signum, frame):
    global _STOP
    _STOP = True
    print("\nshutdown requested, closing WS...", flush=True)


signal.signal(signal.SIGINT, handle_stop)
signal.signal(signal.SIGTERM, handle_stop)


# ── Auth ───────────────────────────────────────────────────────────

def load_private_key():
    if not PRIVATE_KEY_PATH or not os.path.exists(PRIVATE_KEY_PATH):
        raise SystemExit(f"Private key not found at {PRIVATE_KEY_PATH}")
    with open(PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


def sign_request(private_key, method: str, path: str) -> Dict[str, str]:
    """Generate Kalshi auth headers for a request."""
    ts_ms = str(int(time.time() * 1000))
    msg = (ts_ms + method.upper() + path).encode("utf-8")
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
    }


# ── Market state ───────────────────────────────────────────────────

@dataclass
class MarketShadow:
    """Virtual per-market state. The bot doesn't submit orders; it tracks
    the top-of-book and computes what *would* fill against its virtual
    quotes, with realistic fees and sizing constraints."""

    # ── Identity ───────────────────────────────────────────────────
    ticker: str
    title: str
    tick: float
    vol_24h: float

    # ── Risk / sizing ──────────────────────────────────────────────
    # Multiplier on MAX_PER_MARKET_USD. 1.0 = normal; 0.0 = permanent disable
    # (set by structural-break blacklist or discovery drop with inventory).
    cap_multiplier: float = 1.0
    # Market resolution time; drives DTR / close-proximity exit logic.
    close_time_utc: Optional[datetime] = None

    # ── Book state (prices in dollars) ────────────────────────────
    yes_levels: Dict[float, float] = field(default_factory=dict)
    no_levels: Dict[float, float] = field(default_factory=dict)
    best_bid: float = 0.0
    best_ask: float = 0.0
    best_bid_size: int = 0
    best_ask_size: int = 0

    # ── Virtual quotes (dollars) ──────────────────────────────────
    our_buy_px: float = 0.0
    our_sell_px: float = 0.0
    buy_armed: bool = True
    sell_armed: bool = True

    # ── Virtual inventory and realized P&L ────────────────────────
    inventory: float = 0.0
    cost_basis: float = 0.0
    realized_pnl: float = 0.0
    fills_buy: int = 0
    fills_sell: int = 0
    fees_paid_maker: float = 0.0
    fees_paid_taker: float = 0.0

    # ── Activity counters ──────────────────────────────────────────
    book_updates: int = 0
    trades_seen: int = 0
    last_trade_ts: float = 0.0

    # ── Rewards-distance proxy (maker quality metric) ─────────────
    cumulative_distance_score: float = 0.0
    score_samples: int = 0

    # ── Fill history ───────────────────────────────────────────────
    # (ts, size, 'B' | 'S') — powers the turnover gate and net-growth detector.
    recent_fills: list = field(default_factory=list)

    # ── Liquidation state ──────────────────────────────────────────
    liquidating: bool = False
    liquidation_stage: int = 0            # 0=idle, 1=passive_maker, 2=cross, 3=hold
    liquidation_started_ts: float = 0.0
    liquidation_urgent: bool = True       # True → standard 1→2→3 cascade; False → passive maker forever
    stage_2_entries: int = 0              # structural-break counter
    blacklisted: bool = False
    # v5.3 stuck-stage-3 tracking
    stage3_first_entered_ts: float = 0.0   # first time we entered stage 3 for current hold; 0 = not in stage 3
    stage3_last_print_ts: float = 0.0      # throttle print spam on spread-flicker 1↔3 oscillation
    # v5.4 signal-export throttle: last emission per signal type per market
    last_signal_export_ts: Dict[str, float] = field(default_factory=dict)
    # v5.4 opportunity-cost deprioritization marker
    deprioritized: bool = False
    discovery_survivals: int = 0          # cycles survived before core-universe promotion
    # Post-regime cooldown: unix ts until which new BUY fills are blocked
    # on this market following a gate-driven exit.
    regime_cooldown_until_ts: float = 0.0
    # Cumulative taker volume by side — powers discovery-time two-sided
    # flow filter. Since subscription (not windowed).
    cumulative_yes_taker_vol: float = 0.0
    cumulative_no_taker_vol: float = 0.0

    # ── Real-time gate state ──────────────────────────────────────
    # (ts, mid) — feeds rolling volatility and velocity signals.
    mid_history: list = field(default_factory=list)
    # (ts, size, taker_side) — feeds trade-imbalance signal. Populated from
    # ALL observed trades on the market, not just our fills.
    trade_flow: list = field(default_factory=list)
    # Per-gate rejection counter (instrumentation for threshold tuning).
    gate_rejects: Dict[str, int] = field(default_factory=dict)
    # Last gate result; consumed by _should_hold to distinguish transient
    # gate trips from regime changes.
    last_gate_reason: str = ""

    # ── v5.8 inventory discipline ──────────────────────────────────
    # Timestamp when inventory transitioned 0 → positive for the current
    # hold cycle. Reset to 0 when inventory returns to 0. Drives the
    # inventory-age gate and the telemetry oldest_age metric.
    first_inventory_ts: float = 0.0
    # Set True when inventory age exceeds MAX_INV_AGE_MIN. Reset on inv→0.
    inventory_stale: bool = False
    # Set True once profit-lock mode has been entered for this hold cycle.
    # Prevents re-triggering liquidation on every quote tick.
    profit_locked: bool = False
    # Count of URGENT liquidation entries on this market. Tracked for
    # the INV_TRAP_STRIKES_TO_DEMOTE cap-multiplier demotion.
    inv_trap_strikes: int = 0

    # ── v5.9c.1 flow-signal persistence ────────────────────────────────
    # Last N raw regime classifications (where N = FLOW_REGIME_HISTORY_LEN).
    # Mutated only by _compute_flow_metrics — never read by any gate or
    # trading path. Used to suppress single-publish pressure_up/down
    # flickers by requiring FLOW_REGIME_MIN_CONSEC consecutive same-class
    # raw results before the published regime_state diverges from stable.
    recent_regime_raw: list = field(default_factory=list)

    # ── v5.9d delta-path book diagnostics ──────────────────────────────
    # Engine 1 post-v5.9c observation: no_book rejects climb to 1,400+
    # while book_side_zeroed_by_snapshot stays at 0 — meaning the
    # pathology is NOT one-sided snapshot overwrites. The remaining
    # hypotheses are: (a) deltas are being dropped, (b) delta merge
    # logic is corrupting state (crossed books), (c) one side stops
    # updating (asymmetric staleness). These counters answer which one.
    # All fields are write-once-from-one-path diagnostics — never read
    # by any gate, quoting, or trading decision.
    yes_delta_count: int = 0
    no_delta_count: int = 0
    last_yes_delta_ts: float = 0.0   # unix ts of last YES-side delta
    last_no_delta_ts: float = 0.0    # unix ts of last NO-side delta
    # Cumulative count of each invalid-book condition seen during a
    # recompute. Mutually exclusive per recompute (crossed logged only
    # if neither side is empty; empties logged only when the specific
    # side went to zero).
    book_bid_empty_count: int = 0
    book_ask_empty_count: int = 0
    book_crossed_count: int = 0
    # Number of valid→invalid transitions observed (ANY reason).
    book_invalid_transitions: int = 0
    # First-transition forensic metadata — populated ONCE per market on
    # the first valid→invalid transition after boot. Captured so log
    # spam stays bounded but the exact failure mode is preserved.
    first_invalid_ts: float = 0.0
    first_invalid_reason: str = ""    # see _classify_invalid() for taxonomy

    # v5.9e: distinguish "born invalid" (universe admission issue) from
    # "became invalid" (true book-integrity bug). Post-t+10m soak showed
    # invalid_now=17 pinned across 42 subscribed markets, all with
    # first=n/a — no valid→invalid transition ever occurred. These
    # fields make that story legible without inference.
    ever_valid: bool = False          # flipped True at first valid-book observation
    first_book_ts: float = 0.0        # ts of first book observation (snapshot or delta)
    first_valid_ts: float = 0.0       # ts book first became valid (0 if never)

    # v5.9f post-subscription admission control.
    # subscribed_ts: when this market entered STATES (defaulted in
    # __post_init__ so every MarketShadow gets a correct value without
    # callers remembering to set it).
    # admission_rejected: flipped True by _check_market_admission when
    # the grace window elapses without ever_valid=True. Gated markets
    # early-return from _meets_market_conditions so they stop producing
    # no_book rejections and stop consuming quote-loop cycles.
    subscribed_ts: float = 0.0
    admission_rejected: bool = False
    admission_rejected_ts: float = 0.0  # captured at flip for forensic
    admission_rejected_reason: str = ""  # "never_valid" | "stuck_invalid"
    # v5.9g: set when a market transitions valid→invalid; cleared on
    # invalid→valid. Drives the stuck-invalid admission revocation path.
    invalid_since_ts: float = 0.0

    # v5.9e new-taxonomy counters. The existing three (bid_empty_count,
    # ask_empty_count, crossed_count) are preserved byte-for-byte for
    # backward compatibility; these add coverage for empty-both and locked
    # books that previously merged into bid_empty / crossed respectively.
    book_empty_both_count: int = 0    # both sides empty on transition
    book_locked_count: int = 0        # bid == ask (zero-width book)

    # ── v5.9 book-reconstruction diagnostics ──────────────────────────
    # Hypothesis under investigation: the `no_book` gate spike observed
    # at ~t+5m across multiple runs is driven by Kalshi sending partial
    # (one-sided) orderbook snapshots. apply_orderbook_snapshot currently
    # wipes BOTH yes_levels and no_levels before refill, so a one-sided
    # snapshot zeros the side that wasn't delivered. These counters
    # confirm or refute that hypothesis without changing any behavior.
    snapshot_count: int = 0
    snapshot_one_sided_yes_only: int = 0
    snapshot_one_sided_no_only: int = 0
    # Incremented in recompute_top_of_book when either side transitions
    # from non-zero to zero within a single recompute (the exact failure
    # mode that trips the no_book gate on the next quote tick).
    book_side_zeroed_by_snapshot: int = 0

    def __post_init__(self):
        # v5.9f: default subscribed_ts to creation time. Tests can
        # override (e.g. "simulate subscribed 60s ago") by setting
        # s.subscribed_ts = time.time() - 60 after construction.
        if self.subscribed_ts == 0.0:
            self.subscribed_ts = time.time()


STATES: Dict[str, MarketShadow] = {}


# ── Logging ────────────────────────────────────────────────────────

def ensure_logs():
    for path, header in [
        (TRADES_LOG, ["timestamp", "ticker", "side", "fill_px", "size",
                      "inventory_after", "realized_pnl", "title"]),
        (QUOTES_LOG, ["timestamp", "ticker", "best_bid", "best_ask",
                      "our_buy", "our_sell", "inventory", "realized_pnl"]),
        (REWARDS_LOG, ["timestamp", "ticker", "distance_from_mid_bid",
                       "distance_from_mid_ask", "normalized_score"]),
    ]:
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(header)


# ── Book update handlers ───────────────────────────────────────────

def recompute_top_of_book(s: MarketShadow):
    """Rebuild best_bid/best_ask from level dicts. Prices are stored in dollars directly."""
    # v5.9 diagnostics: capture prior top so we can detect non-zero→zero
    # transitions (the exact precondition for the `no_book` gate trip).
    prior_bid = s.best_bid
    prior_ask = s.best_ask
    yes_active = {p: sz for p, sz in s.yes_levels.items() if sz > 0}
    no_active = {p: sz for p, sz in s.no_levels.items() if sz > 0}
    if yes_active:
        best_yes_dollars = max(yes_active.keys())
        s.best_bid = best_yes_dollars
        s.best_bid_size = int(yes_active[best_yes_dollars])
    else:
        s.best_bid = 0.0
        s.best_bid_size = 0
    if no_active:
        best_no_dollars = max(no_active.keys())
        # YES ask = 1.0 - NO bid (complement)
        s.best_ask = round(1.0 - best_no_dollars, 6)
        s.best_ask_size = int(no_active[best_no_dollars])
    else:
        s.best_ask = 0.0
        s.best_ask_size = 0
    # v5.9 diagnostics: count either-side transition from non-zero to zero.
    # One counter event per recompute even if both sides zero simultaneously.
    if (prior_bid > 0 and s.best_bid == 0) or (prior_ask > 0 and s.best_ask == 0):
        s.book_side_zeroed_by_snapshot += 1

    # v5.9e: 5-value invalid-reason taxonomy + ever_valid tracking.
    # Previous 3-value (bid_empty/ask_empty/crossed) collapsed two
    # forensically useful cases: both-sides-empty (universe admission
    # bug fingerprint) and locked books (merge-boundary fingerprint).
    # Split them so discovery vs. reconstruction issues separate cleanly.
    prior_valid = (prior_bid > 0 and prior_ask > 0 and prior_ask > prior_bid)
    now_valid = (s.best_bid > 0 and s.best_ask > 0 and s.best_ask > s.best_bid)
    now_ts = time.time()
    # Record first book observation (any levels on any side) so
    # time_to_first_valid is measurable even if the market never
    # reaches a valid state.
    if s.first_book_ts == 0.0 and (s.yes_levels or s.no_levels):
        s.first_book_ts = now_ts
    # Flip ever_valid once, on first achievement of a valid book.
    if now_valid and not s.ever_valid:
        s.ever_valid = True
        s.first_valid_ts = now_ts
    # v5.9g: maintain invalid_since_ts for the stuck-invalid admission path.
    # Sets on valid→invalid, clears on invalid→valid, no-op otherwise.
    if prior_valid and not now_valid:
        s.invalid_since_ts = now_ts
    elif now_valid and s.invalid_since_ts > 0:
        s.invalid_since_ts = 0.0
    if prior_valid and not now_valid:
        reason = _classify_invalid(s)
        # Map reason → appropriate counter. Preserves existing counter
        # names for backward compat (prior tests reference bid_empty_count
        # etc.) while adding the two new ones for the split categories.
        if reason == "empty_both":
            s.book_empty_both_count += 1
        elif reason == "bid_missing":
            s.book_bid_empty_count += 1
        elif reason == "ask_missing":
            s.book_ask_empty_count += 1
        elif reason == "locked":
            s.book_locked_count += 1
        elif reason == "crossed":
            s.book_crossed_count += 1
        s.book_invalid_transitions += 1
        if s.first_invalid_ts == 0.0:
            s.first_invalid_ts = now_ts
            s.first_invalid_reason = reason
            _log_first_invalid(s, reason, prior_bid, prior_ask)

    # Record mid for rolling volatility / velocity signals.
    if s.best_bid > 0 and s.best_ask > 0 and s.best_ask > s.best_bid:
        s.mid_history.append((now_ts, (s.best_bid + s.best_ask) / 2.0))


def _classify_invalid(s: MarketShadow) -> str:
    """v5.9e: 5-value taxonomy for invalid-book states. Returns empty
    string when the book is valid. Callers use this both for transition
    classification and for describing the current state of a market
    observed to be invalid.

      empty_both   — neither side has live levels
      bid_missing  — YES side empty, NO side populated
      ask_missing  — NO side empty, YES side populated
      crossed      — both sides populated but best_ask < best_bid
      locked       — both sides populated, best_ask == best_bid
    """
    bid_zero = s.best_bid <= 0
    ask_zero = s.best_ask <= 0
    if bid_zero and ask_zero:
        return "empty_both"
    if bid_zero:
        return "bid_missing"
    if ask_zero:
        return "ask_missing"
    if s.best_ask < s.best_bid:
        return "crossed"
    if s.best_ask == s.best_bid:
        return "locked"
    return ""


# v5.9e: forensic-dump throttle. Set of thresholds-already-fired (in
# seconds since _START). Checked on every minute summary; each threshold
# fires exactly once per process.
_FORENSIC_DUMPS_EMITTED: set = set()
_FORENSIC_DUMP_THRESHOLDS: Tuple[int, ...] = (60, 300)

# v5.9i: low_edge family analysis — one-shot at t+600s (plus shutdown).
_LOW_EDGE_ANALYSIS_EMITTED: bool = False


def _maybe_emit_forensic_dump() -> None:
    """v5.9e: at fixed elapsed times (t+60s, t+300s), print a detailed
    snapshot of the top-10 currently-invalid markets. One-shot per
    threshold — bounded log spam — so the data is captured at a known
    point in the run for side-by-side comparison."""
    elapsed = time.time() - _START
    for thr in _FORENSIC_DUMP_THRESHOLDS:
        if elapsed >= thr and thr not in _FORENSIC_DUMPS_EMITTED:
            _FORENSIC_DUMPS_EMITTED.add(thr)
            _emit_invalid_forensic_dump(thr)
    # v5.9i: one-shot low_edge family analysis at t+10min.
    global _LOW_EDGE_ANALYSIS_EMITTED
    if not _LOW_EDGE_ANALYSIS_EMITTED and elapsed >= 600:
        _LOW_EDGE_ANALYSIS_EMITTED = True
        _emit_low_edge_family_analysis()


def _emit_invalid_forensic_dump(threshold_sec: int) -> None:
    """Per-market forensic snapshot. Ranks invalid markets with
    never-valid markets first (those are the universe-admission issues),
    then by book-activity (level count + snapshot count) to surface
    markets that have the most data but are still broken — those are
    the reconstruction-pathology candidates."""
    invalid: List[MarketShadow] = []
    for s in STATES.values():
        if _classify_invalid(s):
            invalid.append(s)
    # Sort: never-valid first (discovery problems), then by total
    # engagement (snapshot_count + deltas) descending so the most
    # "active but broken" markets surface next.
    invalid.sort(key=lambda s: (
        s.ever_valid,                                    # False (0) < True (1) → never-valid first
        -(s.snapshot_count + s.yes_delta_count + s.no_delta_count),
    ))
    print(f"[forensic @ t+{threshold_sec}s] {len(invalid)} invalid markets, "
          f"top {min(10, len(invalid))}:", flush=True)
    now_ts = time.time()
    for s in invalid[:10]:
        now_reason = _classify_invalid(s)
        stale_y = (now_ts - s.last_yes_delta_ts) if s.last_yes_delta_ts > 0 else -1.0
        stale_n = (now_ts - s.last_no_delta_ts) if s.last_no_delta_ts > 0 else -1.0
        ttv = ((s.first_valid_ts - s.first_book_ts)
               if s.first_valid_ts > 0 and s.first_book_ts > 0 else -1.0)
        first_reason = s.first_invalid_reason or "n/a"
        print(f"  {s.ticker[:34]:<36s}  ever_valid={str(s.ever_valid):<5s}  "
              f"first={first_reason:<12s}  now={now_reason:<12s}  "
              f"lvls(y/n)={len(s.yes_levels)}/{len(s.no_levels)}  "
              f"stale(y/n)={stale_y:.0f}s/{stale_n:.0f}s  "
              f"snap={s.snapshot_count} deltas(y/n)={s.yes_delta_count}/{s.no_delta_count}  "
              f"ttv={ttv:.1f}s", flush=True)


def _check_market_admission(s: MarketShadow) -> None:
    """v5.9f/g: admission revocation — two paths.
      (f) never_valid: market subscribed > MARKET_ADMISSION_GRACE_SEC
          ago and ever_valid is still False.
      (g) stuck_invalid: market was ever_valid=True but has been in an
          invalid state continuously for > MARKET_STUCK_INVALID_SEC.
    Either path flips admission_rejected and emits a one-shot line with
    a distinguishable reason string."""
    if not MARKET_ADMISSION_ENABLED:
        return
    if s.admission_rejected:
        return
    if s.subscribed_ts <= 0:
        return
    now_ts = time.time()
    # Path (f): never-valid, past grace.
    if (not s.ever_valid
            and MARKET_ADMISSION_GRACE_SEC > 0
            and (now_ts - s.subscribed_ts) >= MARKET_ADMISSION_GRACE_SEC):
        _finalize_admission_reject(s, now_ts, "never_valid",
                                    now_ts - s.subscribed_ts)
        return
    # Path (g): ever_valid once, now stuck invalid past the stuck window.
    if (s.ever_valid
            and s.invalid_since_ts > 0
            and MARKET_STUCK_INVALID_SEC > 0
            and (now_ts - s.invalid_since_ts) >= MARKET_STUCK_INVALID_SEC):
        _finalize_admission_reject(s, now_ts, "stuck_invalid",
                                    now_ts - s.invalid_since_ts)
        return


def _finalize_admission_reject(s: MarketShadow, now_ts: float,
                                reason: str, elapsed: float) -> None:
    """Common flip path for both admission-reject reasons. Prints a
    single forensic line with the distinguishing reason."""
    s.admission_rejected = True
    s.admission_rejected_ts = now_ts
    s.admission_rejected_reason = reason
    now_state = _classify_invalid(s) or "n/a"
    first = s.first_invalid_reason or "n/a"
    print(f"⚠ ADMISSION-REJECT {s.ticker[:34]}  reason={reason}  "
          f"after={elapsed:.1f}s  first={first}  now={now_state}  "
          f"lvls(y/n)={len(s.yes_levels)}/{len(s.no_levels)}  "
          f"snap={s.snapshot_count}  deltas(y/n)={s.yes_delta_count}/{s.no_delta_count}",
          flush=True)


def _sweep_market_admissions() -> None:
    """v5.9f: run _check_market_admission across the current STATES.
    Cheap linear scan, called ~once per second from the main loop."""
    if not MARKET_ADMISSION_ENABLED:
        return
    for s in STATES.values():
        _check_market_admission(s)


def _log_first_invalid(s: MarketShadow, reason: str,
                       prior_bid: float, prior_ask: float) -> None:
    """v5.9d: one-shot forensic print on first valid→invalid transition.
    Captures: the exact cause, pre- and post-state, level-count snapshot,
    and per-side delta staleness. This is the line that decides between
    the three competing hypotheses (dropped deltas / merge corruption /
    one-sided freeze)."""
    now_ts = time.time()
    stale_yes = (now_ts - s.last_yes_delta_ts) if s.last_yes_delta_ts > 0 else -1.0
    stale_no = (now_ts - s.last_no_delta_ts) if s.last_no_delta_ts > 0 else -1.0
    print(f"⚠ BOOK-INVALID {s.ticker[:32]}  reason={reason}  "
          f"prior_bid={prior_bid:.4f} prior_ask={prior_ask:.4f}  "
          f"now_bid={s.best_bid:.4f} now_ask={s.best_ask:.4f}  "
          f"lvls(y/n)={len(s.yes_levels)}/{len(s.no_levels)}  "
          f"deltas(y/n)={s.yes_delta_count}/{s.no_delta_count}  "
          f"stale(y/n)={stale_yes:.1f}s/{stale_no:.1f}s", flush=True)


def _parse_level(lvl):
    """Parse a book level. Kalshi WS format: ["0.0010", "1000.00"] (dollars, size as strings).
    Returns (price_dollars_float, size_float) or None."""
    if isinstance(lvl, list) and len(lvl) >= 2:
        try:
            return float(lvl[0]), float(lvl[1])
        except (ValueError, TypeError):
            return None
    if isinstance(lvl, dict):
        p = lvl.get("price_dollars") or lvl.get("price") or lvl.get("p")
        sz = lvl.get("size") or lvl.get("quantity") or lvl.get("s")
        if p is None or sz is None:
            return None
        try:
            return float(p), float(sz)
        except (ValueError, TypeError):
            return None
    return None


# One-shot debug: dump first message of each type so we can see raw format
_DEBUG_SEEN_TYPES: set = set()


def _debug_dump(msg_type: str, payload: dict):
    if msg_type in _DEBUG_SEEN_TYPES:
        return
    _DEBUG_SEEN_TYPES.add(msg_type)
    try:
        print(f"  [debug first {msg_type}] {json.dumps(payload)[:500]}", flush=True)
    except Exception:
        pass


def _price_key(price_dollars: float, tick: float) -> float:
    """Snap to the market's tick grid so delta lookups match snapshot keys."""
    return round(round(price_dollars / tick) * tick, 6)


def apply_orderbook_snapshot(s: MarketShadow, snapshot: dict):
    """Reset the local book from a full snapshot message."""
    # v5.9 diagnostics: count snapshots + one-sided snapshot arrivals.
    # A one-sided snapshot is the suspected upstream cause of `no_book`
    # spikes: the handler wipes both sides before refilling only the
    # side that arrived, so the other side goes to zero.
    s.snapshot_count += 1
    yes_raw = snapshot.get("yes_dollars_fp") or snapshot.get("yes") or []
    no_raw = snapshot.get("no_dollars_fp") or snapshot.get("no") or []
    had_yes = bool(yes_raw)
    had_no = bool(no_raw)
    if had_yes and not had_no:
        s.snapshot_one_sided_yes_only += 1
    elif had_no and not had_yes:
        s.snapshot_one_sided_no_only += 1
    s.yes_levels = {}
    s.no_levels = {}
    for lvl in yes_raw:
        parsed = _parse_level(lvl)
        if parsed:
            p_dollars, sz = parsed
            if sz > 0:
                s.yes_levels[_price_key(p_dollars, s.tick)] = sz
    for lvl in no_raw:
        parsed = _parse_level(lvl)
        if parsed:
            p_dollars, sz = parsed
            if sz > 0:
                s.no_levels[_price_key(p_dollars, s.tick)] = sz
    recompute_top_of_book(s)


def apply_orderbook_delta(s: MarketShadow, delta: dict):
    """Incremental update. Kalshi: {price_dollars: "0.6900", delta_fp: "67.57", side: "yes"}."""
    price = delta.get("price_dollars") or delta.get("price") or delta.get("p")
    delta_size = None
    for k in ("delta_fp", "delta", "d", "quantity_delta", "size_delta"):
        if k in delta:
            delta_size = delta[k]
            break
    side = (delta.get("side") or delta.get("s") or "").lower()
    if price is None or delta_size is None:
        return
    try:
        p_dollars = float(price)
        d = float(delta_size)
    except (ValueError, TypeError):
        return
    key = _price_key(p_dollars, s.tick)
    target = s.yes_levels if side == "yes" else s.no_levels if side == "no" else None
    if target is None:
        return
    # v5.9d: count per-side deltas + record last-update ts BEFORE mutating
    # the book. These drive the staleness forensic line in the minute
    # summary and the invalid-transition reason detection downstream.
    now_ts = time.time()
    if side == "yes":
        s.yes_delta_count += 1
        s.last_yes_delta_ts = now_ts
    else:
        s.no_delta_count += 1
        s.last_no_delta_ts = now_ts
    current = target.get(key, 0.0)
    new_size = current + d
    if new_size <= 0:
        target.pop(key, None)
    else:
        target[key] = new_size
    recompute_top_of_book(s)


# ══════════════════════════════════════════════════════════════════════
# Quoting (Avellaneda-Stoikov inventory skew + depth-driven sizing)
# ══════════════════════════════════════════════════════════════════════
#
# Simplified A-S:
#   reservation_price = mid - γ × (q / q_max) × spread
#   skew shifts both bid + ask toward lower prices when long (encourages sells)
#   spread widens as inventory grows (adverse-selection protection)
#
# Full A-S requires calibrating σ (volatility) and κ (arrival rate) per market;
# we use a fixed γ and let real-time gates handle extreme vol. Inventory
# normalization uses the dynamic depth-scaled cap (see _effective_per_market_cap).


def _global_inventory_usd() -> float:
    """Sum inventory × mid across all markets (approximation of total open exposure)."""
    total = 0.0
    for st in STATES.values():
        if st.inventory <= 0:
            continue
        mid = (st.best_bid + st.best_ask) / 2.0 if st.best_bid > 0 and st.best_ask > 0 else st.cost_basis
        total += st.inventory * max(mid, 0.001)
    return total


def _count_open_markets() -> int:
    """Number of markets with non-zero inventory. Drives MAX_OPEN_MARKETS cap."""
    return sum(1 for st in STATES.values() if st.inventory > 0)


def _is_two_sided_flow(s: MarketShadow) -> bool:
    """Discovery-time filter: True if observed taker flow is reasonably balanced.
    Returns True (pass) for markets with insufficient observation so fresh
    markets aren't excluded before they have a chance. Next discovery cycle
    filters them once volume accumulates."""
    total = s.cumulative_yes_taker_vol + s.cumulative_no_taker_vol
    if total < MIN_OBSERVED_VOLUME_FOR_FILTER:
        return True
    imbalance = abs(s.cumulative_yes_taker_vol - s.cumulative_no_taker_vol) / total
    return imbalance <= MAX_OBSERVED_IMBALANCE


def _effective_per_market_cap(s: MarketShadow) -> float:
    """Depth-driven per-market dollar cap. The ceiling (MAX_PER_MARKET_USD)
    is scaled by the thinner side of the top-of-book:
      - REFERENCE_DEPTH_CONTRACTS on both sides  → full ceiling
      - half that depth                          → half cap
      - no two-sided depth                       → 0 (blocks entry)
    cap_multiplier is honored for disable semantics (0 = permanent disable).
    """
    if s.cap_multiplier <= 0.0:
        return 0.0
    if s.best_bid_size <= 0 or s.best_ask_size <= 0:
        return 0.0
    depth_both = min(s.best_bid_size, s.best_ask_size)
    depth_factor = min(1.0, depth_both / REFERENCE_DEPTH_CONTRACTS)
    return MAX_PER_MARKET_USD * s.cap_multiplier * depth_factor


# v5.1: liquidation urgency routing. URGENT reasons = real market signal
# demanding exit (regime change, blacklist, discovery drop, runaway inventory).
# Non-urgent reasons = slow flow that just needs patience (stale inventory,
# turnover). Non-urgent liquidations stay in passive maker forever — we never
# cross the spread to realize a near-zero price move. The t+60-75m cascade
# that cost $2.32 was caused by non-urgent reasons being routed through the
# urgent cascade. This separation eliminates that failure mode.
#
# v5.6 (2026-04-21): demote gate:* from URGENT to NON-URGENT. Days of session
# logs show every regime-gate-triggered exit converts a winning trade into a
# loser — because the gate fires AFTER price has already moved against us,
# and urgent cross-exit compounds the slippage we're trying to avoid.
# Correct behavior: regime change pauses NEW inventory (already works) but
# existing inventory should hold passively for mean reversion. The NON-URGENT
# path has DTR + MAX_SLOW_LIQ_HOURS escalation ceilings so we're protected
# from infinite-hold on truly dead markets.
# Env flag GATE_EXITS_ARE_URGENT=1 restores v5.5 behavior for A/B comparison.
_URGENT_LIQ_REASONS = {"disabled", "discovery_drop"}
GATE_EXITS_ARE_URGENT = os.environ.get("GATE_EXITS_ARE_URGENT", "0") == "1"


def _is_urgent_liquidation(reason: str) -> bool:
    """Return True if reason demands aggressive cross-exit behavior."""
    if reason in _URGENT_LIQ_REASONS:
        return True
    if GATE_EXITS_ARE_URGENT and reason.startswith("gate:"):
        return True                     # v5.6: opt-in via env, default OFF
    if reason.startswith("net_growth"): # inventory compounding faster than exits
        return True
    return False


def _start_liquidation(s: MarketShadow, reason: str):
    """Enter liquidation mode at stage 1 (passive maker).
    Urgency flag determines whether we'll cascade to stage 2 (cross) after
    stage 1 times out, or stay in passive maker indefinitely."""
    if not s.liquidating:
        s.liquidating = True
        s.liquidation_stage = 1
        s.liquidation_started_ts = time.time()
        s.liquidation_urgent = _is_urgent_liquidation(reason)
        # v5.8: count URGENT entries as inventory-trap strikes. Repeated
        # forced exits on the same market = structural loser. Bump strikes
        # first but defer the demote print until AFTER LIQ-STAGE-1 so the
        # log reads in causal order (cause → effect).
        demoted_now = False
        if s.liquidation_urgent:
            s.inv_trap_strikes += 1
            if (s.inv_trap_strikes >= INV_TRAP_STRIKES_TO_DEMOTE
                    and s.cap_multiplier > INV_TRAP_DEMOTE_MULT):
                s.cap_multiplier = INV_TRAP_DEMOTE_MULT
                demoted_now = True
        tag = "URGENT" if s.liquidation_urgent else "SLOW"
        print(f"  🟡 LIQ-STAGE-1 {s.ticker[:32]}  inv={s.inventory:.0f} [{reason}] [{tag}]", flush=True)
        if demoted_now:
            print(f"  🟠 INV-TRAP-DEMOTE {s.ticker[:32]}  "
                  f"strikes={s.inv_trap_strikes}  cap→{INV_TRAP_DEMOTE_MULT:.2f}",
                  flush=True)
        # Arm regime cooldown for gate-driven exits
        if reason.startswith("gate:"):
            s.regime_cooldown_until_ts = time.time() + REGIME_COOLDOWN_MIN * 60


def _synthetic_cross_exit(s: MarketShadow):
    """Simulate Kalshi's matching engine behavior when we cross down.
    When we post SELL at best_bid, Kalshi fills us immediately against the
    existing bid queue — taker fee, guaranteed execution. Size limited by
    best_bid_size (we can only consume what's there).
    """
    if s.inventory <= 0 or s.best_bid <= 0:
        return
    # Size: min of inventory, bid queue depth, sane per-fill cap
    bid_depth = max(s.best_bid_size, 1)
    fill_size = min(s.inventory, float(bid_depth), float(QUOTE_SIZE_CONTRACTS))
    if fill_size < 1.0:
        return
    fill_px = s.best_bid
    fee = compute_fee(fill_size, fill_px, is_taker=True)
    pnl = (fill_px - s.cost_basis) * fill_size - fee
    s.realized_pnl += pnl
    s.fees_paid_taker += fee
    s.inventory -= fill_size
    s.fills_sell += 1
    s.recent_fills.append((time.time(), fill_size, 'S'))
    # v5.8: reset inventory-age tracking when cycle closes (inv→0).
    if s.inventory <= 0:
        s.first_inventory_ts = 0.0
        s.inventory_stale = False
        s.profit_locked = False
    with open(TRADES_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            s.ticker, "SELL_LIQ", f"{fill_px:.4f}", f"{fill_size}",
            f"{s.inventory:.0f}", f"{s.realized_pnl:.2f}", s.title,
        ])
    print(f"  ⚡ CROSS-EXIT  {s.ticker[:32]:<34s} {fill_size:>6.1f}@{fill_px:.4f}  pnl=${pnl:+.2f}  inv={s.inventory:.0f}  fee=${fee:.3f}", flush=True)

    # v5.2 P&L-based blacklist (also applies on taker cross-exits)
    if (s.fills_sell >= PNL_BLACKLIST_MIN_SELLS
            and s.realized_pnl < PNL_BLACKLIST_LOSS_USD
            and s.cap_multiplier > 0.0):
        s.cap_multiplier = 0.0
        s.blacklisted = True
        print(f"  🛑 PNL-BLACKLIST {s.ticker[:32]}  "
              f"sells={s.fills_sell} realized=${s.realized_pnl:+.2f} "
              f"< ${PNL_BLACKLIST_LOSS_USD:.2f} — disabled", flush=True)


def _liquidation_quote(s: MarketShadow, tick: float) -> float:
    """Return the sell price for the current liquidation state.

    URGENT liquidations (gate:*, disabled, discovery_drop, net_growth*):
      Stage 1: passive maker at best_bid + 1 tick (10 min)
      Stage 2: synthetic cross-exit at best_bid (taker fee) (10 min)
      Stage 3: hold to resolution (no quote)

    NON-URGENT liquidations (stale_no_sells, turnover):
      Stage 1 indefinitely — passive maker at best_bid + 1 tick.
      Never cross the spread. Two escalation conditions force a flip to
      URGENT cascade:
        - resolution within CLOSE_TIME_EXIT_HOURS (DTR-based)
        - slow-liq duration exceeds MAX_SLOW_LIQ_HOURS (absolute ceiling)
    """
    elapsed_min = (time.time() - s.liquidation_started_ts) / 60.0 if s.liquidation_started_ts else 0

    # ── NON-URGENT: passive maker forever, subject to two escalation ceilings ──
    if not s.liquidation_urgent:
        # (a) DTR-based escalation: resolution imminent → cross before the gap
        if s.close_time_utc is not None:
            hours_to_close = (s.close_time_utc - datetime.now(timezone.utc)).total_seconds() / 3600.0
            if hours_to_close <= CLOSE_TIME_EXIT_HOURS:
                s.liquidation_urgent = True
                s.liquidation_started_ts = time.time()   # reset cascade clock
                print(f"  🔴 LIQ-ESCALATE {s.ticker[:32]}  slow→urgent  "
                      f"(close in {hours_to_close:.1f}h)", flush=True)
                # Fall through to URGENT branch below

        # (b) Absolute ceiling: held too long regardless of DTR
        if not s.liquidation_urgent and elapsed_min > MAX_SLOW_LIQ_HOURS * 60:
            s.liquidation_urgent = True
            s.liquidation_started_ts = time.time()   # reset cascade clock
            print(f"  🔴 LIQ-ESCALATE {s.ticker[:32]}  slow→urgent  "
                  f"(exceeded {MAX_SLOW_LIQ_HOURS:.0f}h slow-liq ceiling)", flush=True)
            # Fall through to URGENT branch below

        # Neither ceiling hit — stay passive
        if not s.liquidation_urgent:
            candidate = round(s.best_bid + tick, 4)
            if candidate < s.best_ask:
                if s.liquidation_stage != 1:
                    s.liquidation_stage = 1
                # Exiting stage 3 (back to tight-spread-OK) — reset tracker
                s.stage3_first_entered_ts = 0.0
                return candidate
            # Spread too tight for inside-maker quote → hold rather than cross
            now = time.time()
            # v5.3: track first-entered timestamp across spread flicker.
            # Only reset on clean exit (stage 1 transition above) or blacklist,
            # not on every re-entry.
            if s.stage3_first_entered_ts == 0.0:
                s.stage3_first_entered_ts = now
            stuck_min = (now - s.stage3_first_entered_ts) / 60.0
            # v5.3: stuck-stage-3 blacklist — market has been untradeable for
            # too long, permanently disable to free the slot.
            if (stuck_min >= STUCK_STAGE3_BLACKLIST_MIN
                    and s.cap_multiplier > 0.0):
                s.cap_multiplier = 0.0
                s.blacklisted = True
                print(f"  🛑 STUCK-BLACKLIST {s.ticker[:32]}  inv={s.inventory:.0f} "
                      f"stuck in stage-3 for {stuck_min:.0f}min — disabled",
                      flush=True)
            elif s.liquidation_stage != 3:
                s.liquidation_stage = 3
                # v5.3: throttle print on spread-flicker 1↔3 oscillation
                if now - s.stage3_last_print_ts > LIQ_STAGE3_PRINT_THROTTLE_MIN * 60:
                    s.stage3_last_print_ts = now
                    print(f"  🟡 LIQ-STAGE-3 {s.ticker[:32]}  inv={s.inventory:.0f} "
                          f"(slow liq, spread tight — holding to resolution)", flush=True)
            return 0.0
        # (escalated — re-enter function via recompute next tick with urgent=True)
        elapsed_min = 0   # reset locally for this call so stage-1 runs first

    # ── URGENT: original 3-stage cascade ───────────────────────────
    if elapsed_min < LIQ_STAGE_1_DURATION_MIN:
        candidate = round(s.best_bid + tick, 4)
        if candidate < s.best_ask:
            if s.liquidation_stage != 1:
                s.liquidation_stage = 1
            return candidate
        # Spread too tight — fall through to stage 2
    if elapsed_min < LIQ_STAGE_1_DURATION_MIN + LIQ_STAGE_2_DURATION_MIN:
        if s.liquidation_stage != 2:
            s.liquidation_stage = 2
            s.stage_2_entries += 1
            print(f"  🟡 LIQ-STAGE-2 {s.ticker[:32]}  inv={s.inventory:.0f} "
                  f"(cross #{s.stage_2_entries})", flush=True)
            if s.stage_2_entries >= STRUCTURAL_BREAK_THRESHOLD and not s.blacklisted:
                s.blacklisted = True
                s.cap_multiplier = 0.0
                _UNIVERSE_BLACKLIST.add(s.ticker)
                print(f"  🔴 STRUCTURAL BREAK  {s.ticker}  —  "
                      f"{s.stage_2_entries} stage-2 entries. Blacklisted.", flush=True)
        _synthetic_cross_exit(s)
        return 0.0
    # Stage 3: hold
    if s.liquidation_stage != 3:
        s.liquidation_stage = 3
        print(f"  🟡 LIQ-STAGE-3 {s.ticker[:32]}  inv={s.inventory:.0f} "
              f"(holding to resolution)", flush=True)
    return 0.0


def _net_growth_60min(s: MarketShadow) -> float:
    """Compute net (buys - sells) in contracts over NET_GROWTH_WINDOW_MIN."""
    now = time.time()
    cutoff = now - NET_GROWTH_WINDOW_MIN * 60
    s.recent_fills = [(t, sz, side) for (t, sz, side) in s.recent_fills if t >= cutoff]
    buys = sum(sz for (_, sz, side) in s.recent_fills if side == 'B')
    sells = sum(sz for (_, sz, side) in s.recent_fills if side == 'S')
    return buys - sells


# ══════════════════════════════════════════════════════════════════════
# Real-time gate signals
# ══════════════════════════════════════════════════════════════════════

def _prune_mid_history(s: MarketShadow, window_sec: float) -> list:
    """Prune mid_history to the last window_sec seconds; return the slice."""
    cutoff = time.time() - window_sec
    s.mid_history = [(t, m) for (t, m) in s.mid_history if t >= cutoff]
    return s.mid_history


def _prune_trade_flow(s: MarketShadow, window_sec: float) -> list:
    """Prune trade_flow to the last window_sec seconds; return the slice."""
    cutoff = time.time() - window_sec
    s.trade_flow = [(t, sz, side) for (t, sz, side) in s.trade_flow if t >= cutoff]
    return s.trade_flow


def _rolling_mid_stdev(s: MarketShadow) -> float:
    """Population stdev of mid observations over VOL_WINDOW_SEC.
    Returns 0.0 if fewer than 2 samples."""
    hist = _prune_mid_history(s, VOL_WINDOW_SEC)
    if len(hist) < 2:
        return 0.0
    mids = [m for (_, m) in hist]
    mean = sum(mids) / len(mids)
    var = sum((m - mean) ** 2 for m in mids) / len(mids)
    return var ** 0.5


def _price_velocity(s: MarketShadow) -> float:
    """|mid_now - mid_{window_start}| over VELOCITY_WINDOW_SEC.
    Returns 0.0 if fewer than 2 samples."""
    hist = _prune_mid_history(s, VELOCITY_WINDOW_SEC)
    if len(hist) < 2:
        return 0.0
    return abs(hist[-1][1] - hist[0][1])


def _trade_imbalance(s: MarketShadow) -> float:
    """|yes_taker_vol - no_taker_vol| / total_vol over IMBALANCE_WINDOW_SEC.
    Higher = more one-sided flow = info-asymmetry signal.
    Fails open (returns 0.0) below MIN_IMBALANCE_SAMPLES — with only 1-2
    trades, "100% one-sided" is a false positive."""
    flow = _prune_trade_flow(s, IMBALANCE_WINDOW_SEC)
    if len(flow) < MIN_IMBALANCE_SAMPLES:
        return 0.0
    yes_vol = sum(sz for (_, sz, side) in flow if side == "yes")
    no_vol = sum(sz for (_, sz, side) in flow if side == "no")
    total = yes_vol + no_vol
    if total <= 0:
        return 0.0
    return abs(yes_vol - no_vol) / total


def _recent_buy_accumulation(s: MarketShadow, window_sec: float) -> float:
    """Sum of BUY-side contract fills for this market over the last window_sec.
    Drives the accumulation-rate limit in handle_trade."""
    cutoff = time.time() - window_sec
    return sum(sz for (t, sz, side) in s.recent_fills
               if side == 'B' and t >= cutoff)


def _meets_market_conditions(s: MarketShadow) -> Tuple[bool, str]:
    """Five-gate real-time check. Returns (ok, reason_code).
    First failure short-circuits with a reason string used for both
    telemetry and the hold-or-exit decision."""
    # v5.9f gate 0a: post-subscription admission. Markets that failed to
    # achieve a valid book within MARKET_ADMISSION_GRACE_SEC of
    # subscription are silently dropped from quoting. Dedicated reason
    # code keeps these out of the no_book gate-reject histogram so the
    # counter stops reflecting universe-admission noise.
    if s.admission_rejected:
        return False, "admission_rejected"
    # Gate 0: liveness — market must have been observed trading at least once.
    if s.trades_seen < MIN_TRADES_FOR_WARMUP:
        return False, "warmup"

    # Gate 1: book quality (two-sided, not structurally broken). Absolute-
    # dollar ceiling is both mid-independent and tick-independent — works on
    # tapered markets where cached tick is wrong at mid-range. Depth is
    # handled by _effective_per_market_cap, not as a gate.
    if s.best_bid <= 0 or s.best_ask <= 0 or s.best_ask <= s.best_bid:
        return False, "no_book"
    if (s.best_ask - s.best_bid) > MAX_SPREAD_DOLLARS:
        return False, "wide_spread"

    # Gate 2: rolling volatility of mid.
    if _rolling_mid_stdev(s) > MAX_ROLLING_VOL:
        return False, "high_vol"

    # v5.4 feeder: emit velocity signal regardless of gate outcome —
    # downstream (Engine 2) wants to know about velocity spikes even
    # when Engine 1 itself declines to trade them.
    vel = _price_velocity(s)
    _check_velocity_spike(s, vel)

    # Gate 3: short-term price velocity (regime-change detector).
    # v5.9: threshold is spread-relative with an absolute floor and a
    # safety ceiling. Economic meaning: reject when mid moved more than
    # a fraction of quote width in the window — that's when passive
    # quotes are stale. Prior absolute-only gate was miscalibrated
    # across tick regimes (see VELOCITY_* constant block).
    spread = s.best_ask - s.best_bid if (s.best_bid > 0 and s.best_ask > 0) else 0.0
    vel_threshold = max(MAX_VELOCITY_FLOOR, VELOCITY_FRAC_SPREAD * spread)
    vel_threshold = min(vel_threshold, MAX_VELOCITY)
    if vel > vel_threshold:
        return False, "fast_move"

    # Gate 4: trade-flow imbalance (info-asymmetry proxy).
    imb = _trade_imbalance(s)
    direction = "bid_heavy" if imb > 0 else "ask_heavy"
    _check_imbalance_sustained(s, imb, direction)
    if imb > MAX_IMBALANCE:
        return False, "imbalance"

    # v5.4 feeder: emit flow-burst signal (independent of gate). Runs
    # after other checks so signals only emit from markets that at least
    # have clean books.
    _check_flow_burst(s)

    return True, "ok"


def _should_hold(s: MarketShadow) -> bool:
    """Hold-or-exit decision for a market with inventory when the real-time
    gate fails. Returns True = hold silently (wait out the transient),
    False = liquidate via stage-2 cross-exit.

    Four gates — any failure forces exit:
      1. Close-time proximity / DTR — resolution within CLOSE_TIME_EXIT_HOURS
         or DTR below DTR_THRESHOLD_FOR_OVERNIGHT_HOLD → exit
      2. Price deviation from cost basis — adverse move > PRICE_DEVIATION_EXIT_PCT
      3. Oversized position — inventory > OVERSIZED_POSITION_MULTIPLE × ceiling
      4. Regime-change signals — last gate was high_vol / fast_move / imbalance
         (warmup / book-quality trips are transient; regime signals are not)
    """
    if s.inventory <= 0:
        return False

    # Gate 1: close-time proximity
    if s.close_time_utc is not None:
        hours_to_close = (s.close_time_utc - datetime.now(timezone.utc)).total_seconds() / 3600.0
        days_to_close = hours_to_close / 24.0
        if hours_to_close <= CLOSE_TIME_EXIT_HOURS:
            print(f"  ⚠ EXIT {s.ticker[:32]}  resolves in {hours_to_close:.1f}h "
                  f"(< {CLOSE_TIME_EXIT_HOURS:.0f}h threshold)", flush=True)
            return False
        if days_to_close < DTR_THRESHOLD_FOR_OVERNIGHT_HOLD:
            print(f"  ⚠ EXIT {s.ticker[:32]}  DTR={days_to_close:.1f}d "
                  f"< {DTR_THRESHOLD_FOR_OVERNIGHT_HOLD}d", flush=True)
            return False

    # Gate 2: price deviation from cost basis
    if s.cost_basis > 0 and s.best_bid > 0 and s.best_ask > 0:
        mid = (s.best_bid + s.best_ask) / 2.0
        deviation_pct = abs(s.cost_basis - mid) / s.cost_basis
        if deviation_pct > PRICE_DEVIATION_EXIT_PCT:
            # v5.2 log-spam fix: once we've logged the deviation-exit on a
            # position, don't re-log it every tick. Track per-market so a
            # re-entry on the same ticker re-enables logging after unwind.
            if not s.liquidating:
                print(f"  ⚠ EXIT {s.ticker[:32]}  cost={s.cost_basis:.3f} mid={mid:.3f} "
                      f"dev={deviation_pct*100:.1f}% > {PRICE_DEVIATION_EXIT_PCT*100:.0f}%", flush=True)
            return False

    # Gate 3: oversized position (compare to STATIC ceiling — dynamic depth
    # fluctuations would trigger false exits).
    mid = (s.best_bid + s.best_ask) / 2.0 if s.best_ask > 0 else s.cost_basis
    inv_usd = s.inventory * max(mid, 0.001)
    max_per_mkt = MAX_PER_MARKET_USD * s.cap_multiplier
    if inv_usd > max_per_mkt * OVERSIZED_POSITION_MULTIPLE:
        print(f"  ⚠ EXIT {s.ticker[:32]}  oversized: ${inv_usd:.2f} > "
              f"{OVERSIZED_POSITION_MULTIPLE}x cap (${max_per_mkt:.2f})", flush=True)
        return False

    # Gate 4: regime-change signals. Only STRONG price-based signals force exit:
    # high_vol = rolling stdev ceiling tripped (genuine volatility regime shift)
    # fast_move = |Δmid| over 30s too high (genuine price dislocation)
    # Imbalance is intentionally excluded — it's a flow signal with a 15s window
    # that flickers above 80% on noisy sparse flow. Forcing a cross-exit on every
    # imbalance trip costs taker fees for flow that often clears in seconds.
    # Combined signals (imbalance + actual price movement) are caught by Gate 2
    # (PRICE_DEVIATION_EXIT_PCT), which requires real adverse price movement.
    if s.last_gate_reason in ("high_vol", "fast_move"):
        if not s.liquidating:
            print(f"  ⚠ EXIT {s.ticker[:32]}  regime change [{s.last_gate_reason}]", flush=True)
        return False

    return True


def compute_quotes(s: MarketShadow):
    """Recompute the virtual bid/ask quotes for this market.
    Order of operations:
      0. v5.9k global quote kill switch  — sensor-only mode gate
      1. Blacklist gate         — hard disable
      2. Real-time gate         — book quality, vol, velocity, imbalance
      3. Book-readiness check   — defensive
      4. Risk detectors         — net-growth, turnover
      5. Liquidation execution  — if active
      6. A-S skewed placement   — normal quoting path
      7. Cap / break-even gates — last-line position sizing
    """
    tick = s.tick

    # v5.9k Gate 0: master quote kill switch. Sensor-only mode.
    # Existing inventory still unwinds via liquidation paths (gate 5
    # below still runs for holdings > 0), but no NEW quotes are placed.
    # Clears any stale quote state so fills can't fire against old px.
    if not SHADOW_QUOTE_ENABLED and s.inventory <= 0:
        s.our_buy_px = 0.0
        s.our_sell_px = 0.0
        return

    # 1. Blacklist / structural-break: permanent disable, unwind inventory.
    if s.cap_multiplier <= 0.0:
        s.our_buy_px = 0.0
        if s.inventory > 0 and s.best_bid > 0:
            _start_liquidation(s, "disabled")
            s.liquidation_started_ts = time.time() - (LIQ_STAGE_1_DURATION_MIN * 60 + 1)
            s.our_sell_px = _liquidation_quote(s, tick)
        else:
            s.our_sell_px = 0.0
        return

    # 2. Real-time gate. Failure → hold or liquidate based on inventory state.
    ok, reason = _meets_market_conditions(s)
    s.last_gate_reason = reason
    if not ok:
        # Warmup is a market state, not a rate — tracking per-update hits
        # would drown out real gate signals in the telemetry.
        if reason != "warmup":
            s.gate_rejects[reason] = s.gate_rejects.get(reason, 0) + 1
        if s.inventory > 0 and s.best_bid > 0:
            if _should_hold(s):
                # Transient — stop accumulating but KEEP the sell-side passive
                # maker quote active so inventory can unwind naturally as the
                # transient condition clears. Zeroing both sides locks us into
                # the position and forces taker fees when we finally exit.
                s.our_buy_px = 0.0
                if s.best_ask > s.best_bid + tick - 1e-9:
                    s.our_sell_px = round(s.best_ask - tick, 4)
                else:
                    s.our_sell_px = round(s.best_ask, 4)
            else:
                # Regime change / close proximity — engage the 3-stage
                # cascade starting at stage 1 (passive maker at best_bid
                # + 1 tick for LIQ_STAGE_1_DURATION_MIN). v5.1b bug: this
                # branch used to backdate liquidation_started_ts by
                # (stage-1 duration + 1s), which skipped stage 1 entirely
                # and routed every fast_move / high_vol / imbalance trip
                # straight into stage-2 cross-spread. That behavior ate
                # ~$6.50 of taker-cascade losses across 16 gate-urgent
                # exits on a session where maker spread captured +$6.87 —
                # wiping out the edge almost entirely. Fix: don't backdate.
                # Let stage 1 run its full window so the inside-maker
                # quote can catch retail mean-reversion before we pay
                # taker. Stage 2 still fires after the stage-1 window if
                # the regime change persists.
                _start_liquidation(s, f"gate:{reason}")
                s.our_sell_px = _liquidation_quote(s, tick)
        else:
            s.our_buy_px = 0.0
            s.our_sell_px = 0.0
        return

    # 3. Defensive book check (the gate should have caught this).
    if s.best_bid <= 0 or s.best_ask <= 0 or s.best_ask <= s.best_bid:
        s.our_buy_px = 0.0
        s.our_sell_px = 0.0
        return

    mid = (s.best_bid + s.best_ask) / 2.0
    base_spread = s.best_ask - s.best_bid
    runtime_min = (time.time() - _START) / 60.0

    # v5.8-a Inventory-age gate — must run BEFORE the v5.4 spread-compression
    # early-return; otherwise stuck inventory in a tight market never gets
    # harvested (v5.4 bails before v5.8 ever executes). Enters SLOW
    # liquidation (non-urgent passive maker). Reuses v5.6 infra → no
    # cross-cascade, bounded by DTR + MAX_SLOW_LIQ_HOURS ceilings.
    if (s.inventory > 0 and s.first_inventory_ts > 0
            and not s.liquidating):
        inv_age_min = (time.time() - s.first_inventory_ts) / 60.0
        if inv_age_min >= MAX_INV_AGE_MIN:
            s.inventory_stale = True
            _start_liquidation(s, "inventory_stale")

    # v5.8-b Profit lock — also runs before v5.4 early-return so winners
    # with compressed spreads still flip to harvest mode. Once realized
    # P&L clears threshold AND inventory is still open, enter SLOW
    # liquidation to harvest the remainder.
    if (s.realized_pnl >= PROFIT_LOCK_THRESHOLD_USD
            and s.inventory > 0
            and not s.liquidating
            and not s.profit_locked):
        s.profit_locked = True
        _start_liquidation(s, "profit_lock")

    # v5.4 min-edge-after-fees gate. Before considering a NEW quote,
    # check that a round-trip at the current spread would clear the
    # MIN_NET_EDGE_PER_FILL floor. Skip this when we have existing
    # inventory — SELL must stay live for unwind even if the spread
    # compressed after entry (otherwise we orphan positions).
    expected_fee_per_leg = FEE_RATE_MAKER * mid * (1 - mid)
    expected_net_per_rt = (base_spread / 2.0) - 2 * expected_fee_per_leg
    if expected_net_per_rt < MIN_NET_EDGE_PER_FILL and s.inventory <= 0:
        # Flat market, spread too thin to work. Don't quote until it widens.
        s.our_buy_px = 0.0
        s.our_sell_px = 0.0
        return

    # 4a. Net-growth: inventory climbing faster than it's unwinding.
    if runtime_min >= NET_GROWTH_WINDOW_MIN and s.inventory > 0:
        net_growth = _net_growth_60min(s)
        if net_growth > NET_GROWTH_MAX_CONTRACTS:
            _start_liquidation(s, f"net_growth={net_growth:+.0f}")

    # 4b. Turnover: inventory stuck without enough sells to clear it.
    if runtime_min >= TURNOVER_WINDOW_MIN and s.inventory > 0 and not s.liquidating:
        now = time.time()
        recent_sells = [(t, sz) for (t, sz, side) in s.recent_fills
                        if side == 'S' and now - t < TURNOVER_WINDOW_MIN * 60]
        if len(recent_sells) < TURNOVER_MIN_SELLS_PER_WINDOW:
            _start_liquidation(s, "stale_no_sells")

    # 5. Liquidation execution.
    if s.liquidating and s.inventory > 0:
        s.our_buy_px = 0.0
        s.our_sell_px = _liquidation_quote(s, tick)
        return
    if s.liquidating and s.inventory <= 0:
        # Unwound — clear the flag and resume normal quoting.
        s.liquidating = False
        s.liquidation_stage = 0
        s.liquidation_started_ts = 0.0

    # 6. Normal A-S placement.
    #    Baseline: 1 tick inside each side if spread ≥ 2 ticks, else join queue.
    if base_spread < 2 * tick - 1e-9:
        raw_buy = s.best_bid
        raw_sell = s.best_ask
    else:
        raw_buy = s.best_bid + tick
        raw_sell = s.best_ask - tick

    market_cap_usd = _effective_per_market_cap(s)
    inv_dollars = s.inventory * max(mid, 0.01)

    # Inventory skew (A-S): shift both quotes to encourage inventory reduction.
    q_norm = max(-1.0, min(1.0, inv_dollars / max(market_cap_usd, 1.0)))
    skew = GAMMA * q_norm * base_spread
    extra_widen = SPREAD_WIDEN_MAX_PCT * abs(q_norm) * base_spread / 2.0

    def snap(px: float) -> float:
        return round(round(px / tick) * tick, 4)

    s.our_buy_px = snap(raw_buy - skew - extra_widen)
    s.our_sell_px = snap(raw_sell - skew + extra_widen)

    # v5.9k: even when holding inventory, SHADOW_QUOTE_ENABLED=0 must
    # block NEW buy quotes. SELL quotes stay live to unwind existing
    # positions. This lets sensor-mode be safe for a partially-held
    # engine — existing inventory drains out, no new inventory accrues.
    if not SHADOW_QUOTE_ENABLED:
        s.our_buy_px = 0.0

    # Sanity: crossed or out-of-bounds quotes = no quote.
    if s.our_buy_px >= s.our_sell_px or s.our_buy_px <= 0 or s.our_sell_px >= 1.0:
        s.our_buy_px = 0.0
        if not SHADOW_QUOTE_ENABLED and s.inventory > 0:
            # Sensor-mode + has inventory: keep SELL live for unwind.
            return
        s.our_sell_px = 0.0
        return

    # v5.7 MIN_NET_EDGE entry-quality gate. Before placing the BUY, verify
    # expected edge per contract clears MIN_NET_EDGE after both-leg maker
    # fees and slippage reserve. Conservative exit estimate: mid - 1 tick.
    # Entry-only: SELL (unwind) quotes are unaffected. Rejections logged
    # as gate_rejects["low_edge"] and appear in minute-summary gates line.
    # v5.9h: also capture the edge value + book state for distribution
    # analysis — answers whether rejections are near-misses or deep.
    if s.our_buy_px > 0:
        edge_val = _expected_buy_edge(s.our_buy_px, mid, tick)
        if edge_val < MIN_NET_EDGE:
            s.our_buy_px = 0.0
            s.gate_rejects["low_edge"] = s.gate_rejects.get("low_edge", 0) + 1
            _record_low_edge_near_miss(s, edge_val, mid,
                                        (s.best_ask - s.best_bid), tick)

    # 7a. Continuous fee-coverage check: if spread compressed below break-even
    #     at the current mid, stop quoting BUY (no new inventory). SELL stays
    #     live to unwind what we already hold.
    current_spread_ticks = base_spread / tick if tick > 0 else 0
    if current_spread_ticks < min_profitable_ticks(mid, tick):
        s.our_buy_px = 0.0

    # 7b. Per-market cap: stop buying if inventory already at the depth-scaled ceiling.
    if inv_dollars >= market_cap_usd:
        s.our_buy_px = 0.0

    # 7c. Global cap: stop all new buys if the portfolio is at its ceiling.
    if _global_inventory_usd() >= GLOBAL_INVENTORY_CAP_USD:
        s.our_buy_px = 0.0

    # 7d. Concurrency cap: if already at MAX_OPEN_MARKETS and this market
    #     has no inventory, don't open a new front. Forces round-trip
    #     completion before spreading across more markets.
    if s.inventory <= 0 and _count_open_markets() >= MAX_OPEN_MARKETS:
        s.our_buy_px = 0.0

    # 7e. Post-regime cooldown: suppress BUY quote on recently-exited markets.
    if s.regime_cooldown_until_ts > time.time():
        s.our_buy_px = 0.0

    # Don't sell if flat.
    if s.inventory <= 0:
        s.our_sell_px = 0.0

    # Re-arm flags: a quote that's moved far enough from the book re-enables fills.
    if not s.buy_armed and s.our_buy_px > 0 and s.best_ask > s.our_buy_px + tick:
        s.buy_armed = True
    if not s.sell_armed and s.our_sell_px > 0 and s.best_bid < s.our_sell_px - tick:
        s.sell_armed = True


def log_quote_snapshot(s: MarketShadow):
    with open(QUOTES_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            s.ticker, f"{s.best_bid:.4f}", f"{s.best_ask:.4f}",
            f"{s.our_buy_px:.4f}", f"{s.our_sell_px:.4f}",
            f"{s.inventory:.0f}", f"{s.realized_pnl:.2f}",
        ])


def log_rewards(s: MarketShadow):
    if s.best_bid <= 0 or s.best_ask <= 0:
        return
    mid = (s.best_bid + s.best_ask) / 2.0
    bid_dist = (mid - s.our_buy_px) if s.our_buy_px > 0 else 999
    ask_dist = (s.our_sell_px - mid) if s.our_sell_px > 0 else 999
    norm = ((bid_dist + ask_dist) / 2.0) / max(s.tick, 1e-9)
    s.cumulative_distance_score += norm
    s.score_samples += 1
    with open(REWARDS_LOG, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            s.ticker, f"{bid_dist:.4f}", f"{ask_dist:.4f}", f"{norm:.2f}",
        ])


# ── Fill detection ─────────────────────────────────────────────────

def handle_trade(s: MarketShadow, trade: dict):
    """Check whether an observed trade would have filled our virtual quote.
    Trade schema: {yes_price_dollars, count_fp, taker_side: 'yes'|'no'}.
    Every observed trade updates trade_flow (for imbalance signal) regardless
    of whether it crosses our quote."""
    s.trades_seen += 1
    s.last_trade_ts = time.time()
    yes_px_raw = trade.get("yes_price_dollars") or trade.get("yes_price")
    count_raw = trade.get("count_fp") or trade.get("count") or trade.get("size")
    taker_side = (trade.get("taker_side") or "").lower()
    if yes_px_raw is None or count_raw is None:
        return
    try:
        yes_px = float(yes_px_raw)
        size = float(count_raw)
    except (ValueError, TypeError):
        return
    if yes_px <= 0 or yes_px >= 1.0 or size <= 0:
        return

    # Populate imbalance signal from ALL observed trades (including this one).
    s.trade_flow.append((time.time(), size, taker_side))

    # Cumulative taker volume since subscription — drives discovery-time
    # two-sided flow filter.
    if taker_side == "yes":
        s.cumulative_yes_taker_vol += size
    elif taker_side == "no":
        s.cumulative_no_taker_vol += size

    # ── BUY fill: a taker sold YES at or below our_buy_px ──────────
    if (s.buy_armed and s.our_buy_px > 0 and taker_side == "no"
            and yes_px <= s.our_buy_px + 1e-9):

        # Post-regime cooldown: if this market recently forced a gate-driven
        # exit, don't re-enter for REGIME_COOLDOWN_MIN. Info events take
        # time to decay; re-buying during the decay window exposes us to
        # the same catalyst we just exited.
        if s.regime_cooldown_until_ts > time.time():
            s.gate_rejects["regime_cooldown"] = s.gate_rejects.get("regime_cooldown", 0) + 1
            return

        # Pre-fill gate re-check on the freshest state. The gate state that's
        # checked on book updates can be stale during rapid trade bursts — the
        # fills arrive back-to-back before compute_quotes() re-runs. By
        # re-running the gate here, we block new inventory at the moment
        # imbalance or volatility crosses the threshold, not after.
        # (trades_seen was just incremented at the top of this function, so
        # warmup is effectively impossible here — but skip the counter either
        # way for symmetry with compute_quotes.)
        ok, reason = _meets_market_conditions(s)
        if not ok:
            if reason != "warmup":
                s.gate_rejects[reason] = s.gate_rejects.get(reason, 0) + 1
            return

        # Entry-side queue-depth filter: need a bid queue deep enough to
        # later consume our inventory as maker.
        if s.best_bid_size < MIN_BID_DEPTH_FOR_ENTRY:
            return

        # Fee-coverage filter: if spread has compressed below break-even at
        # the current mid, reject the fill — we'd be buying at a loss.
        if s.best_ask > 0 and s.best_bid > 0:
            current_mid = (s.best_bid + s.best_ask) / 2.0
            current_spread_ticks = (s.best_ask - s.best_bid) / s.tick
            if current_spread_ticks < min_profitable_ticks(current_mid, s.tick):
                return

        # Per-market accumulation rate limit: a hard cap on how fast inventory
        # can grow in any single market within a rolling window. This prevents
        # burst fills from stacking beyond what the gate can react to and
        # enforces clip-size discipline independent of book depth.
        recent_buys = _recent_buy_accumulation(s, BUY_ACCUMULATION_WINDOW_SEC)
        rate_remaining = MAX_BUY_ACCUMULATION_CONTRACTS - recent_buys
        if rate_remaining < 1.0:
            s.gate_rejects["rate_limit"] = s.gate_rejects.get("rate_limit", 0) + 1
            return

        # Size the fill to per-market cap, global cap, AND rate-limit room.
        mid_for_size = (s.best_bid + s.best_ask) / 2.0 if s.best_ask > 0 else s.our_buy_px
        market_cap_usd = _effective_per_market_cap(s)
        inv_dollars = s.inventory * max(mid_for_size, 0.001)
        per_mkt_avail = max(market_cap_usd - inv_dollars, 0.0)
        global_avail = max(GLOBAL_INVENTORY_CAP_USD - _global_inventory_usd(), 0.0)
        max_by_cap = min(per_mkt_avail, global_avail) / s.our_buy_px if s.our_buy_px > 0 else 0
        fill_size = min(size, QUOTE_SIZE_CONTRACTS, max_by_cap, rate_remaining)
        if fill_size < 1.0:
            # Not enough cap or rate room — simulate posting a zero-size quote.
            return
        # Taker/maker: our buy is a taker fill if our price crossed the ask.
        is_taker = s.our_buy_px >= s.best_ask - 1e-9 if s.best_ask > 0 else False
        fee = compute_fee(fill_size, s.our_buy_px, is_taker)
        if is_taker:
            s.fees_paid_taker += fee
        else:
            s.fees_paid_maker += fee
        old_inv = s.inventory
        s.inventory += fill_size
        if s.inventory > 0:
            # Cost basis excludes fees (tracked separately in realized_pnl).
            s.cost_basis = (s.cost_basis * old_inv + s.our_buy_px * fill_size) / s.inventory
        # v5.8: mark inventory-age start on 0 → positive transition.
        if old_inv <= 0 and s.inventory > 0:
            s.first_inventory_ts = time.time()
        s.realized_pnl -= fee
        s.fills_buy += 1
        s.buy_armed = False
        s.recent_fills.append((time.time(), fill_size, 'B'))
        with open(TRADES_LOG, "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                s.ticker, "BUY", f"{s.our_buy_px:.4f}", f"{fill_size}",
                f"{s.inventory:.0f}", f"{s.realized_pnl:.2f}", s.title,
            ])
        print(f"  🟢 SHADOW BUY  {s.ticker[:32]:<34s} {fill_size:>6.1f}@{s.our_buy_px:.4f}  inv={s.inventory:.0f}", flush=True)
        return

    # ── SELL fill: a taker bought YES at or above our_sell_px ──────
    if (s.sell_armed and s.our_sell_px > 0 and taker_side == "yes"
            and yes_px >= s.our_sell_px - 1e-9 and s.inventory > 0):
        fill_size = min(size, s.inventory, float(QUOTE_SIZE_CONTRACTS))
        if fill_size <= 0:
            return
        is_taker = s.our_sell_px <= s.best_bid + 1e-9 if s.best_bid > 0 else False
        fee = compute_fee(fill_size, s.our_sell_px, is_taker)
        if is_taker:
            s.fees_paid_taker += fee
        else:
            s.fees_paid_maker += fee
        pnl = (s.our_sell_px - s.cost_basis) * fill_size - fee
        s.realized_pnl += pnl
        s.inventory -= fill_size
        s.fills_sell += 1
        s.sell_armed = False
        s.recent_fills.append((time.time(), fill_size, 'S'))
        # v5.8: reset inventory-age tracking when cycle closes (inv→0).
        if s.inventory <= 0:
            s.first_inventory_ts = 0.0
            s.inventory_stale = False
            s.profit_locked = False
        with open(TRADES_LOG, "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                s.ticker, "SELL", f"{s.our_sell_px:.4f}", f"{fill_size}",
                f"{s.inventory:.0f}", f"{s.realized_pnl:.2f}", s.title,
            ])
        print(f"  🟢 SHADOW SELL {s.ticker[:32]:<34s} {fill_size:>6.1f}@{s.our_sell_px:.4f}  pnl=${pnl:+.2f}  inv={s.inventory:.0f}", flush=True)

        # v5.2 P&L-based blacklist. If this market has accumulated enough
        # sells AND cumulative realized is below the loss threshold, the
        # market is adversely selecting us — permanent disable.
        if (s.fills_sell >= PNL_BLACKLIST_MIN_SELLS
                and s.realized_pnl < PNL_BLACKLIST_LOSS_USD
                and s.cap_multiplier > 0.0):
            s.cap_multiplier = 0.0
            s.blacklisted = True
            print(f"  🛑 PNL-BLACKLIST {s.ticker[:32]}  "
                  f"sells={s.fills_sell} realized=${s.realized_pnl:+.2f} "
                  f"< ${PNL_BLACKLIST_LOSS_USD:.2f} — disabled", flush=True)

        # v5.4 opportunity-cost deprioritization. After enough sells,
        # if the pnl-per-sell is below threshold (even if positive), this
        # market is burning our attention for no return — soft-demote to
        # a 30% cap_multiplier so it keeps a small presence but releases
        # slot capacity for better candidates. Not a hard blacklist —
        # the market can prove itself out at reduced size.
        if (s.fills_sell >= PNL_PER_SELL_MIN_SELLS
                and not s.deprioritized
                and not s.blacklisted
                and s.cap_multiplier > PNL_PER_SELL_DEPRIO_MULT):
            pnl_per_sell = s.realized_pnl / s.fills_sell
            if pnl_per_sell < PNL_PER_SELL_MIN:
                s.cap_multiplier = PNL_PER_SELL_DEPRIO_MULT
                s.deprioritized = True
                print(f"  📉 PNL-DEPRIO {s.ticker[:32]}  "
                      f"sells={s.fills_sell} pnl/sell=${pnl_per_sell:+.4f} "
                      f"< ${PNL_PER_SELL_MIN:.4f} — cap→{PNL_PER_SELL_DEPRIO_MULT}",
                      flush=True)


# ── Message routing ────────────────────────────────────────────────

def on_message(ws, raw):
    try:
        msg = json.loads(raw)
    except Exception:
        return
    msg_type = msg.get("type")
    msg_data = msg.get("msg") or {}

    if msg_type == "orderbook_snapshot":
        _debug_dump("orderbook_snapshot", msg)
        ticker = msg_data.get("market_ticker")
        if ticker and ticker in STATES:
            with _LOCK:
                s = STATES[ticker]
                apply_orderbook_snapshot(s, msg_data)
                s.book_updates += 1
                compute_quotes(s)
                log_quote_snapshot(s)
                log_rewards(s)

    elif msg_type == "orderbook_delta":
        _debug_dump("orderbook_delta", msg)
        ticker = msg_data.get("market_ticker")
        if ticker and ticker in STATES:
            with _LOCK:
                s = STATES[ticker]
                apply_orderbook_delta(s, msg_data)
                s.book_updates += 1
                compute_quotes(s)
                log_quote_snapshot(s)
                log_rewards(s)

    elif msg_type == "trade":
        _debug_dump("trade", msg)
        ticker = msg_data.get("market_ticker") or msg_data.get("ticker")
        if ticker and ticker in STATES:
            with _LOCK:
                handle_trade(STATES[ticker], msg_data)

    elif msg_type == "subscribed":
        print(f"  [ws] subscribed ok: {msg.get('msg', {})}", flush=True)

    elif msg_type == "error":
        print(f"  [ws ERROR] {msg}", flush=True)

    elif msg_type == "ok":
        pass  # ack

    else:
        # Unknown message type — log first occurrence for debugging
        pass


def on_error(ws, err):
    print(f"  [ws error] {err}", flush=True)


def on_close(ws, code, msg):
    print(f"  [ws closed] code={code} msg={msg}", flush=True)


def on_open(ws):
    tickers = list(STATES.keys())
    print(f"  [ws open] subscribing to {len(tickers)} markets...", flush=True)
    # Kalshi WS subscribe format
    sub = {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta", "trade"],
            "market_tickers": tickers,
        },
    }
    ws.send(json.dumps(sub))


# ── Main ───────────────────────────────────────────────────────────

def _tail_ranking_score(m: dict) -> float:
    """v4 legacy ranker: tail-weighted spread. Kept for rollback only.
    Use _tradability_score (v5.5) by default — it captures volume + DTR
    + taper penalty in addition to spread, aligning ranking with actual
    live fill productivity rather than static-looking universe."""
    spread = m.get("spread_ticks", 0)
    mid = m.get("mid", 0.5)
    if mid < MID_TAIL_LOW_MAX or mid > MID_TAIL_HIGH_MIN:
        return spread * TAIL_BONUS_FACTOR
    return spread


# v5.5 composite-tradability weights (env-tunable for calibration).
# v5.9j (2026-04-21): the v5.5 "taper penalty" was factually wrong.
# The low_edge distribution analysis — 935 rejections across 5 families
# — showed penny-tick markets (tick ≤ 0.001) have POSITIVE median edge
# (+0.00235 in politics) while cent-tick markets (tick == 0.01) are
# solidly negative (-0.008 to -0.019 across weather/sports/commod/ent).
# Flipping the term: penny-tick gets a BONUS, narrow-spread cent gets
# a PENALTY, wide-spread cent is neutral.
TRAD_WEIGHT_SPREAD = float(os.environ.get("TRAD_WEIGHT_SPREAD", "0.45"))
TRAD_WEIGHT_VOL = float(os.environ.get("TRAD_WEIGHT_VOL", "0.30"))
TRAD_WEIGHT_DTR = float(os.environ.get("TRAD_WEIGHT_DTR", "0.15"))
# v5.9j: renamed from TRAD_WEIGHT_TAPER. Weight bumped 0.10 → 0.15 —
# tick structure has proven a stronger predictor of edge than
# DTR or vol alone. Still env-tunable for further calibration.
TRAD_WEIGHT_TICK_BIAS = float(os.environ.get("TRAD_WEIGHT_TICK_BIAS", "0.15"))
# Spread threshold above which a cent-tick market is treated as
# neutral rather than penalized. Below this, cent-tick markets have
# no room for maker edge after fees — v5.9i data is the basis.
TICK_BIAS_WIDE_SPREAD_THRESHOLD = float(
    os.environ.get("TICK_BIAS_WIDE_SPREAD_THRESHOLD", "0.05"))
# v5.9j per-market cap multipliers. Set once at MarketShadow
# construction based on tick+spread. Concentrates deployable capital
# on the segment where the data says edge lives.
PENNY_CAP_MULT = float(os.environ.get("PENNY_CAP_MULT", "1.0"))
CENT_WIDE_CAP_MULT = float(os.environ.get("CENT_WIDE_CAP_MULT", "0.5"))
CENT_NARROW_CAP_MULT = float(os.environ.get("CENT_NARROW_CAP_MULT", "0.25"))
# Normalization anchors — values at which a signal hits its max contribution
TRAD_NORM_SPREAD_DOLLARS = float(os.environ.get("TRAD_NORM_SPREAD_DOLLARS", "0.10"))
TRAD_NORM_VOL_USD = float(os.environ.get("TRAD_NORM_VOL_USD", "500000"))
TRAD_NORM_DTR_DAYS = float(os.environ.get("TRAD_NORM_DTR_DAYS", "90"))
# Hard pre-rank exclusions — applied BEFORE scoring
# v5.5b calibration (2026-04-21): v5.5 over-filtered 556→5 markets.
# Fixes: vol floor $25k→$15k, taper exclusion default OFF (opt-in via env).
# Spread floor $0.025 retained — proven right by break-even math.
TRADABILITY_MIN_SPREAD = float(os.environ.get("TRADABILITY_MIN_SPREAD", "0.025"))
TRADABILITY_MIN_VOL = float(os.environ.get("TRADABILITY_MIN_VOL", "15000"))
TRADABILITY_EXCLUDE_TAPERED = os.environ.get("TRADABILITY_EXCLUDE_TAPERED", "0") == "1"
# Use new ranker by default; env flag to roll back to tail-weighted if needed
USE_TRADABILITY_RANKER = os.environ.get("USE_TRADABILITY_RANKER", "1") == "1"


def _tick_bias_score(tick_step: float, spread: float) -> float:
    """v5.9j: signed tick-structure score in [-1, 1].
      +1.0   — penny-tick (tick ≤ 0.001). Evidence: positive median edge.
       0.0   — cent-tick with wide spread (tick == 0.01, spread ≥ $0.05).
              Neutral: not shown to produce edge, but spread may compensate.
      -1.0   — cent-tick narrow spread (tick == 0.01, spread < $0.05).
              Evidence: systematically negative edge across all families.
    Other tick sizes fall back to 0 (no strong prior either way)."""
    if tick_step <= 0.001:
        return 1.0
    if tick_step == 0.01:
        return 0.0 if spread >= TICK_BIAS_WIDE_SPREAD_THRESHOLD else -1.0
    return 0.0


def _initial_cap_multiplier(tick_step: float, spread: float) -> float:
    """v5.9j: tick-structure-aware initial cap_multiplier. Applied once
    at MarketShadow construction. Runtime demotion logic still applies
    on top (blacklist, inv-trap demote, etc.) — this is the PRIOR, not
    a ceiling."""
    if tick_step <= 0.001:
        return PENNY_CAP_MULT
    if tick_step == 0.01:
        if spread >= TICK_BIAS_WIDE_SPREAD_THRESHOLD:
            return CENT_WIDE_CAP_MULT
        return CENT_NARROW_CAP_MULT
    # Unknown tick (e.g. tapered markets with variable tick) — default to
    # penny treatment since those markets typically behave more like penny.
    return PENNY_CAP_MULT


def _tick_bucket(tick_step: float) -> str:
    """v5.9j: classify a market into 'penny' / 'cent' / 'other' for
    per-bucket PnL and fill telemetry."""
    if tick_step <= 0.001:
        return "penny"
    if tick_step == 0.01:
        return "cent"
    return "other"


def _aggregate_by_tick_bucket() -> Tuple[Dict[str, int],
                                          Dict[str, float],
                                          Dict[str, int]]:
    """v5.9j: roll up fills (buy + sell) and realized P&L across all
    STATES by tick bucket. Returns (fills, pnl, market_count) dicts
    keyed by bucket. Safe to call at any time — read-only over STATES."""
    fills: Dict[str, int] = {"penny": 0, "cent": 0, "other": 0}
    pnl: Dict[str, float] = {"penny": 0.0, "cent": 0.0, "other": 0.0}
    mkts: Dict[str, int] = {"penny": 0, "cent": 0, "other": 0}
    for s in STATES.values():
        bucket = _tick_bucket(s.tick)
        mkts[bucket] = mkts.get(bucket, 0) + 1
        fills[bucket] = fills.get(bucket, 0) + s.fills_buy + s.fills_sell
        pnl[bucket] = pnl.get(bucket, 0.0) + s.realized_pnl
    return fills, pnl, mkts


def _tradability_score(m: dict) -> float:
    """v5.5 composite tradability score. Replaces tail-weighted-spread.

    Composite of four signals:
      - spread          (45% weight, [0,1] normalized) — maker edge room
      - volume 24h      (30% weight, [0,1] normalized) — counterparty flow
      - DTR             (15% weight, [0,1] normalized) — not too close to resolution
      - tick_bias       (15% weight, [-1, +1]) — v5.9j tick-structure prior

    Higher score = more likely to produce actual fills AND real edge.
    """
    spread = m.get("spread", 0) or 0
    vol = m.get("vol_24h", 0) or 0
    dtr = m.get("dtr", 0) or 0
    tick_step = m.get("tick_step", 0.01) or 0.01
    norm_spread = min(1.0, spread / max(TRAD_NORM_SPREAD_DOLLARS, 1e-6))
    norm_vol = min(1.0, vol / max(TRAD_NORM_VOL_USD, 1e-6))
    norm_dtr = min(1.0, dtr / max(TRAD_NORM_DTR_DAYS, 1e-6))
    tick_bias = _tick_bias_score(tick_step, spread)
    return (TRAD_WEIGHT_SPREAD * norm_spread
            + TRAD_WEIGHT_VOL * norm_vol
            + TRAD_WEIGHT_DTR * norm_dtr
            + TRAD_WEIGHT_TICK_BIAS * tick_bias)


def _meets_tradability_exclusions(m: dict) -> bool:
    """v5.5 hard pre-rank exclusions. Return False to drop from the pool
    entirely before ranking. Catches the obvious bad candidates so the
    ranker only sees viable options."""
    spread = m.get("spread", 0) or 0
    vol = m.get("vol_24h", 0) or 0
    tick_step = m.get("tick_step", 0.01) or 0.01
    if spread < TRADABILITY_MIN_SPREAD:
        return False
    if vol < TRADABILITY_MIN_VOL:
        return False
    if TRADABILITY_EXCLUDE_TAPERED and tick_step < 0.01:
        return False
    return True


def _meets_fee_coverage(m: dict) -> bool:
    """Pre-filter: candidate's spread_ticks must clear the break-even required
    at its current mid (including FEE_COVERAGE_BUFFER margin)."""
    mid = m.get("mid", 0.5)
    tick = m.get("tick_step", 0.01)
    spread_t = m.get("spread_ticks", 0)
    return spread_t >= min_profitable_ticks(mid, tick)


def _parse_close_time(m: dict) -> Optional[datetime]:
    """Parse close_time ISO string from a candidate dict; None if missing/invalid."""
    raw = m.get("close_time")
    if not raw:
        # Fallback: if dtr was provided (days), approximate close_time from now.
        dtr = m.get("dtr")
        if dtr is not None:
            try:
                return datetime.now(timezone.utc) + timedelta(days=float(dtr))
            except (ValueError, TypeError):
                return None
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


def _is_well_formed_candidate(m: dict) -> bool:
    """v5.5b: reject candidates missing essentials before any other filter.
    Guards against malformed rows in kalshi_candidates_v2.json (empty ticker,
    null bid/ask, non-numeric fields) that would otherwise survive into
    STATES and produce warming-forever shadows."""
    if not isinstance(m, dict):
        return False
    if not m.get("ticker"):
        return False
    # bid/ask must be present and numeric and positive
    bid = m.get("yes_bid")
    ask = m.get("yes_ask")
    if bid is None or ask is None:
        return False
    try:
        if float(bid) <= 0 or float(ask) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    return True


def load_candidates():
    """Load the top-N candidate universe from the JSON file written by
    diag_kalshi_v2.py. v5.5b: malformed rejection + hard pre-rank exclusions
    + composite tradability ranking. Populates STATES and prints a
    drop-count breakdown so we can see which filter is doing the work.
    """
    with open(CANDIDATES_PATH) as f:
        data = json.load(f)
    raw_total = len(data)
    # v5.5b: malformed-candidate rejection before anything else
    wellformed = [m for m in data if _is_well_formed_candidate(m)]
    dropped_malformed = raw_total - len(wellformed)
    # Base filters (valid book, fee coverage) apply always
    base_filtered = [m for m in wellformed
                     if m.get("spread_ticks", 0) >= MIN_SPREAD_TICKS
                     and _meets_fee_coverage(m)]
    dropped_base = len(wellformed) - len(base_filtered)
    # v5.5 tradability exclusions on top of base
    dropped_spread = dropped_vol = dropped_taper = 0
    if USE_TRADABILITY_RANKER:
        filtered = []
        for m in base_filtered:
            spread = m.get("spread", 0) or 0
            vol = m.get("vol_24h", 0) or 0
            tick_step = m.get("tick_step", 0.01) or 0.01
            if spread < TRADABILITY_MIN_SPREAD:
                dropped_spread += 1
                continue
            if vol < TRADABILITY_MIN_VOL:
                dropped_vol += 1
                continue
            if TRADABILITY_EXCLUDE_TAPERED and tick_step < 0.01:
                dropped_taper += 1
                continue
            filtered.append(m)
        filtered.sort(key=lambda x: -_tradability_score(x))
        rank_label = "composite-tradability"
    else:
        # Rollback path: legacy tail-weighted-spread on a looser filter
        filtered = [m for m in base_filtered if m.get("vol_24h", 0) >= 10000]
        filtered.sort(key=lambda x: -_tail_ranking_score(x))
        rank_label = "tail-weighted-spread (legacy)"
    top = filtered[:TOP_N_MARKETS]
    print(f"[v5.5b] funnel: raw={raw_total}  malformed={dropped_malformed}  "
          f"base={dropped_base}  spread<${TRADABILITY_MIN_SPREAD:.3f}={dropped_spread}  "
          f"vol<${TRADABILITY_MIN_VOL/1000:.0f}k={dropped_vol}  "
          f"taper={'on' if TRADABILITY_EXCLUDE_TAPERED else 'off'}({dropped_taper})  "
          f"→ eligible={len(filtered)}  subscribed={min(len(top), TOP_N_MARKETS)}")
    print(f"Loaded {len(top)} candidate markets (top-{TOP_N_MARKETS} by {rank_label}):")
    for m in top:
        tick_val = m.get("tick_step", 0.01)
        spread_val = m.get("spread", 0) or 0
        s = MarketShadow(
            ticker=m["ticker"],
            title=m.get("title", "")[:60],
            tick=tick_val,
            vol_24h=m.get("vol_24h", 0),
            close_time_utc=_parse_close_time(m),
            # v5.9j: initial cap by tick structure. 1.0 for penny,
            # 0.5 for wide cent, 0.25 for narrow cent.
            cap_multiplier=_initial_cap_multiplier(tick_val, spread_val),
        )
        STATES[s.ticker] = s
        dtr_str = ""
        if s.close_time_utc:
            dtr_d = (s.close_time_utc - datetime.now(timezone.utc)).total_seconds() / 86400.0
            dtr_str = f"DTR={dtr_d:.1f}d"
        print(f"  {s.ticker:<35s} sp={m['spread_ticks']:.1f}t  tick={s.tick:.4f}  "
              f"vol=${s.vol_24h:,.0f}/d  {dtr_str:<10s}  {s.title[:35]}")
    print(f"\nPhase {SHADOW_PHASE}: global cap ${GLOBAL_INVENTORY_CAP_USD:.0f} | "
          f"per-market max ${MAX_PER_MARKET_USD:.0f} (depth-driven, scales with book)")
    print(f"Depth sizing: effective_cap = ${MAX_PER_MARKET_USD:.0f} × "
          f"min(1.0, min(bid_size,ask_size)/{REFERENCE_DEPTH_CONTRACTS})")
    print(f"Turnover gate: liquidate if inv>0 with <{TURNOVER_MIN_SELLS_PER_WINDOW} sells in {TURNOVER_WINDOW_MIN}min")
    print(f"Gates: warmup≥{MIN_TRADES_FOR_WARMUP}tr  spread≤${MAX_SPREAD_DOLLARS:.2f}  "
          f"vol≤${MAX_ROLLING_VOL:.2f}  "
          f"vel≤max(${MAX_VELOCITY_FLOOR:.3f}, {VELOCITY_FRAC_SPREAD:.2f}×spread)/"
          f"{VELOCITY_WINDOW_SEC}s cap=${MAX_VELOCITY:.2f}  "
          f"imb≤{MAX_IMBALANCE*100:.0f}%/{IMBALANCE_WINDOW_SEC}s")
    print(f"Rate limit: ≤{MAX_BUY_ACCUMULATION_CONTRACTS} new contracts per market "
          f"per {BUY_ACCUMULATION_WINDOW_SEC}s (pre-fill gate recheck enabled)")
    print(f"v5.1: concurrency={MAX_OPEN_MARKETS}  tail_bonus={TAIL_BONUS_FACTOR:.1f}  "
          f"flow_filter={MAX_OBSERVED_IMBALANCE*100:.0f}%  "
          f"regime_cooldown={REGIME_COOLDOWN_MIN}min  "
          f"slow_liq=passive-forever (no cross)")
    if FLOW_SIGNALS_ENABLED:
        print(f"v5.9c flow signals: publishing every {FLOW_SIGNALS_PUBLISH_SEC:.1f}s → "
              f"{FLOW_SIGNALS_PATH}  (regime threshold={FLOW_SIGNALS_THRESHOLD:.2f}, "
              f"weights={FLOW_W_IMBALANCE:.2f}/{FLOW_W_VELOCITY:.2f}/{FLOW_W_BIAS:.2f})")
    if MARKET_ADMISSION_ENABLED:
        print(f"v5.9f admission: drop markets not ever_valid within "
              f"{MARKET_ADMISSION_GRACE_SEC:.0f}s of subscribe")
    else:
        print("v5.9f admission: DISABLED (MARKET_ADMISSION_ENABLED=0, rollback mode)")
    if SHADOW_QUOTE_ENABLED:
        print("v5.9k: QUOTING ENABLED — MM strategy active")
    else:
        print("v5.9k: SENSOR-ONLY MODE (SHADOW_QUOTE_ENABLED=0) — "
              "no new quotes, existing inventory still unwinds, "
              "WS+flow publisher+telemetry all running")


# ══════════════════════════════════════════════════════════════════════
# WebSocket runner
# ══════════════════════════════════════════════════════════════════════

def ws_run():
    """Run the Kalshi WebSocket connection in a reconnect loop until _STOP."""
    global _ACTIVE_WS
    private_key = load_private_key()

    def subprotocol_or_header():
        from urllib.parse import urlparse
        parsed = urlparse(WS_URL)
        path = parsed.path
        hdrs = sign_request(private_key, "GET", path)
        return [f"{k}: {v}" for k, v in hdrs.items()]

    while not _STOP:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                header=subprotocol_or_header(),
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            _ACTIVE_WS = ws
            ws.run_forever(ping_interval=25, ping_timeout=10)
            _ACTIVE_WS = None
        except Exception as e:
            print(f"  [ws exception] {e}", flush=True)
        if not _STOP:
            print("  [ws] reconnecting in 3s...", flush=True)
            time.sleep(3)


# ══════════════════════════════════════════════════════════════════════
# Dynamic discovery (background thread)
# ══════════════════════════════════════════════════════════════════════

_KALSHI_PARLAY_PREFIXES = ("KXMVE", "KXMB")


def _fetch_fresh_candidates_inline():
    """Paginate the Kalshi /markets endpoint and return a filtered list of
    candidate dicts. Applies volume, DTR, and spread floors. Returns None on
    network failure so the caller can skip the cycle cleanly."""
    try:
        import requests
    except ImportError:
        print("  [discovery] requests not installed; skipping cycle", flush=True)
        return None

    base = "https://api.elections.kalshi.com/trade-api/v2"
    candidates = []
    cursor = None
    try:
        while True:
            params = {"limit": 1000, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(f"{base}/markets", params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            batch = data.get("markets", [])
            for m in batch:
                ticker = m.get("ticker", "")
                if any(ticker.startswith(p) for p in _KALSHI_PARLAY_PREFIXES):
                    continue
                vol = float(m.get("volume_24h_fp") or 0)
                if vol < 10000:
                    continue
                close_ts = m.get("close_time")
                dtr = None
                if close_ts:
                    try:
                        dt = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
                        dtr = (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
                    except (ValueError, TypeError):
                        pass
                if dtr is not None and dtr < MIN_DAYS_TO_RESOLUTION:
                    continue
                yb = float(m.get("yes_bid_dollars") or 0)
                ya = float(m.get("yes_ask_dollars") or 0)
                if yb <= 0 or ya <= 0 or ya <= yb:
                    continue
                spread = ya - yb
                mid = (yb + ya) / 2.0
                if mid < 0.05 or mid > 0.95:
                    continue
                tick_step = 0.01
                pr = m.get("price_ranges") or []
                if pr and isinstance(pr, list) and pr[0].get("step"):
                    try:
                        tick_step = float(pr[0]["step"])
                    except (ValueError, TypeError):
                        pass
                spread_ticks = spread / tick_step if tick_step > 0 else 0
                if spread_ticks < MIN_SPREAD_TICKS:
                    continue
                candidates.append({
                    "ticker": ticker,
                    "title": m.get("title", ""),
                    "vol_24h": vol,
                    "yes_bid": yb,
                    "yes_ask": ya,
                    "spread": spread,
                    "spread_ticks": round(spread_ticks, 2),
                    "mid": mid,
                    "tick_step": tick_step,
                    "dtr": round(dtr, 1) if dtr else None,
                    "close_time": close_ts,
                })
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
    except Exception as e:
        print(f"  [discovery] fetch error: {e}", flush=True)
        return None
    return candidates


def discovery_loop():
    """Every DISCOVERY_INTERVAL_MIN, refresh the universe from the Kalshi API
    and reconcile against STATES. Preserves state for still-qualifying markets,
    liquidates dropped markets that still have inventory, and triggers a WS
    reconnect so subscriptions match the new universe."""
    global _ACTIVE_WS
    # Wait for initial WS to establish before the first refresh.
    time.sleep(60)
    while not _STOP:
        # Sleep in small chunks so shutdown is responsive.
        for _ in range(DISCOVERY_INTERVAL_MIN * 60 // 5):
            if _STOP:
                return
            time.sleep(5)

        print("\n  [discovery] re-fetching Kalshi universe...", flush=True)
        fresh = _fetch_fresh_candidates_inline()
        if not fresh:
            print("  [discovery] fetch failed, skipping cycle", flush=True)
            continue
        fresh = [m for m in fresh if _meets_fee_coverage(m)]
        # v5.5 apply the same ranker + exclusions on discovery refresh
        if USE_TRADABILITY_RANKER:
            fresh = [m for m in fresh if _meets_tradability_exclusions(m)]
            fresh.sort(key=lambda x: -_tradability_score(x))
        else:
            fresh.sort(key=lambda x: -_tail_ranking_score(x))

        # v5.1: two-sided flow filter. For markets we've already subscribed to,
        # exclude any where observed taker flow is chronically one-sided — these
        # are structurally hostile to passive MM (we fill bids but can't exit
        # asks, forcing taker-fee liquidations). Fresh markets with no observation
        # pass through; they'll be filtered on the next cycle once data
        # accumulates.
        pre_filter = len(fresh)
        fresh = [m for m in fresh
                 if m["ticker"] not in STATES or _is_two_sided_flow(STATES[m["ticker"]])]
        excluded_one_sided = pre_filter - len(fresh)

        target = fresh[:TOP_N_MARKETS]
        target_tickers = {m["ticker"] for m in target}
        # Hysteresis zone: markets between TOP_N and RANKING_HYSTERESIS_SIZE
        # are retained (not promoted, not dropped) to prevent boundary churn.
        hysteresis_zone = {m["ticker"] for m in fresh[:RANKING_HYSTERESIS_SIZE]}

        with _LOCK:
            current_tickers = set(STATES.keys())
            to_add = target_tickers - current_tickers
            # Drop markets that fall outside the hysteresis zone AND aren't core.
            to_drop = {t for t in current_tickers
                       if t not in hysteresis_zone and t not in _CORE_UNIVERSE}
            # Promote long-term survivors to the core universe (discovery-immune).
            for t in current_tickers:
                if t in hysteresis_zone:
                    STATES[t].discovery_survivals += 1
                    if (STATES[t].discovery_survivals >= CORE_UNIVERSE_SURVIVAL_CYCLES
                            and t not in _CORE_UNIVERSE):
                        _CORE_UNIVERSE.add(t)
                        print(f"  [core] pinned to core universe: {t}", flush=True)

            added_count = 0
            blacklist_skips = 0
            for m in target:
                if m["ticker"] not in to_add:
                    continue
                if m["ticker"] in _UNIVERSE_BLACKLIST:
                    blacklist_skips += 1
                    continue
                tick_val = m.get("tick_step", 0.01)
                spread_val = m.get("spread", 0) or 0
                s = MarketShadow(
                    ticker=m["ticker"],
                    title=m.get("title", "")[:60],
                    tick=tick_val,
                    vol_24h=m.get("vol_24h", 0),
                    close_time_utc=_parse_close_time(m),
                    # v5.9j: apply tick-structure cap prior on discovery-added markets too.
                    cap_multiplier=_initial_cap_multiplier(tick_val, spread_val),
                )
                STATES[s.ticker] = s
                added_count += 1

            # Drops: if inventory, mark for liquidation (removed once flat);
            # if already flat, remove immediately.
            dropped_count = 0
            marked_for_exit = 0
            for t in list(to_drop):
                s = STATES[t]
                if s.inventory <= 0:
                    del STATES[t]
                    dropped_count += 1
                else:
                    _start_liquidation(s, "discovery_drop")
                    s.cap_multiplier = 0.0   # block any new buys
                    marked_for_exit += 1

            print(f"  [discovery] +{added_count} added, -{dropped_count} removed, "
                  f"{marked_for_exit} marked-for-exit, {blacklist_skips} blacklist-skip, "
                  f"{excluded_one_sided} flow-filtered, "
                  f"{len(STATES)} active (blacklist size: {len(_UNIVERSE_BLACKLIST)})", flush=True)

        # Trigger a WS reconnect so subscriptions match the new universe.
        if _ACTIVE_WS is not None:
            try:
                _ACTIVE_WS.close()
            except Exception:
                pass


def _print_minute_summary():
    """Emit the per-minute status line + gate-rejection breakdown.
    Reports: active quoting markets, warming-up markets (instantaneous state),
    cumulative book/trade/fill counts, P&L, fees, and gate-reject rates
    (warmup is excluded — it's surfaced as a state instead)."""
    now = time.time()
    elapsed_min = (now - _START) / 60.0
    with _LOCK:
        total_fills = sum(s.fills_buy + s.fills_sell for s in STATES.values())
        total_trades = sum(s.trades_seen for s in STATES.values())
        total_books = sum(s.book_updates for s in STATES.values())
        total_realized = sum(s.realized_pnl for s in STATES.values())
        total_fees_m = sum(s.fees_paid_maker for s in STATES.values())
        total_fees_t = sum(s.fees_paid_taker for s in STATES.values())
        active = sum(1 for s in STATES.values() if s.our_buy_px > 0 or s.our_sell_px > 0)
        warming_up = sum(1 for s in STATES.values() if s.trades_seen < MIN_TRADES_FOR_WARMUP)
        # Single pass over STATES to aggregate gate counters + v5.8
        # inventory-discipline telemetry (oldest age, stale/lock counts,
        # no-sells windows, trap-demoted count).
        gate_agg: Dict[str, int] = {}
        now_ts = time.time()
        oldest_age = 0.0
        stale_count = 0
        profit_locked_count = 0
        open_inv_count = 0
        no_sells_warn = 0
        no_sells_alert = 0
        trap_demoted = 0
        # v5.9 book-reconstruction diagnostics (cumulative since boot).
        snap_total = 0
        snap_1s_yes = 0
        snap_1s_no = 0
        side_zeroed = 0
        # v5.9d delta-path book diagnostics (cumulative since boot).
        total_yes_deltas = 0
        total_no_deltas = 0
        total_bid_empty = 0
        total_ask_empty = 0
        total_crossed = 0
        # v5.9e additions.
        total_empty_both = 0
        total_locked = 0
        born_invalid_now = 0     # currently invalid AND never valid
        became_invalid_now = 0   # currently invalid AND was once valid
        invalid_now = 0          # markets CURRENTLY in an invalid state
        # v5.9f: admission-state tally.
        admission_rejected_count = 0
        stalest: List[MarketShadow] = []  # selected for per-market print
        for s in STATES.values():
            for reason, n in s.gate_rejects.items():
                gate_agg[reason] = gate_agg.get(reason, 0) + n
            if s.inv_trap_strikes >= INV_TRAP_STRIKES_TO_DEMOTE:
                trap_demoted += 1
            snap_total += s.snapshot_count
            snap_1s_yes += s.snapshot_one_sided_yes_only
            snap_1s_no += s.snapshot_one_sided_no_only
            side_zeroed += s.book_side_zeroed_by_snapshot
            total_yes_deltas += s.yes_delta_count
            total_no_deltas += s.no_delta_count
            total_bid_empty += s.book_bid_empty_count
            total_ask_empty += s.book_ask_empty_count
            total_crossed += s.book_crossed_count
            total_empty_both += s.book_empty_both_count
            total_locked += s.book_locked_count
            if s.admission_rejected:
                admission_rejected_count += 1
            is_invalid = (s.best_bid <= 0 or s.best_ask <= 0
                          or s.best_ask <= s.best_bid)
            if is_invalid:
                invalid_now += 1
                if s.ever_valid:
                    became_invalid_now += 1
                else:
                    born_invalid_now += 1
                stalest.append(s)
            if s.inventory <= 0:
                continue
            open_inv_count += 1
            if s.inventory_stale:
                stale_count += 1
            if s.profit_locked:
                profit_locked_count += 1
            if s.first_inventory_ts > 0:
                age_min = (now_ts - s.first_inventory_ts) / 60.0
                if age_min > oldest_age:
                    oldest_age = age_min
            # Find most-recent sell on this market (or None if never sold).
            last_sell_ago = None
            for (t, sz, side) in reversed(s.recent_fills):
                if side == 'S':
                    last_sell_ago = (now_ts - t) / 60.0
                    break
            if last_sell_ago is None:
                # No sell on record — use hold duration as the "no-sells" age.
                last_sell_ago = ((now_ts - s.first_inventory_ts) / 60.0
                                 if s.first_inventory_ts > 0 else 0.0)
            if last_sell_ago >= NO_SELLS_WARN_MIN:
                no_sells_warn += 1
            if last_sell_ago >= NO_SELLS_ALERT_MIN:
                no_sells_alert += 1

    print(f"t+{elapsed_min:.0f}m  active={active}/{len(STATES)}  warming={warming_up}  "
          f"books={total_books}  trades={total_trades}  "
          f"fills={total_fills}  realized=${total_realized:+.2f}  "
          f"fees(m/t)=${total_fees_m:.2f}/${total_fees_t:.2f}", flush=True)
    if gate_agg:
        top_gates = sorted(gate_agg.items(), key=lambda x: -x[1])[:6]
        gate_str = "  ".join(f"{k}={v}" for k, v in top_gates)
        print(f"       gates: {gate_str}", flush=True)
    # v5.9: book-reconstruction diagnostics. Cumulative counters since
    # boot. Emit only when any snapshot has been seen (keeps line out of
    # the log before WS activity starts). If snap_1s_yes/no > 0 AND
    # correlates with `no_book` spikes, WS one-sided-snapshot hypothesis
    # is confirmed and the fix belongs in apply_orderbook_snapshot.
    if snap_total > 0:
        print(f"       book: snap={snap_total}  1s_yes={snap_1s_yes}  "
              f"1s_no={snap_1s_no}  zeroed={side_zeroed}", flush=True)
    # v5.9d: delta-path diagnostics. Emit whenever any deltas have been
    # applied OR any invalid transition has been counted — keeps the line
    # silent at boot but surfaces the failure path as soon as anything
    # non-trivial has happened.
    if (total_yes_deltas + total_no_deltas) > 0 or invalid_now > 0 \
            or (total_bid_empty + total_ask_empty + total_crossed
                + total_empty_both + total_locked) > 0:
        print(f"       book_delta: y={total_yes_deltas} n={total_no_deltas}  "
              f"invalid_now={invalid_now} (born={born_invalid_now} "
              f"became={became_invalid_now})  "
              f"bid_missing={total_bid_empty}  ask_missing={total_ask_empty}  "
              f"crossed={total_crossed}  empty_both={total_empty_both}  "
              f"locked={total_locked}  admission_rejected={admission_rejected_count}",
              flush=True)
        # v5.9g: break admission_rejected down by reason if any flipped.
        if admission_rejected_count > 0:
            never_valid_n = sum(
                1 for s in STATES.values()
                if s.admission_rejected
                and s.admission_rejected_reason == "never_valid")
            stuck_invalid_n = sum(
                1 for s in STATES.values()
                if s.admission_rejected
                and s.admission_rejected_reason == "stuck_invalid")
            print(f"       admission_reasons: never_valid={never_valid_n}  "
                  f"stuck_invalid={stuck_invalid_n}", flush=True)
    # v5.9h: low_edge near-miss distribution. Only print when any
    # low_edge rejection has been captured (silent at boot).
    if _LOW_EDGE_NEAR_MISS:
        dist = _low_edge_distribution()
        print(f"       low_edge_dist (n={len(_LOW_EDGE_NEAR_MISS)}): "
              f"pos_below_thr={dist['pos_below_thr']}  "
              f"near_neg={dist['near_neg']}  "
              f"small_neg={dist['small_neg']}  "
              f"med_neg={dist['med_neg']}  "
              f"far_neg={dist['far_neg']}", flush=True)
    # v5.9j: per-tick-bucket fills + realized P&L. Only emit when a
    # fill has actually landed somewhere so the line stays silent
    # before the engine produces any activity.
    bucket_fills, bucket_pnl, bucket_mkts = _aggregate_by_tick_bucket()
    total_fills = sum(bucket_fills.values())
    if total_fills > 0:
        parts = []
        for bucket in ("penny", "cent", "other"):
            n_mkts = bucket_mkts.get(bucket, 0)
            n_fills = bucket_fills.get(bucket, 0)
            pnl = bucket_pnl.get(bucket, 0.0)
            parts.append(f"{bucket}(mkts={n_mkts} fills={n_fills} "
                         f"pnl=${pnl:+.2f})")
        print(f"       tick_bucket: {'  '.join(parts)}", flush=True)
        # Per-market forensic line: up to 5 currently-invalid markets
        # with their level counts and per-side delta staleness. This is
        # the line that confirms (or refutes) the "one side stops
        # updating" hypothesis.
        if stalest:
            now_ts = time.time()
            for s in stalest[:5]:
                stale_y = ((now_ts - s.last_yes_delta_ts)
                           if s.last_yes_delta_ts > 0 else -1.0)
                stale_n = ((now_ts - s.last_no_delta_ts)
                           if s.last_no_delta_ts > 0 else -1.0)
                first = s.first_invalid_reason or "n/a"
                now_reason = _classify_invalid(s) or "n/a"
                print(f"         invalid: {s.ticker[:28]:<30s} "
                      f"ever_valid={str(s.ever_valid):<5s} "
                      f"bid={s.best_bid:.4f} ask={s.best_ask:.4f}  "
                      f"lvls(y/n)={len(s.yes_levels)}/{len(s.no_levels)}  "
                      f"stale(y/n)={stale_y:.0f}s/{stale_n:.0f}s  "
                      f"first={first} now={now_reason}", flush=True)
    # v5.9c.1: flow-regime tally from the most recent publish. Emit only
    # when the publisher is enabled and at least one market is in a
    # non-stable regime (keeps line muted on quiet periods).
    if FLOW_SIGNALS_ENABLED and (_FLOW_REGIME_COUNTS.get("pressure_up", 0)
                                  + _FLOW_REGIME_COUNTS.get("pressure_down", 0) > 0):
        print(f"       flow: pressure_up={_FLOW_REGIME_COUNTS.get('pressure_up', 0)}  "
              f"pressure_down={_FLOW_REGIME_COUNTS.get('pressure_down', 0)}  "
              f"stable={_FLOW_REGIME_COUNTS.get('stable', 0)}", flush=True)
    # v5.8: inventory telemetry — only print when something non-trivial is open.
    if open_inv_count > 0 or trap_demoted > 0:
        print(f"       inv: open={open_inv_count}  stale={stale_count}  "
              f"prof_lock={profit_locked_count}  oldest={oldest_age:.1f}m  "
              f"no_sells≥{NO_SELLS_WARN_MIN:.0f}m={no_sells_warn}  "
              f"≥{NO_SELLS_ALERT_MIN:.0f}m={no_sells_alert}  "
              f"trap_demoted={trap_demoted}", flush=True)


def _print_final_summary():
    """Print runtime totals and per-market fill breakdown on shutdown."""
    print("\n" + "=" * 90)
    print("FINAL")
    print("=" * 90)
    elapsed_h = (time.time() - _START) / 3600.0
    total_fills = sum(s.fills_buy + s.fills_sell for s in STATES.values())
    total_realized = sum(s.realized_pnl for s in STATES.values())
    print(f"runtime:      {elapsed_h:.2f}h")
    print(f"fills:        {total_fills}")
    print(f"realized P&L: ${total_realized:+.2f}")
    print("\nper-market with fills:")
    for s in sorted(STATES.values(), key=lambda x: -(x.fills_buy + x.fills_sell)):
        if s.fills_buy + s.fills_sell == 0:
            continue
        print(f"  {s.ticker[:32]:<34s} buy={s.fills_buy} sell={s.fills_sell} "
              f"realized=${s.realized_pnl:+.2f} inv={s.inventory:.0f}")
    # v5.9i: always emit the low_edge family analysis at shutdown so a
    # Ctrl-C before t+10min still produces the breakdown.
    try:
        _emit_low_edge_family_analysis()
    except Exception as e:
        print(f"[analysis/low_edge] emit failed: {type(e).__name__}: {e}",
              flush=True)


def main():
    if not KEY_ID or not PRIVATE_KEY_PATH:
        raise SystemExit("Missing env vars. Run: source ~/Documents/kalshi_secrets.env")
    ensure_logs()
    load_candidates()
    if not STATES:
        raise SystemExit("No markets loaded.")

    print()
    print("=" * 90)
    print(f"KALSHI SHADOW v5.1 — {len(STATES)} markets — Path 1 (two-sided flow + passive-liq) — "
          f"{datetime.now().isoformat(timespec='seconds')}")
    print("=" * 90)

    ws_thread = threading.Thread(target=ws_run, daemon=True)
    ws_thread.start()

    if DISCOVERY_ENABLED:
        disc_thread = threading.Thread(target=discovery_loop, daemon=True)
        disc_thread.start()
        print(f"[discovery] enabled — re-fetching every {DISCOVERY_INTERVAL_MIN} min", flush=True)
    else:
        print("[discovery] disabled (DISCOVERY_ENABLED=0)", flush=True)

    last_summary = time.time()
    last_flow_publish = 0.0
    # v5.9c: shorter sleep so the flow-signals publish cadence (default 2s)
    # is honored. Previous 5s sleep was fine when the only scheduled work
    # was the minute summary; with 1-2s publish target we need <=1s sleep.
    tick_sleep = min(1.0, FLOW_SIGNALS_PUBLISH_SEC) if FLOW_SIGNALS_ENABLED else 5.0
    while not _STOP:
        time.sleep(tick_sleep)
        now = time.time()
        if FLOW_SIGNALS_ENABLED and (now - last_flow_publish) >= FLOW_SIGNALS_PUBLISH_SEC:
            last_flow_publish = now
            _publish_flow_signals()
        # v5.9e: forensic invalid-market dumps at fixed elapsed times.
        # Cheap check each tick; emits at most once per threshold.
        _maybe_emit_forensic_dump()
        # v5.9f: admission sweep. One linear pass across STATES each
        # tick to flip admission_rejected on markets past grace.
        _sweep_market_admissions()
        if now - last_summary >= 60:
            last_summary = now
            _print_minute_summary()

    _print_final_summary()


if __name__ == "__main__":
    main()
