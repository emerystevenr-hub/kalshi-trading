"""Terminal 2 — Daily T5 → T2 Picks Pipeline.

Nightly bridge between T5 (kalshi_thesis_factory) and T2 (catalyst book).

Flow:
  1. Run T5 scanner (kalshi_thesis_factory.py) → fresh thesis_candidates_*.json
  2. Read candidates, filter to calendar_fade + DTR ≤ MAX_DTR
  3. Dedup against existing open T2 book by (ticker, correlation_group)
  4. Rank by score, take top N
  5. Write three artifacts:
        ~/Documents/terminal2_data/t5_picks_YYYYMMDD.md       (human report)
        ~/Documents/terminal2_data/t5_picks_latest.md         (convenience)
        ~/Documents/terminal2_data/t2_deploy_YYYYMMDD.py      (ready-to-run)
        ~/Documents/terminal2_data/t2_deploy_latest.py        (convenience)
  6. Fire macOS notification with pick count

User workflow each morning:
  cat ~/Documents/terminal2_data/t5_picks_latest.md
  # If picks look good:
  python3 ~/Documents/terminal2_data/t2_deploy_latest.py --apply
  # If not, just delete the deploy script. Tomorrow brings fresh picks.

Scheduled via:
  ~/Library/LaunchAgents/com.t5.daily-picks.plist  (23:00 PT nightly)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


HOME = Path.home()
DOCS = HOME / "Documents"
DATA_DIR = DOCS / "terminal2_data"
LEDGER = DOCS / "shadow_pnl" / "ledger.jsonl"
T5_SCRIPT = DOCS / "kalshi_thesis_factory.py"
LATEST_CANDIDATES = DOCS / "thesis_candidates_latest.json"
LOG_PATH = DATA_DIR / "daily_picks.log"

PY = "/opt/homebrew/bin/python3"

# Selection criteria
MAX_DTR_DAYS = 60
TOP_N_PICKS = 3
DISPLAY_TOP_N = 5
ENGINE = "T2"
DEFAULT_SIZE_USD = 50.0     # Tighter than T5's $75 default — see KXVOTEHUBTRUMPUPDOWN lesson


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


def run_t5_scan(skip_scan: bool) -> bool:
    """Invoke kalshi_thesis_factory.py. Returns True if successful or
    skipped. The scan typically takes 5-15 minutes."""
    if skip_scan:
        log("--skip-scan set; using existing thesis_candidates_latest.json")
        return LATEST_CANDIDATES.exists()
    log("Running T5 scanner (kalshi_thesis_factory.py)...")
    try:
        result = subprocess.run(
            [PY, str(T5_SCRIPT)],
            cwd=str(DOCS),
            timeout=1800,  # 30 min hard cap
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log(f"  [error] T5 scanner exit={result.returncode}")
            log(f"  stderr: {result.stderr[:500]}")
            return False
        log(f"  T5 scanner ok ({len(result.stdout.splitlines())} lines)")
        return True
    except subprocess.TimeoutExpired:
        log("  [error] T5 scanner timed out after 30 min")
        return False
    except Exception as e:
        log(f"  [error] T5 invoke failed: {type(e).__name__}: {e}")
        return False


def load_candidates() -> List[dict]:
    if not LATEST_CANDIDATES.exists():
        log(f"[error] {LATEST_CANDIDATES} not found")
        return []
    try:
        with open(LATEST_CANDIDATES) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"[error] candidates load: {e}")
        return []
    return data.get("thesis_candidates", []) or []


def _load_thesis_archive() -> Dict[str, dict]:
    """Build {ticker: {correlation_group, ...}} from all historical
    thesis_candidates archives. Used as fallback for positions whose
    signal_metadata didn't preserve correlation_group at open time."""
    out: Dict[str, dict] = {}
    for path in DOCS.glob("thesis_candidates_*.json"):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for c in data.get("thesis_candidates", []) or []:
            cg = c.get("correlation_group")
            for tk in c.get("target_ticker_prefixes", []) or []:
                if cg and tk not in out:
                    out[tk] = {"correlation_group": cg}
    return out


def _ticker_event_prefix(ticker: str) -> str:
    """Approximate event_ticker for a market ticker by stripping the
    trailing strike suffix. Robust to most Kalshi naming conventions:
        KXTRUMPCHINA-26-MAY15           → KXTRUMPCHINA-26-MAY15  (no strike)
        KXKASHOUT-26APR-JUN01           → KXKASHOUT-26APR-JUN01
        KXCPIYOY-26APR-T3.5             → KXCPIYOY-26APR
        KXGOVTSHUTLENGTH-26FEB07-G100   → KXGOVTSHUTLENGTH-26FEB07
    Strategy: drop trailing segment if it starts with a known strike code
    (T, B, G, A) followed by digits."""
    parts = ticker.split("-")
    if len(parts) >= 2:
        last = parts[-1]
        if last and last[0].upper() in ("T", "B", "G", "A") and any(c.isdigit() for c in last[1:]):
            return "-".join(parts[:-1])
    return ticker


def load_open_t2_universe() -> Tuple[Set[str], Set[str]]:
    """Return (open_tickers, open_correlation_groups) for current T2 book.

    Uses three sources to populate correlation_groups:
      1. signal_metadata.correlation_group (modern positions)
      2. thesis_candidates archive lookup by ticker (legacy positions)
      3. ticker event-prefix heuristic as last resort
    """
    open_tickers: Set[str] = set()
    open_groups: Set[str] = set()
    if not LEDGER.exists():
        return open_tickers, open_groups
    open_map = {}
    annulled: Dict[str, set] = {}
    try:
        with open(LEDGER) as f:
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
                    open_map[pid] = r
                elif t == "close":
                    if r.get("ts") not in annulled.get(pid, set()):
                        open_map.pop(pid, None)
                elif t == "annul_close":
                    annulled.setdefault(pid, set()).add(r.get("annulled_close_ts"))
    except OSError:
        pass

    archive = _load_thesis_archive()
    for r in open_map.values():
        ticker = r.get("ticker")
        if not ticker:
            continue
        open_tickers.add(ticker)
        md = r.get("signal_metadata") or {}
        cg = md.get("correlation_group")
        if not cg:
            cg = (archive.get(ticker, {}) or {}).get("correlation_group")
        if not cg:
            cg = _ticker_event_prefix(ticker)
        open_groups.add(cg)
    return open_tickers, open_groups


def _load_blocked_archetypes() -> Set[str]:
    """Per-archetype kill switch driven by settled-outcome calibration.

    Imports t2_archetype_calibration on the fly so this module remains
    runnable even before the calibration script lands. Empty set ==
    no-op (everything passes archetype filter)."""
    try:
        sys.path.insert(0, str(DOCS))
        from t2_archetype_calibration import blocked_archetypes  # noqa
        blocked = blocked_archetypes()
        if blocked:
            log(f"  [archetype-cal] blocked: {sorted(blocked)}")
        else:
            log(f"  [archetype-cal] no archetypes blocked yet")
        return blocked
    except Exception as e:
        log(f"  [warn] archetype calibration load failed: {e}; allowing all")
        return set()


def filter_and_rank(candidates: List[dict], open_tickers: Set[str],
                    open_groups: Set[str]) -> Tuple[List[dict], List[dict]]:
    """Return (top_picks, all_filtered) sorted by score desc.

    2026-05-03: replaced the calendar_fade hardcode with a calibration-
    driven archetype filter. All sub_engines pass UNLESS they're in the
    blocked set (settled n ≥ 3 AND win_rate < 30%). This makes the
    pipeline self-correcting from settled outcomes.
    """
    blocked = _load_blocked_archetypes()
    eligible = []
    blocked_count = 0
    for c in candidates:
        sub = (c.get("sub_engine") or "").lower()
        if sub in blocked:
            blocked_count += 1
            continue
        dtr = c.get("dtr_days") or 999
        if dtr > MAX_DTR_DAYS:
            continue
        tickers = c.get("target_ticker_prefixes") or []
        if not tickers:
            continue
        ticker = tickers[0]
        cg = c.get("correlation_group", "")
        c_view = dict(c)
        c_view["_first_ticker"] = ticker
        c_view["_in_book_ticker"] = ticker in open_tickers
        c_view["_in_book_group"] = cg in open_groups
        c_view["_dedup_block"] = c_view["_in_book_ticker"] or c_view["_in_book_group"]
        eligible.append(c_view)
    if blocked_count:
        log(f"  filtered {blocked_count} candidate(s) on blocked archetypes")
    eligible.sort(key=lambda x: -float(x.get("score", 0)))
    fresh = [c for c in eligible if not c["_dedup_block"]]
    return fresh[:TOP_N_PICKS], eligible[:DISPLAY_TOP_N]


def render_markdown(top_picks: List[dict], display: List[dict],
                    open_tickers: Set[str], open_groups: Set[str]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# T2 Daily Picks — {today}",
        "",
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}._  ",
        f"_Filter: calendar_fade only, DTR ≤ {MAX_DTR_DAYS}d, dedup vs open T2 book._",
        f"_Open T2 positions: {len(open_tickers)} tickers across {len(open_groups)} correlation groups._",
        "",
        f"## Top {len(top_picks)} fresh picks (recommended for opening today)",
        "",
    ]
    if not top_picks:
        lines.append("**No fresh picks today.** All eligible candidates either already in book or in held correlation groups.")
        lines.append("")
    else:
        for i, c in enumerate(top_picks, 1):
            tk = c["_first_ticker"]
            score = c.get("score", 0)
            dtr = c.get("dtr_days", 0)
            edge = c.get("edge_dollars", 0)
            our_p = c.get("our_probability", 0) * 100
            implied = c.get("implied_yes_prob", 0) * 100
            entry = c.get("max_entry_price", 0)
            target = c.get("target_price", 0)
            stop = c.get("stop_price", 0)
            cg = c.get("correlation_group", "?")
            title = c.get("market_title") or c.get("calendar_event") or "?"
            lines.append(f"### {i}. `{tk}`")
            lines.append("")
            lines.append(f"**{title[:160]}**")
            lines.append("")
            lines.append(f"- side: **NO** @ entry ≤ ${entry:.3f}")
            lines.append(f"- target: ${target:.2f}   stop: ${stop:.2f}")
            lines.append(f"- our_probability: {our_p:.0f}%   (implied YES: {implied:.0f}%)")
            lines.append(f"- edge: ${edge:.3f}   DTR: {dtr:.0f}d   score: {score:.3f}")
            lines.append(f"- correlation_group: `{cg}`")
            lines.append(f"- recommended size: ${DEFAULT_SIZE_USD:.0f}")
            lines.append("")
    if display:
        lines.append("## Full top-5 (including blocked / dedup'd)")
        lines.append("")
        lines.append("| # | Ticker | DTR | Edge | Score | Status |")
        lines.append("|---|---|---|---|---|---|")
        for i, c in enumerate(display, 1):
            tk = c["_first_ticker"]
            dtr = c.get("dtr_days", 0)
            edge = c.get("edge_dollars", 0)
            score = c.get("score", 0)
            if c["_in_book_ticker"]:
                status = "in book"
            elif c["_in_book_group"]:
                status = "group held"
            else:
                status = "**FRESH**"
            lines.append(f"| {i} | `{tk}` | {dtr:.0f}d | ${edge:.2f} | {score:.3f} | {status} |")
        lines.append("")
    lines.append("## Action")
    lines.append("")
    if top_picks:
        lines.append("If you want to open the recommended picks:")
        lines.append("")
        lines.append("```bash")
        deploy_path = DATA_DIR / f"t2_deploy_{datetime.now(timezone.utc).strftime('%Y%m%d')}.py"
        lines.append(f"# Dry-run preview:")
        lines.append(f"{PY} {deploy_path}")
        lines.append("")
        lines.append(f"# If preview looks right, commit:")
        lines.append(f"{PY} {deploy_path} --apply")
        lines.append("```")
    else:
        lines.append("_No new picks to open. Check back tomorrow._")
    lines.append("")
    lines.append("---")
    lines.append("_Generated by `terminal2_daily_t5_picks.py`._")
    return "\n".join(lines) + "\n"


def render_deploy_script(top_picks: List[dict]) -> str:
    """Emit a ready-to-run deploy script that opens the top picks."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plan_repr = []
    for c in top_picks:
        tk = c["_first_ticker"]
        max_entry = float(c.get("max_entry_price", 0))
        target = float(c.get("target_price", 0))
        stop = float(c.get("stop_price", 0))
        our_p = float(c.get("our_probability", 0))
        cg = c.get("correlation_group", "")
        title = c.get("market_title") or c.get("calendar_event") or ""
        # Size in contracts: DEFAULT_SIZE_USD / max_entry, rounded
        size = max(1, int(DEFAULT_SIZE_USD / max_entry)) if max_entry > 0 else 25
        plan_repr.append(
            f"    {{\n"
            f"        'ticker': {tk!r},\n"
            f"        'side': 'NO',\n"
            f"        'max_entry': {max_entry},\n"
            f"        'size': {size},\n"
            f"        'target_price': {target},\n"
            f"        'stop_price': {stop},\n"
            f"        'our_probability': {our_p},\n"
            f"        'correlation_group': {cg!r},\n"
            f"        'thesis': {(title[:200])!r},\n"
            f"    }},"
        )
    plan_str = "\n".join(plan_repr)
    return f'''"""T2 daily deploy — generated {today} by terminal2_daily_t5_picks.

Auto-emitted from T5 top-{len(top_picks)} short-DTR calendar_fade candidates,
deduped against open T2 book at scan time.

Usage:
    # Preview (default):
    {PY} {DATA_DIR}/t2_deploy_{today.replace('-', '')}.py

    # Commit:
    {PY} {DATA_DIR}/t2_deploy_{today.replace('-', '')}.py --apply
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "Documents"))
from terminal2_reshadow import open_position, ShadowLedger  # noqa: E402

OPEN_PLAN = [
{plan_str}
]

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dry_run = not args.apply
    sl = ShadowLedger() if not dry_run else None
    print("=" * 80)
    print("T2 DAILY DEPLOY — generated {today}")
    print(f"Mode: {{'LIVE WRITE' if not dry_run else 'DRY RUN (default)'}}")
    print("=" * 80)
    for plan in OPEN_PLAN:
        # Add the sub_engine + source so reconciler / mark_drift recognize the trade
        plan["sub_engine"] = "calendar_fade"
        open_position(plan, dry_run, sl)
    if dry_run:
        print("\\nDRY RUN — re-run with --apply to commit.")
'''


def macos_notify(title: str, body: str) -> None:
    """Fire a macOS Notification Center alert."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}" sound name "Glass"'],
            timeout=5,
            check=False,
        )
    except Exception as e:
        log(f"  [warn] notification failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-scan", action="store_true",
                    help="skip running T5; use existing thesis_candidates_latest.json")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log("=" * 70)
    log("T2 DAILY T5 PICKS PIPELINE")
    log("=" * 70)

    if not run_t5_scan(args.skip_scan):
        log("Aborting — no candidates available.")
        macos_notify("T2 Picks", "T5 scan failed — see log")
        return 1

    candidates = load_candidates()
    log(f"Loaded {len(candidates)} candidates from latest scan")

    open_tickers, open_groups = load_open_t2_universe()
    log(f"Open T2 book: {len(open_tickers)} tickers, "
        f"{len(open_groups)} correlation groups")

    top_picks, display = filter_and_rank(candidates, open_tickers, open_groups)
    # Count true eligible from full filtered list, not display window (was causing
    # the misleading "Top 3 fresh picks from 0 eligible" output 2026-04-30).
    eligible_count = len([c for c in display if not c.get("_dedup_block")])
    blocked_in_display = len([c for c in display if c.get("_dedup_block")])
    log(f"Top {len(top_picks)} fresh picks selected. "
        f"display window: {eligible_count} eligible / {blocked_in_display} blocked-by-dedup")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_compact = today.replace("-", "")

    md = render_markdown(top_picks, display, open_tickers, open_groups)
    md_dated = DATA_DIR / f"t5_picks_{today_compact}.md"
    md_latest = DATA_DIR / "t5_picks_latest.md"
    md_dated.write_text(md)
    md_latest.write_text(md)
    log(f"Wrote {md_dated.name} and t5_picks_latest.md")

    if top_picks:
        deploy_src = render_deploy_script(top_picks)
        deploy_dated = DATA_DIR / f"t2_deploy_{today_compact}.py"
        deploy_latest = DATA_DIR / "t2_deploy_latest.py"
        deploy_dated.write_text(deploy_src)
        deploy_latest.write_text(deploy_src)
        os.chmod(deploy_dated, 0o755)
        os.chmod(deploy_latest, 0o755)
        log(f"Wrote {deploy_dated.name} and t2_deploy_latest.py")
        macos_notify(
            "T2 Daily Picks",
            f"{len(top_picks)} new candidates ready for review",
        )
    else:
        macos_notify(
            "T2 Daily Picks",
            "No new picks today — all candidates blocked by dedup",
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
