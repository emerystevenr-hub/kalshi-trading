"""Terminal 1 — Phase 2 Forward Paper Trader.

Reads latest model forecasts + latest Kalshi market snapshots. For each OPEN
Kalshi weather market (close_time in future) at a whitelisted station,
computes ensemble-implied YES probability and fires a shadow trade if edge
is above threshold.

Output: shadow positions in ~/Documents/shadow_pnl/ledger.jsonl tagged
engine=T1. Dashboard + reconciler pick up from there.

NO EXECUTION. This is paper-trading only.

Filters (Phase 1 verdict applied):
  - Station whitelist: NYC, LAX (ORD excluded — reverse monotonic edge metric)
  - Minimum edge: $0.08 (lowest-bucket noise filter)
  - Trade-lead window: only trade target_dates 12-24h out (proven skill zone)
  - Dedupe: won't re-open on a ticker already held

Usage:
    # Smoke test — dry run, log what would fire, open nothing:
    python3 ~/Documents/terminal1_phase2_paper_trader.py --dry-run --once

    # Real paper trade (opens shadow positions):
    python3 ~/Documents/terminal1_phase2_paper_trader.py --once

    # Daemonize, check every 30 min:
    nohup caffeinate -is python3 ~/Documents/terminal1_phase2_paper_trader.py \\
        --interval-sec 1800 > /dev/null 2>&1 &

Reads: forecasts_*.jsonl, nws_actuals_*.jsonl, kalshi_*.jsonl
Writes: shadow_pnl/ledger.jsonl (via ShadowLedger)
"""

import argparse
import json
import math
import re
import signal
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path.home() / "Documents"))
from terminal1_ensemble_backtest import (  # noqa: E402
    aggregate_daily,
    compute_bias,
    build_ensemble,
    bucket_probability,
    load_jsonl,
    DATA_DIR,
    MODELS,
)
# Alias for readability
compute_bias_correction = compute_bias
from shadow_pnl_core import ShadowLedger  # noqa: E402


STATION_WHITELIST = ["NYC", "LAX", "MIA", "PHX", "ATL"]
# Expanded 2026-04-24 after retrosim calibration check:
#   Per-station calibration ratios on 723 historical predictions:
#     NYC 1.03, LAX 1.02, MIA 1.02, PHX 0.90, ATL 0.89 → all calibrated
#     DEN 0.77 (too over-confident, hold)
#     ORD 0.86 (reverse monotonic in Phase 1 buckets, hold)
#
# 2026-04-26 (post Apr 25 settlement, 57 settled):
#   - MIN_EDGE raised from 0.08 → 0.20. Sub-$0.12 buckets bled -$48.20.
#     $0.20+ bucket printed +$22.10. Tight filter, real edge.
#   - STATION_METRIC_BLOCKLIST adds ATL HIGH. Apr 25 cool-bias of -7.7°F
#     across 5 positions (-$4.20). Structural model error, not variance.
#     Re-enable after diagnosing Southeast surface-heating handling.
#   - σ widening fix (×1.5 + 3°F floor + ×1.10 highs) replaced with
#     empirical residual σ from terminal1_data/empirical_sigma.json.
MIN_EDGE = 0.20

# 2026-05-09 sub-bucket gate (Steve, post T2 archive / T6 doubling decisions):
# Whole-engine T1 lifetime edge is statistically negative after fees:
# mean -$0.40/close, lower-95 -$0.84, upper-95 +$0.04, n=311. Only the
# entry-price 40-50¢ bucket has positive lower-95 (+$0.18, n=24, 70.8% win).
# Restrict T1 to that subset only. The remaining capital is recycled to T6.
# Kill criterion: if 40-50¢ bucket lower-95 turns negative on next n=50,
# T1 archives entirely.
T1_ENTRY_MIN = 0.40
T1_ENTRY_MAX = 0.50

STATION_METRIC_BLOCKLIST = {("ATL", "high")}
EMPIRICAL_SIGMA_PATH = Path.home() / "Documents" / "terminal1_data" / "empirical_sigma.json"
TRADE_LEAD_HOURS_MIN = 12
TRADE_LEAD_HOURS_MAX = 36   # slight over-buffer vs 24h — allow catch during the day
FEE_PER_CONTRACT_ESTIMATE = 0.02
# DEFAULT_CONTRACTS is the per-fire flat sizing for T1. There is no Kelly
# sizing in T1; --contracts on the daemon command line is the lever for
# per-trade size. Bankroll discipline ($1K post-2026-05-09) is enforced via
# the launcher argv: at $0.40-0.50 entry × DEFAULT_CONTRACTS=10 × ~5 fires/day
# × hold-to-settle of 1-2 days, peak open exposure ~$25-50 per fire and ~$300
# total — comfortably inside $1K. Do NOT raise DEFAULT_CONTRACTS without
# also adding a real exposure check. See AUDIT_2026-05-09.md H-T1-2.
DEFAULT_CONTRACTS = 10
ENGINE = "T1"
MAX_POSITIONS_PER_CLUSTER = 5
# A "cluster" is one (station, target_date) pair. We cap positions per
# cluster to limit single-day model-miss disasters (Apr 24 lost -$12 because
# 10 LAX positions all hit the same 4°F LAX HIGH model error). Top-N by edge
# wins when >N signals pass the filter. 5 covers most Kalshi event grids
# (5-7 buckets per metric) without over-concentrating.

LOG_PATH = Path.home() / "Documents" / "terminal1_phase2_paper_trader.log"

_STOP = False


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _handle_sigint(sig, frame):
    global _STOP
    _STOP = True
    log("SIGINT — finishing current iteration.")


# ---------------------------------------------------------------------------
# Kalshi weather ticker parser
#
# Grammar (from terminal1_kalshi_logger.py):
#   KX(HIGH|LOW)(T?)<STATION>-<DATE>-<STRIKE>
#
# STRIKE is either:
#   T<int>   — threshold, e.g. T42 means "<42°" OR ">42°" (title disambiguates)
#   B<decimal> — bucket, e.g. B42.5 means "42-43°" (integer inclusive range)
# ---------------------------------------------------------------------------

TITLE_BELOW_RE = re.compile(r"<\s*(-?\d+)°", re.IGNORECASE)
TITLE_ABOVE_RE = re.compile(r">\s*(-?\d+)°", re.IGNORECASE)
TITLE_BUCKET_RE = re.compile(r"(-?\d+)\s*-\s*(-?\d+)°", re.IGNORECASE)


def parse_weather_ticker(ticker: str, title: str) -> Optional[dict]:
    """Return dict of parsed fields or None if not a parseable weather market.

    Output keys:
      which_metric: "high" or "low"
      strike_type: "threshold_below" | "threshold_above" | "bucket"
      strike_lo, strike_hi: float (°F) — range for P(YES) calc
                            For threshold_below: (-inf, strike]
                            For threshold_above: (strike, +inf)
                            For bucket: [lo, hi+1) (e.g. 42-43 → [42, 44))
    """
    t = ticker.upper()
    if t.startswith("KXHIGH"):
        metric = "high"
    elif t.startswith("KXLOW"):
        metric = "low"
    else:
        return None

    # Parse title for direction — more reliable than ticker-only
    m_below = TITLE_BELOW_RE.search(title)
    m_above = TITLE_ABOVE_RE.search(title)
    m_bucket = TITLE_BUCKET_RE.search(title)

    if m_above:
        strike = float(m_above.group(1))
        return {
            "which_metric": metric,
            "strike_type": "threshold_above",
            "strike_lo": strike,           # P(X > strike)
            "strike_hi": float("inf"),
            "strike_display": f">{int(strike)}°",
        }
    if m_below:
        strike = float(m_below.group(1))
        return {
            "which_metric": metric,
            "strike_type": "threshold_below",
            "strike_lo": float("-inf"),     # P(X < strike)
            "strike_hi": strike,
            "strike_display": f"<{int(strike)}°",
        }
    if m_bucket:
        lo = float(m_bucket.group(1))
        hi = float(m_bucket.group(2))
        # Kalshi bucket "42-43°" = integers 42 or 43. Use [lo, hi+1) continuous.
        return {
            "which_metric": metric,
            "strike_type": "bucket",
            "strike_lo": lo,
            "strike_hi": hi + 1.0,
            "strike_display": f"{int(lo)}-{int(hi)}°",
        }
    return None


_EMPIRICAL_SIGMA_CACHE: Optional[dict] = None


def _load_empirical_sigma() -> dict:
    """Lazy-load empirical_sigma.json (cached). Returns the parsed dict
    or {} if the file is missing — caller falls back to ensemble_std then
    a hard floor."""
    global _EMPIRICAL_SIGMA_CACHE
    if _EMPIRICAL_SIGMA_CACHE is not None:
        return _EMPIRICAL_SIGMA_CACHE
    try:
        with open(EMPIRICAL_SIGMA_PATH) as f:
            _EMPIRICAL_SIGMA_CACHE = json.load(f)
    except (OSError, json.JSONDecodeError):
        _EMPIRICAL_SIGMA_CACHE = {}
    return _EMPIRICAL_SIGMA_CACHE


def _resolve_sigma(station: str, which_metric: str, ensemble_std: float) -> float:
    """Resolve σ in this priority order:
       1. Per-cell empirical std (if n ≥ min_n_per_cell)
       2. Global empirical std for that metric
       3. Raw ensemble_std (model-disagreement) with 1°F floor
    """
    table = _load_empirical_sigma()
    if not table:
        return max(ensemble_std, 1.0)
    floor = float(table.get("sigma_floor", 1.0))
    min_n = int(table.get("min_n_per_cell", 5))

    cell = (table.get("by_station_metric") or {}).get(f"{station}_{which_metric}")
    if cell and cell.get("n", 0) >= min_n:
        return max(float(cell["std_resid"]), floor)

    glb = table.get(f"global_{which_metric}") or {}
    if glb.get("n", 0) >= min_n:
        return max(float(glb["std_resid"]), floor)

    return max(ensemble_std, floor)


# Probability clip — defensive against tight-σ artifacts from thin-N empirical
# fits. With n=6 per cell, several stations have σ ≤ 1.0°F floor; the normal
# CDF then collapses tail-bucket probabilities to 0.0000 or 1.0000 for buckets
# more than ~3°F from the ensemble mean. That produces "100% certain" signals
# that fire NO bets at $0.85+ entry — symmetric failure mode of T1 v1's wide-σ
# YES tail over-claiming. Clipping keeps the directional edge while preventing
# pathological certainty until σ fit has more N (target: ≥30 per cell).
PROB_CLIP_MIN = 0.05
PROB_CLIP_MAX = 0.95


def yes_probability_from_ensemble(
    parsed: dict,
    ensemble_mean: float,
    ensemble_std: float,
    station: Optional[str] = None,
) -> Optional[float]:
    """Compute P(YES) for a parsed Kalshi market given ensemble forecast.

    Sigma source (2026-04-26): empirical residual std per (station, metric)
    from terminal1_data/empirical_sigma.json. Output clipped to
    [PROB_CLIP_MIN, PROB_CLIP_MAX] — see comment above the constants.
    """
    if ensemble_std <= 0:
        return None
    which = parsed.get("which_metric") or "high"
    sigma = _resolve_sigma(station or "?", which, ensemble_std)
    lo = parsed["strike_lo"]
    hi = parsed["strike_hi"]

    if lo == float("-inf"):
        # P(X < hi)
        raw = _phi((hi - ensemble_mean) / sigma)
    elif hi == float("inf"):
        # P(X > lo) = 1 - P(X ≤ lo)
        raw = 1.0 - _phi((lo - ensemble_mean) / sigma)
    else:
        # Bucket: P(lo ≤ X < hi)
        raw = _phi((hi - ensemble_mean) / sigma) - _phi((lo - ensemble_mean) / sigma)

    # Clip to prevent pathological certainty on thin-N σ cells
    return max(PROB_CLIP_MIN, min(PROB_CLIP_MAX, raw))


def _phi(z: float) -> float:
    """Standard normal CDF via erfc."""
    return 0.5 * math.erfc(-z / math.sqrt(2))


# ---------------------------------------------------------------------------
# Kalshi snapshot reader
# ---------------------------------------------------------------------------

def load_latest_kalshi_snapshots(station: str) -> List[dict]:
    """Return the LATEST snapshot per (ticker) from today's + yesterday's
    kalshi_{station}_*.jsonl files. Each dict has the market fields the
    logger writes (ticker, title, yes_bid, yes_ask, close_time, etc).
    """
    now = datetime.now(timezone.utc)
    latest_by_ticker: Dict[str, dict] = {}
    for day_offset in (0, 1):   # today, yesterday
        d = (now - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        path = DATA_DIR / f"kalshi_{station}_{d}.jsonl"
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = r.get("ticker")
                if not t:
                    continue
                prev = latest_by_ticker.get(t)
                if prev is None or r.get("ts_utc", "") > prev.get("ts_utc", ""):
                    latest_by_ticker[t] = r
    return list(latest_by_ticker.values())


def already_open_on_ticker(engine: str, ticker: str) -> bool:
    """Check shadow ledger for an open position on this (engine, ticker).

    Honors annul_close events: if a close is later annulled, the position
    is restored to open. Mirrors the canonical replay pattern in
    shadow_pnl_core.compute_state. Fixed 2026-05-09 (audit H-T1-3) — prior
    version popped on close without checking subsequent annul_close, which
    allowed duplicate opens after any reconciler annul.
    """
    ledger_path = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"
    if not ledger_path.exists():
        return False
    open_records: Dict[str, dict] = {}
    closes: List[dict] = []
    annulled_close_ts: Dict[str, set] = {}
    try:
        with open(ledger_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = r.get("type")
                pid = r.get("position_id")
                if t == "open" and r.get("engine") == engine and r.get("ticker") == ticker:
                    open_records[pid] = r
                elif t == "close" and pid in open_records:
                    closes.append(r)
                elif t == "annul_close" and pid in open_records:
                    annul_ts = r.get("annulled_close_ts")
                    if annul_ts is not None:
                        annulled_close_ts.setdefault(pid, set()).add(annul_ts)
    except OSError:
        return False
    # A position is currently open if it was opened AND there is no
    # close for it whose ts is NOT in the annulled set.
    closed_pids = {
        c["position_id"] for c in closes
        if c.get("ts") not in annulled_close_ts.get(c.get("position_id"), set())
    }
    return any(pid not in closed_pids for pid in open_records)


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def generate_signals(
    ensemble: Dict[tuple, Dict[str, dict]],
    stations: List[str],
    min_edge: float,
) -> List[dict]:
    """Walk the open Kalshi market universe and compute signals.
    ensemble keys: (station, target_date) → {"high": {mean, std, n, members}, "low": {...}}
    """
    now = datetime.now(timezone.utc)
    signals: List[dict] = []

    for station in stations:
        kalshi_markets = load_latest_kalshi_snapshots(station)
        log(f"  [{station}] loaded {len(kalshi_markets)} kalshi snapshots")

        for m in kalshi_markets:
            ticker = m["ticker"]
            title = m.get("title", "")
            close_time = m.get("close_time")
            if not close_time:
                continue
            try:
                close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            except ValueError:
                continue
            hours_to_close = (close_dt - now).total_seconds() / 3600.0
            if hours_to_close <= 0:
                continue  # already closed
            if hours_to_close < TRADE_LEAD_HOURS_MIN or hours_to_close > TRADE_LEAD_HOURS_MAX:
                continue  # outside trade window

            parsed = parse_weather_ticker(ticker, title)
            if parsed is None:
                continue

            # 2026-04-26 station/metric blocklist (ATL HIGH paused — see
            # STATION_METRIC_BLOCKLIST docstring).
            if (station, parsed["which_metric"]) in STATION_METRIC_BLOCKLIST:
                continue

            # event_ticker format KX(HIGH|LOW)(T?)STN-YYMMMDD — target_date is YYMMMDD
            ev = m.get("event_ticker", "")
            target_date = _event_to_target_date(ev)
            if not target_date:
                continue

            # Look up ensemble for this (station, target_date)
            ens = ensemble.get((station, target_date))
            if ens is None:
                continue
            which = parsed["which_metric"]
            side_data = ens.get(which)
            if side_data is None:
                continue

            mu = side_data["mean"]
            sigma = side_data["std"]
            our_p = yes_probability_from_ensemble(parsed, mu, sigma, station=station)
            if our_p is None:
                continue

            yes_bid = m.get("yes_bid")
            yes_ask = m.get("yes_ask")
            if yes_bid is None or yes_ask is None:
                continue
            # Market-implied probability of YES
            market_p = (yes_bid + yes_ask) / 2.0

            # Side selection:
            #   If our_p > market_p → buy YES at ask
            #   If our_p < market_p → buy NO at (1 - yes_bid)
            if our_p > market_p:
                side = "YES"
                entry = yes_ask
                edge = our_p - market_p
            else:
                side = "NO"
                entry = 1.0 - yes_bid
                edge = market_p - our_p

            # Subtract fee estimate (contracts × fee)
            fee_est_per_contract = FEE_PER_CONTRACT_ESTIMATE
            # Our EV per contract at settlement =
            #   side=YES: our_p × 1 + (1 - our_p) × 0 - entry = our_p - entry
            #   side=NO:  (1-our_p) × 1 - entry
            if side == "YES":
                ev_per_contract = our_p - entry
            else:
                ev_per_contract = (1.0 - our_p) - entry
            ev_after_fee = ev_per_contract - fee_est_per_contract

            if edge < min_edge:
                continue

            # 2026-04-26 (after the σ-too-tight failure mode):
            # Filter 1: skip if our_p hit the clip boundary. our_p = 0.05 or
            # 0.95 means the model wanted to claim 0% or 100% certainty and
            # got capped — the underlying probability estimate is unreliable
            # because σ on this cell is too tight (n=6 empirical fit), not
            # because we have genuine 5%/95% confidence. Don't trade those.
            EPS = 1e-6
            if our_p <= 0.05 + EPS or our_p >= 0.95 - EPS:
                log(f"  [skip-clip-boundary] {ticker:<28} {side} our_p={our_p:.3f} "
                    f"hit clip boundary; σ-artifact suspect, not trading")
                continue

            # Filter 2: skip if EV after fees is non-positive. Edge ≥ min_edge
            # doesn't guarantee positive EV when entry price is near $1 (e.g.
            # NO @ $0.97 with our_p=0.05 → edge $0.20 but EV-fee = -$0.04).
            if ev_after_fee <= 0:
                log(f"  [skip-neg-ev]  {ticker:<28} {side} edge=${edge:.3f} but "
                    f"ev_post_fee=${ev_after_fee:+.3f} (entry too high)")
                continue

            # Filter 3 (2026-05-09): restrict to validated entry-price subset.
            # Whole-engine T1 lifetime is statistically negative after fees;
            # only the [T1_ENTRY_MIN, T1_ENTRY_MAX) entry bucket has positive
            # lower-95 CI. Bucket is HALF-OPEN: t1_edge_analysis.py classifies
            # entry==0.50 into the next decile ("50-60c"), so the validated
            # subset excludes 0.50. Gate boundary mirrors that semantic
            # (fixed 2026-05-09 from `> T1_ENTRY_MAX` which leaked entry=0.50
            # into the trader despite being out-of-sample).
            if entry < T1_ENTRY_MIN or entry >= T1_ENTRY_MAX:
                log(f"  [skip-bucket-gate] {ticker:<28} {side} entry=${entry:.3f} "
                    f"outside validated [{T1_ENTRY_MIN:.2f}, {T1_ENTRY_MAX:.2f}) subset")
                continue

            signals.append({
                "station": station,
                "target_date": target_date,
                "hours_to_close": hours_to_close,
                "ticker": ticker,
                "title": title[:100],
                "strike_display": parsed["strike_display"],
                "which_metric": which,
                "ensemble_mean": round(mu, 2),
                "ensemble_std": round(sigma, 2),
                "our_p": round(our_p, 4),
                "market_p": round(market_p, 4),
                "side": side,
                "entry_price": round(entry, 4),
                "edge": round(edge, 4),
                "ev_per_contract_pre_fee": round(ev_per_contract, 4),
                "ev_per_contract_after_fee": round(ev_after_fee, 4),
                "kalshi_volume_24h": m.get("volume_24h", 0),
                "kalshi_open_interest": m.get("open_interest", 0),
            })
    return signals


def _event_to_target_date(event_ticker: str) -> Optional[str]:
    """KX...-YYMMMDD → YYYY-MM-DD. E.g. KXHIGHTNYC-26APR24 → 2026-04-24."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    date_part = parts[-1]
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})$", date_part)
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2), m.group(3)
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    mi = months.get(mon)
    if mi is None:
        return None
    yyyy = 2000 + int(yy)
    return f"{yyyy:04d}-{mi:02d}-{int(dd):02d}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once(dry_run: bool, min_edge: float, contracts: int, stations: List[str]) -> dict:
    # Freshness gate (added 2026-05-08 after T1 NWS actuals went silently stale
    # for 7 days). If portfolio_freshness_watchdog wrote the flag, force dry-run
    # for this cycle. Watchdog auto-clears the flag once data recovers.
    _flag = Path.home() / "Documents" / "freshness_alarm.flag"
    if _flag.exists() and not dry_run:
        try:
            log(f"[freshness_alarm] flag present at {_flag} — forcing dry-run for this cycle")
            log(f"[freshness_alarm] {_flag.read_text().strip()}")
        except OSError:
            log(f"[freshness_alarm] flag present at {_flag} — forcing dry-run for this cycle")
        dry_run = True

    # Load all forecasts + actuals (actuals drive bias correction)
    forecasts: List[dict] = []
    for m in MODELS:
        for s in stations + (["ORD"] if "ORD" not in stations else []):
            # Include ORD for bias correction training even if we don't trade it
            p = DATA_DIR / f"forecasts_{m}_{s}.jsonl"
            forecasts.extend(load_jsonl(p))
    actuals: List[dict] = []
    for s in stations + (["ORD"] if "ORD" not in stations else []):
        p = DATA_DIR / f"nws_actuals_{s}.jsonl"
        actuals.extend(load_jsonl(p))

    log(f"loaded {len(forecasts):,} forecasts, {len(actuals):,} actuals")
    if not forecasts or not actuals:
        log("insufficient data — cannot generate ensembles")
        return {"signals": [], "opened": []}

    daily = aggregate_daily(forecasts)
    log(f"aggregated into {len(daily):,} daily forecast rows")

    actuals_idx = {(a["station"], a["date_local"]): a for a in actuals}
    bias_map = compute_bias_correction(daily, actuals_idx)
    log(f"bias correction ready for {len(bias_map)} cells")

    ensemble = build_ensemble(daily, bias_map)
    log(f"built ensembles for {len(ensemble)} (station, target_date) pairs")

    signals = generate_signals(ensemble, stations, min_edge)
    log(f"generated {len(signals)} candidate signals "
        f"(after station whitelist, edge≥{min_edge}, lead window)")

    # Rank by edge
    signals.sort(key=lambda s: -s["edge"])

    sl = ShadowLedger() if not dry_run else None
    opened: List[str] = []

    # Per-cluster cap: count existing open T1 positions by (station, target_date)
    # so the cap honors positions opened in earlier polls.
    from collections import defaultdict
    cluster_count: Dict[tuple, int] = defaultdict(int)
    ledger_path = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"
    if ledger_path.exists():
        opens_by_pid = {}
        closed_pids = set()
        annulled_close_ts_by_pid: Dict[str, set] = defaultdict(set)
        try:
            with open(ledger_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("engine") != ENGINE:
                        continue
                    if r.get("type") == "open":
                        opens_by_pid[r["position_id"]] = r
                    elif r.get("type") == "close":
                        closed_pids.add(r["position_id"])
                    elif r.get("type") == "annul_close":
                        # If close was annulled, the position is open again
                        annulled_close_ts_by_pid.setdefault(
                            r["position_id"], set()
                        ).add(r.get("annulled_close_ts"))
                        closed_pids.discard(r["position_id"])
        except OSError:
            pass
        for pid, op in opens_by_pid.items():
            if pid in closed_pids:
                continue
            meta = op.get("signal_metadata", {}) or {}
            station = meta.get("station")
            target_date = meta.get("target_date")
            if station and target_date:
                cluster_count[(station, target_date)] += 1

    for sig in signals:
        ticker = sig["ticker"]
        if already_open_on_ticker(ENGINE, ticker):
            log(f"  [skip] already open on {ticker}")
            continue

        cluster_key = (sig["station"], sig["target_date"])
        if cluster_count[cluster_key] >= MAX_POSITIONS_PER_CLUSTER:
            log(f"  [cap] {ticker:<30} {sig['side']} {sig['strike_display']:<8} "
                f"edge=${sig['edge']:.3f} — cluster {cluster_key} already at "
                f"{MAX_POSITIONS_PER_CLUSTER} positions")
            continue

        log(f"  [FIRE] {ticker:<30} {sig['side']} {sig['strike_display']:<8} "
            f"our_p={sig['our_p']:.3f}  mkt_p={sig['market_p']:.3f}  "
            f"edge=${sig['edge']:.3f}  entry=${sig['entry_price']:.3f}  "
            f"ev_post_fee=${sig['ev_per_contract_after_fee']:.3f}  "
            f"{'DRY-RUN' if dry_run else 'opening...'}")
        cluster_count[cluster_key] += 1

        if not dry_run:
            pid = sl.open(
                engine=ENGINE,
                venue="kalshi",
                ticker=ticker,
                side=sig["side"],
                price=sig["entry_price"],
                size=contracts,
                fee_usd=FEE_PER_CONTRACT_ESTIMATE * contracts,
                reason=(
                    f"T1 Phase 2 paper trade. {sig['strike_display']} "
                    f"{sig['which_metric']} {sig['station']} {sig['target_date']}. "
                    f"Ensemble μ={sig['ensemble_mean']}, σ={sig['ensemble_std']}. "
                    f"Our P={sig['our_p']:.2f} vs market {sig['market_p']:.2f}. "
                    f"Edge ${sig['edge']:.3f}."
                ),
                signal_metadata={
                    "station": sig["station"],
                    "target_date": sig["target_date"],
                    "hours_to_close": round(sig["hours_to_close"], 1),
                    "strike_display": sig["strike_display"],
                    "ensemble_mean": sig["ensemble_mean"],
                    "ensemble_std": sig["ensemble_std"],
                    "our_p": sig["our_p"],
                    "market_p": sig["market_p"],
                    "edge": sig["edge"],
                    "kalshi_vol_24h": sig["kalshi_volume_24h"],
                    "kalshi_oi": sig["kalshi_open_interest"],
                },
            )
            opened.append(pid)

    return {"signals": signals, "opened": opened}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=1800, help="daemon poll (default 30 min)")
    ap.add_argument("--min-edge", type=float, default=MIN_EDGE)
    ap.add_argument("--contracts", type=int, default=DEFAULT_CONTRACTS)
    ap.add_argument("--stations", default=",".join(STATION_WHITELIST),
                    help="comma-separated station whitelist (default NYC,LAX)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    stations = [s.strip() for s in args.stations.split(",") if s.strip()]
    log(f"T1 Phase 2 Paper Trader starting. "
        f"dry_run={args.dry_run}  once={args.once}  "
        f"stations={stations}  min_edge={args.min_edge}  "
        f"contracts={args.contracts}  interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            result = run_once(args.dry_run, args.min_edge, args.contracts, stations)
            log(f"loop #{loops}: signals={len(result['signals'])} opened={len(result['opened'])}")
        except Exception as e:
            log(f"[error] run_once: {type(e).__name__}: {e}")
        if args.once or _STOP:
            break
        end = time.time() + args.interval_sec
        while time.time() < end and not _STOP:
            time.sleep(min(1.0, end - time.time()))

    log(f"Paper trader stopped. Loops: {loops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
