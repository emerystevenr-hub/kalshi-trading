"""Terminal 2 — Historical Thesis Backtest (Phase 0).

Validates T5's thesis categories against historical Kalshi resolutions
WITHOUT waiting for shadow positions to settle. Solves the long-dated
validation problem: if we know that "Will X happen by deadline" markets
historically resolve NO at 70%+, calendar_fade has evidence-based edge
before we open a single live shadow position.

Methodology:
  1. Pull recent settled Kalshi markets (status=settled) via /markets.
  2. Heuristically classify each by thesis category from title/structure:
       calendar_fade   — has explicit deadline, "will X by Y" pattern
       tail_probability — long-dated improbable-YES (≥30d at create, low base rate)
       neither         — sports, ordinal, pick-the-winner, etc
  3. Resolution = market.last_price_dollars (post-close = 1.0 if YES won, 0.0 if NO won)
  4. Tabulate hit rate per category (NO hit = fade was right).

Limitations (read these before drawing conclusions):
  - Without historical orderbook, we can't filter to "high YES probability
    at entry," which is T5's actual selection criterion. This backtest
    measures ALL settled markets in each category, not the subset T5 would
    have flagged. Treat the resulting hit rates as a CEILING / sanity check,
    not a precise edge estimate.
  - Sample is recent settled-only — short window, possible regime bias.
  - Classifier is heuristic. Some markets will be miscategorized.

Output:
  ~/Documents/terminal2_data/thesis_backtest.jsonl   — one row per scanned market
  ~/Documents/terminal2_data/thesis_backtest_summary.md — readable report

Usage:
  python3 ~/Documents/terminal2_thesis_backtest.py --max-markets 500
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
DATA_DIR = Path.home() / "Documents" / "terminal2_data"
OUT_JSONL = DATA_DIR / "thesis_backtest.jsonl"
OUT_MD = DATA_DIR / "thesis_backtest_summary.md"
LOG_PATH = DATA_DIR / "thesis_backtest.log"


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


# --------------------------------------------------------------------------
# Heuristic classifier
# --------------------------------------------------------------------------

CALENDAR_FADE_PATTERNS = [
    r"\bbefore\b.*\b\d{4}\b",
    r"\bby\b\s+\w+\s+\d{1,2}",
    r"\bbefore\b\s+\w+\s+\d{1,2}",
]
PICK_WINNER_HINTS = [
    "rank in", "lead the", "win the", "be #1", "be the next",
    "beat the spread", "score the most",
]
SPORTS_HINTS = [
    "nba", "nfl", "nhl", "mlb", "ucl", "fifa", "world cup", "premier league",
    "tennis", "golf", "boxing", "ufc", "soccer",
]


def classify(title: str, market: dict, dtr_at_create_days: Optional[float]) -> str:
    """Return one of: calendar_fade, tail_probability, sports, ordinal, other."""
    if not title:
        return "other"
    t = title.lower()
    if any(h in t for h in SPORTS_HINTS):
        return "sports"
    if any(h in t for h in PICK_WINNER_HINTS):
        return "ordinal"
    is_deadline = any(re.search(p, t, re.IGNORECASE) for p in CALENDAR_FADE_PATTERNS)
    if is_deadline:
        if dtr_at_create_days is not None and dtr_at_create_days > 90:
            return "tail_probability"
        return "calendar_fade"
    if dtr_at_create_days is not None and dtr_at_create_days > 60:
        return "tail_probability"
    return "other"


# --------------------------------------------------------------------------
# Kalshi pull
# --------------------------------------------------------------------------

# Series prefixes T2 actually plays — political, regulatory, economic.
# Avoids the recent-settled sample being dominated by sports parlays.
# Discovered via /events?series_ticker=... pulls; full list is much longer
# but these are representative of T5's actual scope.
T2_RELEVANT_SERIES = [
    "KXTRUMPADMINLEAVE",   # admin departures
    "KXLEAVE",             # generic leaving role (Powell, Starmer, etc.)
    "KXTRUMPCHINA",        # Trump travel
    "KXTRUMPOUT",          # Trump out before X
    "KXFEDCHAIR",          # Fed chair confirmations
    "KXFEDDECISION",       # Fed rate decisions (T3a but useful as fade reference)
    "KXMJSCHEDULE",        # marijuana rescheduling
    "KXFDAAPPROVAL",       # FDA approval markets
    "KXGOVTSHUTDOWN",      # govt shutdown (different from KXGOVTSHUTLENGTH)
    "KXGOVTSHUTLENGTH",    # shutdown duration
    "KXDHSFUND",           # DHS funding
    "KXBALANCEPOWER",      # balance of power 2026/27
    "KXRECSSNBER",         # NBER recession call
    "KXHORMUZNORM",        # Hormuz normalization
    "KXKASHOUT",           # Kash Patel out
    "KXEOWEEK",            # weekly EO count
    "KXMAMDANIMENTION",    # specific public-figure mentions
    "KXBILL",              # legislation passage
    "KXALIENS",            # alien contact (yes really)
    "OAIAGI",              # OpenAI AGI announcement
]


def fetch_settled_markets_by_series(series_list: List[str], max_markets: int) -> List[dict]:
    """Pull settled markets ONLY in T2-relevant series. Avoids sports parlay
    sample bias from a generic /markets?status=settled scan."""
    out: List[dict] = []
    for series in series_list:
        if len(out) >= max_markets:
            break
        cursor: Optional[str] = None
        page_count = 0
        backoff = 1.0
        while len(out) < max_markets and page_count < 20:
            params: Dict[str, object] = {
                "series_ticker": series,
                "status": "settled",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                r = requests.get(
                    f"{KALSHI_BASE}/markets",
                    params=params,
                    timeout=30,
                    proxies={"http": None, "https": None},
                )
                if r.status_code == 429:
                    log(f"  [rate-limit] {series} backing off {backoff:.1f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                r.raise_for_status()
                backoff = 1.0
            except requests.RequestException as e:
                log(f"  [error] {series}: {e}")
                break
            body = r.json()
            new_markets = body.get("markets", []) or []
            out.extend(new_markets)
            cursor = body.get("cursor")
            page_count += 1
            if not cursor or not new_markets:
                break
            time.sleep(0.5)   # 2 req/s polite rate
        log(f"  {series}: cumulative {len(out)} markets")
        time.sleep(0.5)
    return out[:max_markets]


def fetch_settled_markets(max_markets: int) -> List[dict]:
    """Wrapper that selects the series-targeted pull (default) over the
    generic settled-pull (which is biased toward sports parlays)."""
    return fetch_settled_markets_by_series(T2_RELEVANT_SERIES, max_markets)


def days_between(iso_a: str, iso_b: str) -> Optional[float]:
    try:
        a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
        b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
        return (b - a).total_seconds() / 86400.0
    except (ValueError, AttributeError):
        return None


def settled_outcome(market: dict) -> Optional[str]:
    """Return 'YES' / 'NO' / None based on settlement.

    Kalshi's `result` field is 'yes' or 'no' for binary settled markets.
    Fall back to last_price_dollars: 1.0 ⇒ YES, 0.0 ⇒ NO.
    """
    res = (market.get("result") or "").lower()
    if res == "yes":
        return "YES"
    if res == "no":
        return "NO"
    try:
        lp = float(market.get("last_price_dollars") or 0)
    except (TypeError, ValueError):
        return None
    if lp >= 0.95:
        return "YES"
    if lp <= 0.05:
        return "NO"
    return None


# --------------------------------------------------------------------------
# Aggregation + report
# --------------------------------------------------------------------------

def render_md(records: List[dict]) -> str:
    """Build a category-level summary."""
    by_cat: Dict[str, List[dict]] = defaultdict(list)
    for r in records:
        by_cat[r["category"]].append(r)

    lines = [
        "# T2 Thesis Backtest — Phase 0 Validation",
        "",
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}._  ",
        f"_n={len(records)} settled Kalshi markets, recent slice._",
        "",
        "## Resolution by category",
        "",
        "| category | n | YES wins | NO wins | NO hit rate | implication |",
        "|---|---|---|---|---|---|",
    ]
    for cat in ("calendar_fade", "tail_probability", "ordinal", "sports", "other"):
        rs = by_cat.get(cat, [])
        if not rs:
            continue
        yes_n = sum(1 for r in rs if r["outcome"] == "YES")
        no_n = sum(1 for r in rs if r["outcome"] == "NO")
        n_resolved = yes_n + no_n
        rate = (no_n / n_resolved * 100) if n_resolved else 0
        if cat in ("calendar_fade", "tail_probability"):
            if rate >= 70:
                impl = "**Strong fade edge**"
            elif rate >= 55:
                impl = "Modest fade edge"
            elif rate >= 45:
                impl = "Coin flip — no edge"
            else:
                impl = "**FADE THESIS BROKEN**"
        else:
            impl = "—"
        lines.append(
            f"| {cat} | {len(rs)} | {yes_n} | {no_n} | "
            f"{rate:.0f}% | {impl} |"
        )

    lines.append("")
    lines.append("## Read")
    lines.append("")
    lines.append("- **calendar_fade** thesis is "
                 "\"things rarely happen on schedule\" → NO should win >55%.")
    lines.append("- **tail_probability** thesis is "
                 "\"improbable-YES decays toward 0\" → NO should win >65%.")
    lines.append("- This is a CEILING estimate — without historical orderbook we can't")
    lines.append("  filter to \"high YES probability at entry\" (T5's actual criterion).")
    lines.append("  T5's selection is more accurate than this scan; if THIS hit rate is")
    lines.append("  below the band, T5 either has no edge or the heuristic classifier missed it.")
    lines.append("- Sample biases toward recent + actively-settled markets. Re-run periodically.")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=500,
                    help="cap markets pulled (default 500; max practical ~3000)")
    args = ap.parse_args()

    log("=" * 60)
    log(f"T2 Thesis Backtest — pulling up to {args.max_markets} settled markets")
    log("=" * 60)

    markets = fetch_settled_markets(args.max_markets)
    log(f"Fetched {len(markets)} settled markets")

    records: List[dict] = []
    for m in markets:
        outcome = settled_outcome(m)
        if outcome is None:
            continue
        title = m.get("title", "") or ""
        # DTR at creation = days from open_time to close_time (heuristic for
        # "how long was this market live")
        dtr = days_between(m.get("open_time", ""), m.get("close_time", ""))
        cat = classify(title, m, dtr)
        records.append({
            "ticker": m.get("ticker"),
            "title": title[:120],
            "open_time": m.get("open_time"),
            "close_time": m.get("close_time"),
            "dtr_total_days": round(dtr, 1) if dtr is not None else None,
            "result": m.get("result"),
            "last_price_dollars": m.get("last_price_dollars"),
            "outcome": outcome,
            "category": cat,
        })

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSONL, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    log(f"Wrote {len(records)} records → {OUT_JSONL.name}")

    md = render_md(records)
    OUT_MD.write_text(md)
    log(f"Wrote summary → {OUT_MD.name}")

    # Brief stdout summary
    by_cat: Dict[str, List[dict]] = defaultdict(list)
    for r in records:
        by_cat[r["category"]].append(r)
    log("\n" + "=" * 50)
    log("CATEGORY SUMMARY (NO hit rate = fade hit rate)")
    log("=" * 50)
    for cat in ("calendar_fade", "tail_probability", "ordinal", "sports", "other"):
        rs = by_cat.get(cat, [])
        if not rs:
            continue
        yes_n = sum(1 for r in rs if r["outcome"] == "YES")
        no_n = sum(1 for r in rs if r["outcome"] == "NO")
        rate = no_n / (yes_n + no_n) * 100 if (yes_n + no_n) else 0
        log(f"  {cat:<20} n={len(rs):>3}  YES={yes_n:>3}  NO={no_n:>3}  NO_rate={rate:>5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
