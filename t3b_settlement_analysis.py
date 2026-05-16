"""Terminal 3b — CPI Settlement Analysis.

Run after T3b positions settle (typically the morning after a BLS CPI print).
Reports calibration verdict on real settlements:

  1. Total + side-aware overall calibration
  2. By side (YES vs NO) — primary T1 lesson, exposes asymmetric calibration
  3. By event (per CPI print) — small n per event, but lets us see regime drift
  4. By lead band (1–3d vs 4–7d at open time)
  5. By strike zone relative to μ at open (below_μ / at_μ / above_μ)
  6. Nowcast residual check — was the actual within our empirical σ?
  7. Verdict — SHIP / ITERATE / KILL per spec v0.3 §7

Reads:
  ~/Documents/shadow_pnl/ledger.jsonl                  (filter engine=T3b)
  ~/Documents/terminal3b_data/bls_cpi_yoy.jsonl
  ~/Documents/terminal3b_data/empirical_sigma.json
  ~/Documents/terminal3b_data/nowcast_cleveland_fed.jsonl

Honors annul_close events.

Usage:
    python3 ~/Documents/t3b_settlement_analysis.py
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path.home() / "Documents"))
from t1_settlement_analysis import _model_win_prob  # noqa: E402 — reuse the side-aware helper


LEDGER = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"
T3B_DATA = Path.home() / "Documents" / "terminal3b_data"
BLS_PATH = T3B_DATA / "bls_cpi_yoy.jsonl"
SIGMA_PATH = T3B_DATA / "empirical_sigma.json"
NOWCAST_PATH = T3B_DATA / "nowcast_cleveland_fed.jsonl"
ENGINE = "T3b"

# Decision gate constants — match spec v0.3 §7
TARGET_PRINTS_FOR_SHIP = 3
TARGET_CAL_YES_LO = 0.7
TARGET_CAL_YES_HI = 1.3
KILL_CAL_YES = 0.3
KILL_PNL_PCT = -0.10


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def load_settled_t3b():
    """Replay the ledger and return list of {open, close} pairs for settled
    (and non-annulled) T3b positions."""
    if not LEDGER.exists():
        return []
    opens = {}
    closes_by_pid: Dict[str, list] = defaultdict(list)
    annulled_close_ts: Dict[str, set] = defaultdict(set)
    with open(LEDGER) as f:
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
            t = r.get("type")
            pid = r.get("position_id")
            if t == "open":
                opens[pid] = r
            elif t == "close":
                closes_by_pid[pid].append(r)
            elif t == "annul_close":
                annulled_close_ts[pid].add(r.get("annulled_close_ts"))

    settled = []
    for pid, op in opens.items():
        valid = [c for c in closes_by_pid.get(pid, [])
                 if c["ts"] not in annulled_close_ts.get(pid, set())]
        if not valid:
            continue
        close = max(valid, key=lambda c: c["ts"])
        settled.append({"open": op, "close": close})
    return settled


def load_bls_actuals() -> Dict[Tuple[int, int], dict]:
    out: Dict[Tuple[int, int], dict] = {}
    if not BLS_PATH.exists():
        return out
    with open(BLS_PATH) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            y = r.get("year")
            period = r.get("period", "")
            if not y or not period.startswith("M"):
                continue
            try:
                m = int(period[1:])
            except ValueError:
                continue
            if 1 <= m <= 12:
                out[(int(y), m)] = r
    return out


def load_sigma_table() -> dict:
    if not SIGMA_PATH.exists():
        return {}
    try:
        with open(SIGMA_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def load_final_nowcast_per_target() -> Dict[Tuple[int, int], dict]:
    """Pick the LATEST nowcast (max publish_date) per (target_year, target_month)."""
    out: Dict[Tuple[int, int], dict] = {}
    if not NOWCAST_PATH.exists():
        return out
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
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def event_target(event_ticker: str) -> Optional[Tuple[int, int]]:
    """KXCPIYOY-26APR → (2026, 4)."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    code = parts[-1].upper()
    if len(code) != 5 or not code[:2].isdigit():
        return None
    yy = int(code[:2])
    mm = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}.get(code[2:])
    if not mm:
        return None
    return (2000 + yy, mm)


def strike_zone(strike: float, mu: float) -> str:
    """Relative position of strike vs nowcast μ at open."""
    delta = strike - mu
    if abs(delta) < 0.15:
        return "at_μ"
    return "above_μ" if delta > 0 else "below_μ"


def cal_split(positions: List[dict]) -> dict:
    """Side-aware aggregation."""
    if not positions:
        return {"n": 0, "wins": 0, "pnl": 0.0, "cost": 0.0,
                "n_yes": 0, "cal_yes": None,
                "n_no": 0, "cal_no": None,
                "cal_overall": None}
    sums = defaultdict(lambda: {"n": 0, "wins": 0, "mwp": 0.0, "pnl": 0.0, "cost": 0.0})
    for p in positions:
        side = (p["open"].get("side") or "").upper()
        if side not in ("YES", "NO"):
            side = "YES"
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


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    print("=" * 80)
    print("TERMINAL 3b — CPI SETTLEMENT ANALYSIS")
    print("=" * 80)
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print()

    settled = load_settled_t3b()
    if not settled:
        print("No settled T3b positions yet.")
        print("First print is BLS CPI for April 2026, releasing 2026-05-13.")
        return 0

    actuals = load_bls_actuals()
    sigma_table = load_sigma_table()
    final_nowcasts = load_final_nowcast_per_target()

    print(f"Total settled T3b positions: {len(settled)}")
    total_cost = sum(p["open"]["cost_usd"] + p["open"].get("fee_usd", 0) for p in settled)
    total_pnl = sum(p["close"]["realized_pnl_usd"] for p in settled)
    total_wins = sum(1 for p in settled if p["close"]["outcome"] == "win")
    total_losses = sum(1 for p in settled if p["close"]["outcome"] == "loss")
    sum_mwp = sum(_model_win_prob(p["open"]) for p in settled)
    cal_overall = total_wins / sum_mwp if sum_mwp > 0 else None
    pnl_pct = total_pnl / total_cost if total_cost > 0 else 0.0
    print(f"  W/L: {total_wins}/{total_losses}")
    print(f"  Cost basis (incl. fees):  ${total_cost:,.2f}")
    print(f"  Realized P&L:             ${total_pnl:+.2f}  ({pnl_pct*100:+.1f}% on cost)")
    print(f"  Hit rate:                 {total_wins/len(settled)*100:.1f}%  "
          f"(model said {sum_mwp/len(settled)*100:.1f}%)")
    if cal_overall is not None:
        print(f"  Calibration ratio:        {cal_overall:.2f}  "
              f"(side-aware: wins / Σ model P(this-bet-wins))")
    print()

    # ----------------------------------------------------------------
    # 1. By side
    # ----------------------------------------------------------------
    print("-" * 80)
    print("1. BY SIDE")
    print("-" * 80)
    by_side: Dict[str, list] = defaultdict(list)
    for p in settled:
        side = (p["open"].get("side") or "").upper()
        by_side[side].append(p)
    print(f"  {'side':<5} {'n':>4} {'W':>3} {'hit%':>6} "
          f"{'avg_our_p':>10} {'avg_mwp':>8} {'cal':>5} "
          f"{'cost':>9} {'P&L':>9}")
    for side in ("YES", "NO"):
        ps = by_side.get(side, [])
        if not ps:
            continue
        n = len(ps)
        wins = sum(1 for p in ps if p["close"]["outcome"] == "win")
        cost = sum(p["open"]["cost_usd"] for p in ps)
        pnl = sum(p["close"]["realized_pnl_usd"] for p in ps)
        sum_op = sum(float((p["open"].get("signal_metadata") or {}).get("our_p", 0) or 0)
                     for p in ps)
        sum_mp = sum(_model_win_prob(p["open"]) for p in ps)
        cal = wins / sum_mp if sum_mp > 0 else float("nan")
        print(f"  {side:<5} {n:>4} {wins:>3} {wins/n*100:>5.1f}% "
              f"{sum_op/n*100:>9.1f}% {sum_mp/n*100:>7.1f}% {cal:>5.2f} "
              f"${cost:>+7.2f} ${pnl:>+7.2f}")
    print()

    # ----------------------------------------------------------------
    # 2. By event (per CPI print)
    # ----------------------------------------------------------------
    print("-" * 80)
    print("2. BY EVENT (one row per BLS print)")
    print("-" * 80)
    by_event: Dict[str, list] = defaultdict(list)
    for p in settled:
        et = (p["open"].get("signal_metadata") or {}).get("event_ticker", "?")
        by_event[et].append(p)
    print(f"  {'event':<22} {'target':<8} {'actual':>7} {'μ_open':>8} "
          f"{'n':>3} {'W':>2} {'cal':>5} {'P&L':>9}")
    for et in sorted(by_event.keys()):
        ps = by_event[et]
        tt = event_target(et)
        actual_str = "—"
        mu_open_str = "—"
        if tt:
            arow = actuals.get(tt)
            if arow and arow.get("yoy_rounded") is not None:
                actual_str = f"{arow['yoy_rounded']:.1f}%"
        # μ at open: average across positions (they were opened at slightly
        # different times so μ may have shifted; use mean for the table)
        mus = [float((p["open"].get("signal_metadata") or {}).get("mu", 0) or 0)
               for p in ps]
        mus = [m for m in mus if m]
        if mus:
            mu_open_str = f"{mean(mus):.2f}%"
        n = len(ps)
        wins = sum(1 for p in ps if p["close"]["outcome"] == "win")
        pnl = sum(p["close"]["realized_pnl_usd"] for p in ps)
        sum_mp = sum(_model_win_prob(p["open"]) for p in ps)
        cal = wins / sum_mp if sum_mp > 0 else float("nan")
        target_str = f"{tt[0]}-{tt[1]:02d}" if tt else "?"
        print(f"  {et:<22} {target_str:<8} {actual_str:>7} {mu_open_str:>8} "
              f"{n:>3} {wins:>2} {cal:>5.2f} ${pnl:>+7.2f}")
    print()

    # ----------------------------------------------------------------
    # 3. By lead band at open
    # ----------------------------------------------------------------
    print("-" * 80)
    print("3. BY LEAD BAND (at open time)")
    print("-" * 80)
    by_band: Dict[str, list] = defaultdict(list)
    for p in settled:
        band = (p["open"].get("signal_metadata") or {}).get("lead_band", "?")
        by_band[band].append(p)
    print(f"  {'band':<8} {'n':>3} {'W':>2} {'hit%':>6} "
          f"{'cal':>5} {'P&L':>9}")
    for band in ("4to7", "1to3", "?"):
        ps = by_band.get(band, [])
        if not ps:
            continue
        n = len(ps)
        wins = sum(1 for p in ps if p["close"]["outcome"] == "win")
        pnl = sum(p["close"]["realized_pnl_usd"] for p in ps)
        sum_mp = sum(_model_win_prob(p["open"]) for p in ps)
        cal = wins / sum_mp if sum_mp > 0 else float("nan")
        print(f"  {band:<8} {n:>3} {wins:>2} {wins/n*100:>5.1f}% "
              f"{cal:>5.2f} ${pnl:>+7.2f}")
    print()

    # ----------------------------------------------------------------
    # 4. By strike zone (vs μ at open)
    # ----------------------------------------------------------------
    print("-" * 80)
    print("4. BY STRIKE ZONE (relative to nowcast μ at open)")
    print("-" * 80)
    by_zone: Dict[str, list] = defaultdict(list)
    for p in settled:
        md = p["open"].get("signal_metadata") or {}
        strike = md.get("strike")
        mu = md.get("mu")
        if strike is None or mu is None:
            continue
        z = strike_zone(float(strike), float(mu))
        by_zone[z].append(p)
    print(f"  {'zone':<10} {'n':>3} {'W':>2} {'hit%':>6} "
          f"{'cal':>5} {'P&L':>9}")
    for z in ("below_μ", "at_μ", "above_μ"):
        ps = by_zone.get(z, [])
        if not ps:
            continue
        n = len(ps)
        wins = sum(1 for p in ps if p["close"]["outcome"] == "win")
        pnl = sum(p["close"]["realized_pnl_usd"] for p in ps)
        sum_mp = sum(_model_win_prob(p["open"]) for p in ps)
        cal = wins / sum_mp if sum_mp > 0 else float("nan")
        print(f"  {z:<10} {n:>3} {wins:>2} {wins/n*100:>5.1f}% "
              f"{cal:>5.2f} ${pnl:>+7.2f}")
    print()

    # ----------------------------------------------------------------
    # 5. Nowcast residual check (per print, not per position)
    # ----------------------------------------------------------------
    print("-" * 80)
    print("5. NOWCAST RESIDUAL CHECK")
    print("-" * 80)
    print("  For each settled print: was the actual within our empirical σ?")
    print()
    print(f"  {'event':<22} {'final μ':>9} {'actual':>8} "
          f"{'residual':>10} {'σ (24m)':>9} {'z':>6} {'flag':<14}")
    for et, ps in sorted(by_event.items()):
        tt = event_target(et)
        if not tt:
            continue
        arow = actuals.get(tt)
        nrec = final_nowcasts.get(tt)
        if arow is None or nrec is None:
            continue
        actual = arow.get("yoy_rounded")
        mu_final = nrec.get("cpi_yoy")
        if actual is None or mu_final is None:
            continue
        residual = float(actual) - float(mu_final)
        # Use the smallest lead bucket for σ comparison (latest nowcast = closest to release)
        sig_cell = ((sigma_table.get("by_lead_bucket") or {}).get("le3") or {})
        sigma = sig_cell.get("std_resid")
        z_str = "—"
        flag = ""
        if sigma:
            z = residual / sigma
            z_str = f"{z:+.2f}"
            if abs(z) > 2:
                flag = "← outside 2σ"
            elif abs(z) > 1:
                flag = "← outside 1σ"
        print(f"  {et:<22} {mu_final:>+8.2f}% {actual:>+7.1f}% "
              f"{residual:>+9.2f}% {sigma if sigma else '—':>8}% "
              f"{z_str:>6} {flag:<14}")
    print()

    # ----------------------------------------------------------------
    # 6. Verdict
    # ----------------------------------------------------------------
    print("=" * 80)
    print("VERDICT")
    print("=" * 80)
    n_prints = len(by_event)
    cal_yes = None
    sum_mp_yes = sum(_model_win_prob(p["open"])
                     for p in settled if (p["open"].get("side") or "").upper() == "YES")
    wins_yes = sum(1 for p in settled
                   if (p["open"].get("side") or "").upper() == "YES"
                   and p["close"]["outcome"] == "win")
    if sum_mp_yes > 0:
        cal_yes = wins_yes / sum_mp_yes

    print(f"  Prints settled:         {n_prints} / {TARGET_PRINTS_FOR_SHIP} required for SHIP")
    print(f"  Total settled positions: {len(settled)}")
    print(f"  YES side cal:            {cal_yes:.2f}" if cal_yes is not None else
          "  YES side cal:            — (no YES settlements)")
    print(f"  Realized P&L:            ${total_pnl:+.2f} ({pnl_pct*100:+.1f}% on cost)")
    print()

    if n_prints < TARGET_PRINTS_FOR_SHIP:
        print(f"  → IN PROGRESS: need {TARGET_PRINTS_FOR_SHIP - n_prints} more prints.")
        print(f"    Decision deferred. Review by-side and residual-check sections "
              f"for early warning signals.")
    elif cal_yes is None:
        print(f"  → NO YES SETTLEMENTS in {n_prints} prints — engine is essentially "
              f"NO-only. Review side selection.")
    elif cal_yes < KILL_CAL_YES:
        print(f"  → KILL: YES cal {cal_yes:.2f} < {KILL_CAL_YES}. "
              f"Engine fundamentally broken on YES side; redeploy capital.")
    elif pnl_pct < KILL_PNL_PCT:
        print(f"  → KILL: P&L {pnl_pct*100:+.1f}% < {KILL_PNL_PCT*100:.0f}% on cost. "
              f"Engine bleeding capital; redeploy.")
    elif TARGET_CAL_YES_LO <= cal_yes <= TARGET_CAL_YES_HI:
        print(f"  → SHIP: YES cal {cal_yes:.2f} in [{TARGET_CAL_YES_LO}, "
              f"{TARGET_CAL_YES_HI}]. Engine calibrated, P&L positive. "
              f"Plan real-money execution.")
    else:
        if cal_yes < TARGET_CAL_YES_LO:
            direction = "OVER-confident on YES"
        else:
            direction = "UNDER-confident on YES"
        print(f"  → ITERATE: YES cal {cal_yes:.2f} outside band; {direction}.")
        print(f"    Refit YES-side σ from settled residuals; do NOT ship to real money.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
