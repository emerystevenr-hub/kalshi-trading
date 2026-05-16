"""Terminal 1 — Daily Calibration Trend Tracker.

Idempotent: regenerates the full trend table from the ledger on every run.
Safe to run hourly, daily, or manually; output is deterministic for the
same ledger state.

Reads:
  ~/Documents/shadow_pnl/ledger.jsonl
  ~/Documents/terminal1_phase2_paper_trader.log   (regime cutoff detection)

Writes (regenerated each run):
  ~/Documents/calibration_trend.jsonl   — one record per UTC settle date
  ~/Documents/calibration_trend.md      — human-readable table + day-7 verdict

Schedule (launchd):
  Drop ~/Library/LaunchAgents/com.terminal1.calibration-trend.plist and:
    launchctl load ~/Library/LaunchAgents/com.terminal1.calibration-trend.plist

Usage (manual):
  python3 ~/Documents/terminal1_calibration_trend.py
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path.home() / "Documents"))
from t1_settlement_analysis import _model_win_prob, load_settled_positions  # noqa: E402


PAPER_LOG = Path.home() / "Documents" / "terminal1_phase2_paper_trader.log"
TREND_JSONL = Path.home() / "Documents" / "calibration_trend.jsonl"
TREND_MD = Path.home() / "Documents" / "calibration_trend.md"

# Day-7 decision gate (locked in 2026-04-26 post-settlement review)
TARGET_NEW_N = 30
TARGET_CAL_YES_LO = 0.7
TARGET_CAL_YES_HI = 1.3
KILL_CAL_YES = 0.3
KILL_PNL_PCT = -0.10  # -10% on cost

# Fallback cutoff if paper_trader log lacks a recent "starting" line.
FALLBACK_CUTOFF = datetime(2026, 4, 26, 16, 30, tzinfo=timezone.utc)


def find_new_regime_cutoff() -> datetime:
    """Most recent paper_trader 'Paper Trader starting' line — that marks
    when the new config (empirical σ, min_edge=0.20, ATL HIGH paused) took
    over. Pre-cutoff opens are 'old regime' even if they close after."""
    if not PAPER_LOG.exists():
        return FALLBACK_CUTOFF
    cutoff = None
    try:
        with open(PAPER_LOG) as f:
            for line in f:
                if "Paper Trader starting" not in line:
                    continue
                # [2026-04-26 16:32:11 UTC] ... Paper Trader starting ...
                try:
                    ts_str = line.split("] ", 1)[0].lstrip("[")
                    cutoff = datetime.strptime(
                        ts_str, "%Y-%m-%d %H:%M:%S UTC"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    except OSError:
        return FALLBACK_CUTOFF
    return cutoff or FALLBACK_CUTOFF


def cal_split(positions: list) -> dict:
    """Side-aware calibration aggregate for a list of settled positions."""
    if not positions:
        return {"n": 0, "wins": 0, "pnl": 0.0, "cost": 0.0,
                "n_yes": 0, "cal_yes": None,
                "n_no": 0, "cal_no": None,
                "cal_overall": None}
    sums = defaultdict(lambda: {"n": 0, "wins": 0, "mwp": 0.0,
                                "pnl": 0.0, "cost": 0.0})
    for p in positions:
        side = (p["open"].get("side") or "").upper()
        if side not in ("YES", "NO"):
            side = "YES"  # default fallback
        s = sums[side]
        s["n"] += 1
        s["mwp"] += _model_win_prob(p["open"])
        s["pnl"] += p["close"]["realized_pnl_usd"]
        s["cost"] += p["open"]["cost_usd"]
        if p["close"]["outcome"] == "win":
            s["wins"] += 1

    out = {
        "n": sum(s["n"] for s in sums.values()),
        "wins": sum(s["wins"] for s in sums.values()),
        "pnl": round(sum(s["pnl"] for s in sums.values()), 2),
        "cost": round(sum(s["cost"] for s in sums.values()), 2),
    }
    for side in ("YES", "NO"):
        s = sums.get(side, {"n": 0, "wins": 0, "mwp": 0.0})
        out[f"n_{side.lower()}"] = s["n"]
        out[f"cal_{side.lower()}"] = (
            round(s["wins"] / s["mwp"], 2) if s["mwp"] > 0 else None
        )
    total_mwp = sum(s["mwp"] for s in sums.values())
    out["cal_overall"] = round(out["wins"] / total_mwp, 2) if total_mwp > 0 else None
    return out


def render_md(rows: dict, cutoff: datetime) -> str:
    """Render markdown table with day-7 verdict on top."""
    sorted_dates = sorted(rows.keys())
    last_row = rows[sorted_dates[-1]] if sorted_dates else None

    new_cum = (last_row or {}).get("cumulative_new_regime", {}) or {}
    new_n = new_cum.get("n", 0)
    cal_yes = new_cum.get("cal_yes")
    cal_no = new_cum.get("cal_no")
    new_pnl = new_cum.get("pnl", 0.0)
    new_cost = new_cum.get("cost", 0.0)
    pnl_pct = (new_pnl / new_cost) if new_cost > 0 else 0.0

    days_elapsed = (
        (datetime.now(timezone.utc) - cutoff).days if cutoff else 0
    )

    # Verdict
    if new_n < TARGET_NEW_N:
        verdict = (f"IN PROGRESS — need {TARGET_NEW_N - new_n} more new-regime "
                   f"settlements (have {new_n}/{TARGET_NEW_N}, day {days_elapsed})")
    elif cal_yes is not None and TARGET_CAL_YES_LO <= cal_yes <= TARGET_CAL_YES_HI:
        verdict = f"SHIP — YES cal {cal_yes} in band [{TARGET_CAL_YES_LO}, {TARGET_CAL_YES_HI}], n={new_n}"
    elif cal_yes is not None and cal_yes < KILL_CAL_YES:
        verdict = f"KILL — YES cal {cal_yes} < {KILL_CAL_YES}; engine fundamentally broken"
    elif pnl_pct < KILL_PNL_PCT:
        verdict = f"KILL — P&L {pnl_pct*100:+.1f}% < {KILL_PNL_PCT*100:.0f}% on cost ${new_cost:.2f}"
    else:
        verdict = (f"ITERATE — YES cal {cal_yes}, outside band. "
                   f"Refit YES-side σ; do not ship to real money.")

    lines = [
        "# T1 Calibration Trend",
        "",
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}._  ",
        f"_New-regime cutoff: {cutoff.isoformat()}._",
        "",
        "## Day-7 Decision Status",
        "",
        f"- Days since restart: **{days_elapsed}**",
        f"- New-regime settlements: **{new_n}** / {TARGET_NEW_N} target",
        f"- YES cal (new regime): **{cal_yes if cal_yes is not None else '—'}**  "
        f"(target [{TARGET_CAL_YES_LO}, {TARGET_CAL_YES_HI}], kill if <{KILL_CAL_YES})",
        f"- NO cal (new regime): **{cal_no if cal_no is not None else '—'}**",
        f"- P&L (new regime): **${new_pnl:+.2f}** on ${new_cost:.2f} cost ({pnl_pct*100:+.1f}%)",
        "",
        f"### Verdict: **{verdict}**",
        "",
        "## Daily Trend",
        "",
        ("Side-aware: YES win prob = our_p, NO win prob = 1−our_p. "
         "cal = wins / Σ(side-aware win prob)."),
        "",
        ("| UTC date | Closed today (n / W / cal_yes / cal_no / $) "
         "| New-regime cum (n / cal_yes / cal_no / $) "
         "| All-time cum (n / cal_yes / cal_no / $) |"),
        "|---|---|---|---|",
    ]

    def fmt(d):
        n = d.get("n", 0)
        if n == 0:
            return "—"
        cy = d.get("cal_yes")
        cn = d.get("cal_no")
        cy_s = f"{cy:.2f}" if cy is not None else "—"
        cn_s = f"{cn:.2f}" if cn is not None else "—"
        return f"{n} / {d['wins']} / {cy_s} / {cn_s} / ${d['pnl']:+.2f}"

    for d in sorted_dates:
        r = rows[d]
        lines.append(
            f"| {d} | {fmt(r['today'])} "
            f"| {fmt(r['cumulative_new_regime'])} "
            f"| {fmt(r['cumulative_all'])} |"
        )

    if not sorted_dates:
        lines.append("| (no settlements yet) | — | — | — |")

    lines.append("")
    lines.append("## Decision criteria (locked 2026-04-26)")
    lines.append("")
    lines.append(f"- **SHIP** when new-regime n ≥ {TARGET_NEW_N} AND "
                 f"YES cal ∈ [{TARGET_CAL_YES_LO}, {TARGET_CAL_YES_HI}]")
    lines.append(f"- **ITERATE** (YES-side σ refit) when n ≥ {TARGET_NEW_N} "
                 f"and YES cal outside band but ≥ {KILL_CAL_YES}")
    lines.append(f"- **KILL** when YES cal < {KILL_CAL_YES} OR "
                 f"P&L < {KILL_PNL_PCT*100:.0f}% on cost")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    cutoff = find_new_regime_cutoff()
    settled = load_settled_positions()

    # Build per-close-date rows. Each row holds "closed today" plus the
    # cumulative state AS OF end of that day.
    by_close_date: dict = defaultdict(list)
    for p in settled:
        cd = (p["close"].get("ts") or "")[:10]
        if cd:
            by_close_date[cd].append(p)

    rows: dict = {}
    for date in sorted(by_close_date.keys()):
        today_settled = by_close_date[date]
        cum_all = [p for p in settled if (p["close"].get("ts") or "")[:10] <= date]
        cum_new = []
        for p in cum_all:
            try:
                open_dt = datetime.fromisoformat(
                    p["open"]["ts"].replace("Z", "+00:00")
                )
            except (KeyError, ValueError):
                continue
            if open_dt >= cutoff:
                cum_new.append(p)
        rows[date] = {
            "date_utc": date,
            "cutoff_utc": cutoff.isoformat(),
            "today": cal_split(today_settled),
            "cumulative_all": cal_split(cum_all),
            "cumulative_new_regime": cal_split(cum_new),
        }

    TREND_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(TREND_JSONL, "w") as f:
        for d in sorted(rows.keys()):
            f.write(json.dumps(rows[d]) + "\n")

    TREND_MD.write_text(render_md(rows, cutoff))

    # Brief stdout summary so launchd log captures something useful.
    last = rows[max(rows.keys())] if rows else None
    if last:
        cn = last["cumulative_new_regime"]
        print(f"calibration_trend updated. "
              f"new_regime n={cn['n']} cal_yes={cn['cal_yes']} "
              f"cal_no={cn['cal_no']} pnl=${cn['pnl']:+.2f}")
    else:
        print("calibration_trend updated. No settlements yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
