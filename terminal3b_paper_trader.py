"""Terminal 3b — CPI Nowcast Paper Trader.

Reads:
  - terminal3b_data/kalshi_KXCPIYOY-{event}_{date}.jsonl   (snapshots)
  - terminal3b_data/nowcast_cleveland_fed.jsonl             (latest μ per target)
  - terminal3b_data/empirical_sigma.json                    (σ by lead bucket)

Generates signals against open KXCPIYOY markets, applying every filter from
spec v0.3 (§5): lead-time edge scaling, YES-side guardrail, volume floor,
cluster cap, divergence flag, stale-nowcast safety.

Opens shadow positions in ~/Documents/shadow_pnl/ledger.jsonl tagged
engine="T3b".

NO EXECUTION. Paper trading only.

Usage:
    python3 ~/Documents/terminal3b_paper_trader.py --dry-run --once
    python3 ~/Documents/terminal3b_paper_trader.py --once
    nohup caffeinate -is python3 ~/Documents/terminal3b_paper_trader.py \\
        --interval-sec 1800 > /dev/null 2>&1 &
"""

import argparse
import json
import math
import signal
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path.home() / "Documents"))
from shadow_pnl_core import ShadowLedger  # noqa: E402


DATA_DIR = Path.home() / "Documents" / "terminal3b_data"
NOWCAST_PATH = DATA_DIR / "nowcast_cleveland_fed.jsonl"
SIGMA_PATH = DATA_DIR / "empirical_sigma.json"
LOG_PATH = DATA_DIR / "paper_trader.log"

ENGINE = "T3b"
SERIES_TICKER_PREFIX = "KXCPIYOY"
SCHEMA_VERSION = "v1"

# Spec §5 constants
NOWCAST_STALENESS_HOURS = 48
DIVERGENCE_THRESHOLD = 0.30        # Atlanta Sticky vs Cleveland Fed
YES_BAND_HALF_WIDTH = 0.30         # ±0.3% around μ
YES_BID_MIN = 0.10
YES_BID_MAX = 0.85
CLUSTER_CAP = 7
DEFAULT_CONTRACTS = 25
SHORT_LEAD_CONTRACTS = 12
FEE_PER_CONTRACT = 0.02
BLS_RELEASE_DAY_OF_MONTH = 13      # canonical mid-month

# Lead window table (days_to_release)
def lead_band(dtr: int) -> Tuple[Optional[str], float, int]:
    """Return (band_name, min_edge, contracts). band_name=None means no trade."""
    if dtr > 7:
        return (None, 0.0, 0)
    if 4 <= dtr <= 7:
        return ("4to7", 0.20, SHORT_LEAD_CONTRACTS)
    if 1 <= dtr <= 3:
        return ("1to3", 0.10, DEFAULT_CONTRACTS)
    return (None, 0.0, 0)  # <24h or negative


_STOP = False


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


def _handle_sigint(sig, frame):
    global _STOP
    _STOP = True
    log("SIGINT — finishing iteration.")


# --------------------------------------------------------------------------
# σ + nowcast loaders
# --------------------------------------------------------------------------

def load_sigma_table() -> dict:
    if not SIGMA_PATH.exists():
        return {}
    try:
        with open(SIGMA_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"  [error] sigma table load: {e}")
        return {}


def resolve_sigma(table: dict, dtr: int) -> Optional[float]:
    """Look up σ for given days_to_release. Returns the per-bucket std_resid."""
    if not table:
        return None
    buckets = table.get("by_lead_bucket") or {}
    # Mirror spec lead bucket boundaries
    if dtr <= 3:
        key = "le3"
    elif dtr <= 7:
        key = "4to7"
    elif dtr <= 14:
        key = "8to14"
    else:
        key = "gt14"
    cell = buckets.get(key) or {}
    if cell.get("n", 0) >= table.get("min_n_per_bucket", 10):
        return float(cell["std_resid"])
    glb = table.get("global") or {}
    if glb.get("n", 0) >= table.get("min_n_per_bucket", 10):
        return float(glb["std_resid"])
    return None


def load_latest_nowcasts() -> Dict[Tuple[int, int], dict]:
    """Build {(target_year, target_month): latest record} from nowcast JSONL."""
    out: Dict[Tuple[int, int], dict] = {}
    if not NOWCAST_PATH.exists():
        return out
    try:
        with open(NOWCAST_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = (r.get("target_year"), r.get("target_month"))
                if k[0] is None or k[1] is None:
                    continue
                cur = out.get(k)
                if cur is None or r.get("publish_date", "") > cur.get("publish_date", ""):
                    out[k] = r
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# Kalshi snapshot loader
# --------------------------------------------------------------------------

def load_latest_market_snaps_for_event(event_ticker: str) -> Dict[str, dict]:
    """Read both today's and yesterday's snap files; return latest snap per
    ticker (by ts_utc)."""
    now = datetime.now(timezone.utc)
    latest: Dict[str, dict] = {}
    for off in (0, 1):
        d = (now - timedelta(days=off)).strftime("%Y-%m-%d")
        p = DATA_DIR / f"kalshi_{event_ticker}_{d}.jsonl"
        if not p.exists():
            continue
        try:
            with open(p) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = r.get("ticker")
                    if not t:
                        continue
                    cur = latest.get(t)
                    if cur is None or r.get("ts_utc", "") > cur.get("ts_utc", ""):
                        latest[t] = r
        except OSError:
            continue
    return latest


def discover_open_events_from_snaps() -> List[str]:
    """Find KXCPIYOY events we have any local snapshot for."""
    seen = set()
    if not DATA_DIR.exists():
        return []
    for p in DATA_DIR.glob(f"kalshi_{SERIES_TICKER_PREFIX}-*_*.jsonl"):
        # filename: kalshi_{event}_{date}.jsonl
        try:
            stem = p.stem
            after = stem[len("kalshi_"):]
            event = after.rsplit("_", 1)[0]   # event_ticker before final _date
            seen.add(event)
        except Exception:
            continue
    return sorted(seen)


# --------------------------------------------------------------------------
# Probability + filters
# --------------------------------------------------------------------------

def _phi(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


def yes_prob_above(strike: float, mu: float, sigma: float) -> float:
    """For 'Above X%' market: P(CPI YoY > X%)."""
    if sigma <= 0:
        return 0.0
    return 1.0 - _phi((strike - mu) / sigma)


def parse_target_from_event_ticker(event_ticker: str) -> Optional[Tuple[int, int]]:
    """KXCPIYOY-26APR → (2026, 4). Three-letter month code."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    code = parts[-1].upper()  # "26APR"
    if len(code) != 5 or not code[:2].isdigit():
        return None
    yy = int(code[:2])
    mon_map = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    m = mon_map.get(code[2:])
    if not m:
        return None
    return (2000 + yy, m)


def bls_release_date(target_year: int, target_month: int) -> date:
    rel_y = target_year + (1 if target_month == 12 else 0)
    rel_m = 1 if target_month == 12 else target_month + 1
    return date(rel_y, rel_m, BLS_RELEASE_DAY_OF_MONTH)


def days_to_release(target_year: int, target_month: int) -> int:
    today = datetime.now(timezone.utc).date()
    return (bls_release_date(target_year, target_month) - today).days


def is_nowcast_stale(now_rec: dict) -> bool:
    pub = now_rec.get("publish_date")
    if not pub:
        return True
    try:
        pub_dt = datetime.fromisoformat(pub).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age_h = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600.0
    return age_h > NOWCAST_STALENESS_HOURS


def already_open_t3b(ticker: str) -> bool:
    p = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"
    if not p.exists():
        return False
    open_ids = {}
    try:
        with open(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("engine") != ENGINE:
                    continue
                t = r.get("type")
                if t == "open" and r.get("ticker") == ticker:
                    open_ids[r["position_id"]] = r
                elif t == "close" and r.get("position_id") in open_ids:
                    open_ids.pop(r["position_id"], None)
    except OSError:
        return False
    return len(open_ids) > 0


def cluster_count_t3b(event_ticker: str) -> int:
    p = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"
    if not p.exists():
        return 0
    opens = {}
    try:
        with open(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("engine") != ENGINE:
                    continue
                t = r.get("type")
                pid = r.get("position_id")
                if t == "open":
                    md = r.get("signal_metadata") or {}
                    if md.get("event_ticker") == event_ticker:
                        opens[pid] = r
                elif t == "close" and pid in opens:
                    opens.pop(pid, None)
    except OSError:
        return 0
    return len(opens)


# --------------------------------------------------------------------------
# Signal generation
# --------------------------------------------------------------------------

def evaluate_market(market: dict, mu: float, sigma: float, dtr: int,
                    event_ticker: str) -> Tuple[str, dict]:
    """Return (reason_code, signal_dict_or_detail).

    reason_code is one of (per spec §13):
      accept_yes, accept_no, reject_edge_below_min, reject_lead_too_long,
      reject_lead_too_short, reject_yes_outside_band, reject_yes_bid_low,
      reject_yes_bid_high, reject_volume_low
    """
    band_name, min_edge, contracts = lead_band(dtr)
    if band_name is None:
        return ("reject_lead_too_long" if dtr > 7 else "reject_lead_too_short", {"dtr": dtr})

    # Strike from market
    if market.get("strike_type") != "greater":
        return ("reject_unsupported_strike_type", {"strike_type": market.get("strike_type")})
    strike = market.get("floor_strike")
    if strike is None:
        return ("reject_no_strike", {})
    strike = float(strike)

    yes_bid = market.get("yes_bid")
    yes_ask = market.get("yes_ask")
    if yes_bid is None or yes_ask is None:
        return ("reject_no_bbo", {})

    our_p = yes_prob_above(strike, mu, sigma)
    market_p = (yes_bid + yes_ask) / 2.0

    if our_p > market_p:
        side = "YES"
        entry = yes_ask
        edge = our_p - market_p
    else:
        side = "NO"
        entry = 1.0 - yes_bid
        edge = market_p - our_p

    # Volume filter (lead-scaled)
    vol_24h = float(market.get("volume_24h") or 0)
    oi = float(market.get("open_interest") or 0)
    if band_name == "4to7":
        if not (vol_24h >= 50 or oi >= 500):
            return ("reject_volume_low", {"vol_24h": vol_24h, "oi": oi, "band": band_name})
    elif band_name == "1to3":
        if not (vol_24h >= 200 and oi >= 500):
            return ("reject_volume_low", {"vol_24h": vol_24h, "oi": oi, "band": band_name})

    # YES guardrail (asymmetric)
    if side == "YES":
        if abs(strike - mu) > YES_BAND_HALF_WIDTH:
            return ("reject_yes_outside_band", {"strike": strike, "mu": mu})
        if yes_bid < YES_BID_MIN:
            return ("reject_yes_bid_low", {"yes_bid": yes_bid})
        if yes_bid > YES_BID_MAX:
            return ("reject_yes_bid_high", {"yes_bid": yes_bid})
        # Tighter edge bar on YES
        if edge < min_edge + 0.05:
            return ("reject_edge_below_min", {"edge": edge, "min": min_edge + 0.05, "side": side})
    else:
        if edge < min_edge:
            return ("reject_edge_below_min", {"edge": edge, "min": min_edge, "side": side})

    # Made it through → accept
    accept_code = "accept_yes" if side == "YES" else "accept_no"
    return (accept_code, {
        "ticker": market["ticker"],
        "event_ticker": event_ticker,
        "strike": strike,
        "side": side,
        "entry_price": entry,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "our_p": round(our_p, 4),
        "market_p": round(market_p, 4),
        "edge": round(edge, 4),
        "contracts": contracts,
        "mu": mu,
        "sigma": sigma,
        "days_to_release": dtr,
        "lead_band": band_name,
        "vol_24h": vol_24h,
        "oi": oi,
    })


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def run_once(dry_run: bool) -> dict:
    # Freshness gate (added 2026-05-08 after T1's silent 7-day actuals starvation
    # — same pattern that bit T3b for 12 days in late April). If watchdog flag
    # is up, force dry-run. Auto-clears when data recovers.
    _flag = Path.home() / "Documents" / "freshness_alarm.flag"
    if _flag.exists() and not dry_run:
        try:
            log(f"[freshness_alarm] flag present at {_flag} — forcing dry-run for this cycle")
            log(f"[freshness_alarm] {_flag.read_text().strip()}")
        except OSError:
            log(f"[freshness_alarm] flag present at {_flag} — forcing dry-run for this cycle")
        dry_run = True

    sigma_table = load_sigma_table()
    if not sigma_table:
        log("[fatal] empirical_sigma.json missing — run terminal3b_nowcast_backfill.py first.")
        return {"opened": 0, "rejected": 0}

    nowcasts = load_latest_nowcasts()
    if not nowcasts:
        log("[fatal] no nowcasts loaded — run terminal3b_nowcast_puller.py first.")
        return {"opened": 0, "rejected": 0}

    events = discover_open_events_from_snaps()
    if not events:
        log("no Kalshi snapshots found — run terminal3b_kalshi_logger.py first.")
        return {"opened": 0, "rejected": 0}

    sl = ShadowLedger() if not dry_run else None
    accepted: List[dict] = []
    reject_counts: Dict[str, int] = defaultdict(int)
    opened = 0

    for event in events:
        target = parse_target_from_event_ticker(event)
        if not target:
            continue
        ty, tm = target

        # Skip if event is past or too far out
        dtr = days_to_release(ty, tm)
        if dtr > 7:
            log(f"  [{event}] skip — {dtr} days to release (>7 cutoff)")
            continue
        if dtr < 0:
            log(f"  [{event}] skip — release was {-dtr} days ago")
            continue

        # Nowcast lookup + staleness check
        now_rec = nowcasts.get(target)
        if now_rec is None:
            log(f"  [{event}] skip — no nowcast for {ty}-{tm}")
            continue
        if is_nowcast_stale(now_rec):
            log(f"  [{event}] skip — nowcast >{NOWCAST_STALENESS_HOURS}h stale "
                f"(publish={now_rec.get('publish_date')})")
            continue

        mu = float(now_rec["cpi_yoy"])
        sigma = resolve_sigma(sigma_table, dtr)
        if sigma is None:
            log(f"  [{event}] skip — no σ available for dtr={dtr}")
            continue

        # TODO Phase 1 follow-up: Atlanta Fed Sticky CPI divergence check.
        # Placeholder: divergence_flag = False until terminal3b_atlanta_sticky_puller is built.
        divergence_flag = False
        if divergence_flag:
            log(f"  [{event}] skip — Atlanta vs Cleveland divergence >0.30%")
            continue

        snaps = load_latest_market_snaps_for_event(event)
        if not snaps:
            log(f"  [{event}] no market snapshots loaded")
            continue

        log(f"  [{event}] target={ty}-{tm:02d}  μ={mu:.3f}%  σ={sigma:.3f}%  "
            f"dtr={dtr}  markets={len(snaps)}")

        for ticker, market in snaps.items():
            if already_open_t3b(ticker):
                reject_counts["reject_already_open"] += 1
                continue
            code, detail = evaluate_market(market, mu, sigma, dtr, event)
            if code.startswith("accept"):
                accepted.append(detail)
            else:
                reject_counts[code] += 1
                log(f"    [{code}] {ticker}  {detail}")

    # Sort accepted by edge, apply cluster cap per event
    accepted.sort(key=lambda s: -s["edge"])
    cluster_seen = defaultdict(int)
    # Seed cluster_seen with EXISTING T3b open positions per event
    for s in accepted:
        cluster_seen[s["event_ticker"]] = cluster_count_t3b(s["event_ticker"])
        # Compute once per event by querying ledger; loop body only adds in-memory
        break
    # (Single-pass populate: re-query for any other event_tickers not yet seeded)
    seeded = set()
    for s in accepted:
        et = s["event_ticker"]
        if et in seeded:
            continue
        cluster_seen[et] = cluster_count_t3b(et)
        seeded.add(et)

    for s in accepted:
        et = s["event_ticker"]
        if cluster_seen[et] >= CLUSTER_CAP:
            log(f"    [reject_cluster_cap] {s['ticker']}  "
                f"(event {et} already at {CLUSTER_CAP} positions)")
            reject_counts["reject_cluster_cap"] += 1
            continue
        # Entropy collapse defensive gate (added 2026-05-09).
        # If a recent watch alert exists on this ticker AND our proposed side
        # would fade the informed flow direction, refuse. Phase 1 = block only.
        try:
            from entropy_alert_helpers import should_block_for_collapse
            blocked, reason = should_block_for_collapse(
                ticker=s["ticker"], proposed_side=s["side"], engine=ENGINE,
            )
            if blocked:
                log(f"    [entropy-block] {s['ticker']} {reason}")
                reject_counts["entropy_collapse_block"] += 1
                continue
        except Exception as e:
            log(f"    [warn] entropy gate failed open: {e}")
        cluster_seen[et] += 1
        log(f"    [FIRE] {s['ticker']:<28} {s['side']:<3} "
            f"strike={s['strike']:<5}  μ={s['mu']:.2f}%  σ={s['sigma']:.3f}%  "
            f"our_p={s['our_p']:.3f}  mkt_p={s['market_p']:.3f}  "
            f"edge=${s['edge']:.3f}  entry=${s['entry_price']:.3f}  "
            f"contracts={s['contracts']}  dtr={s['days_to_release']}d  "
            f"{'DRY-RUN' if dry_run else 'opening...'}")
        if not dry_run:
            sl.open(
                engine=ENGINE,
                venue="kalshi",
                ticker=s["ticker"],
                side=s["side"],
                price=s["entry_price"],
                size=s["contracts"],
                fee_usd=FEE_PER_CONTRACT * s["contracts"],
                reason=(
                    f"T3b CPI nowcast paper trade. "
                    f"strike >{s['strike']:.1f}%  μ={s['mu']:.2f}%  σ={s['sigma']:.3f}%. "
                    f"Our P={s['our_p']:.3f} vs market {s['market_p']:.3f}. "
                    f"Edge ${s['edge']:.3f}. Lead {s['days_to_release']}d."
                ),
                signal_metadata={
                    "event_ticker": s["event_ticker"],
                    "ticker": s["ticker"],
                    "strike": s["strike"],
                    "side": s["side"],
                    "our_p": s["our_p"],
                    "market_p": s["market_p"],
                    "edge": s["edge"],
                    "mu": s["mu"],
                    "sigma": s["sigma"],
                    "days_to_release": s["days_to_release"],
                    "lead_band": s["lead_band"],
                    "vol_24h": s["vol_24h"],
                    "oi": s["oi"],
                },
            )
            opened += 1

    # Summary
    log(f"summary: accepted={len(accepted)}  opened={opened}  "
        + " ".join(f"{k}={v}" for k, v in sorted(reject_counts.items())))
    return {"opened": opened, "rejected": sum(reject_counts.values())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=1800,
                    help="daemon poll interval (default 30 min)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    log(f"T3b CPI Paper Trader starting. dry_run={args.dry_run} once={args.once} "
        f"interval={args.interval_sec}s")

    loops = 0
    while True:
        loops += 1
        try:
            run_once(args.dry_run)
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
