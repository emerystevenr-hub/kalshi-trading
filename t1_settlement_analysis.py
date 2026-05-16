"""Terminal 1 — Settlement Analysis.

Run after T1 positions close. Reports calibration verdict on real settlements:

  1. Win rate + avg P&L by station
  2. Calibration ratio (actual hits / Σ our_p) — overall + by station + by edge bucket
  3. Per-cluster outcome (station × target_date) — did clustering help or hurt?
  4. Fee drag vs gross P&L
  5. Capped-vs-fired counts (validates per-cluster cap)
  6. Edge bucket breakdown — does $0.20+ edge perform better than $0.08-0.12?

Reads:
  ~/Documents/shadow_pnl/ledger.jsonl
  ~/Documents/terminal1_phase2_paper_trader.log

Honors annul_close events (excludes annulled closes from analysis).

Usage:
    python3 ~/Documents/t1_settlement_analysis.py
"""

import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean


LEDGER = Path.home() / "Documents" / "shadow_pnl" / "ledger.jsonl"
PAPER_LOG = Path.home() / "Documents" / "terminal1_phase2_paper_trader.log"
ENGINE = "T1"


def _model_win_prob(open_event: dict) -> float:
    """Probability OUR MODEL assigns to THIS BET winning (side-aware).

    `our_p` in signal_metadata is always P(YES). For YES side, the bet
    wins when YES happens → win_prob = our_p. For NO side, the bet wins
    when YES doesn't happen → win_prob = 1 - our_p.

    Calibration ratio = total_wins / sum(model_win_prob) is well-defined
    only when this side correction is applied. Without it, NO-heavy books
    on extreme tails inflate the ratio mechanically — a calibrated model
    saying "P(YES)=5%" on a bet you took NO at $0.95 should be CHECKED
    against the 95% win prob, not the 5% YES prob.
    """
    meta = open_event.get("signal_metadata", {}) or {}
    our_p = float(meta.get("our_p", 0) or 0)
    side = (open_event.get("side") or "").upper()
    if side == "NO":
        return 1.0 - our_p
    return our_p  # default: treat as YES


def load_settled_positions():
    """Return list of {open, close} pairs for closed (and not-annulled) T1 positions."""
    if not LEDGER.exists():
        return []
    opens = {}
    closes_by_pid = defaultdict(list)
    annulled_close_ts = defaultdict(set)

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
        # Find the most recent non-annulled close
        valid_closes = [
            c for c in closes_by_pid.get(pid, [])
            if c["ts"] not in annulled_close_ts.get(pid, set())
        ]
        if not valid_closes:
            continue
        # Use the latest one (in case there are multiple after reopen)
        close = max(valid_closes, key=lambda c: c["ts"])
        settled.append({"open": op, "close": close})
    return settled


def load_actuals_index():
    """Build {(station, date_local): {high_f, low_f}} from NWS actuals files."""
    actuals_dir = Path.home() / "Documents" / "terminal1_data"
    idx = {}
    for path in actuals_dir.glob("nws_actuals_*.jsonl"):
        station = path.stem.replace("nws_actuals_", "")
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d = r.get("date_local")
                if d:
                    idx[(station, d)] = {
                        "high_f": r.get("high_f"),
                        "low_f": r.get("low_f"),
                    }
    return idx


def main() -> int:
    print("=" * 80)
    print("TERMINAL 1 — SETTLEMENT ANALYSIS")
    print("=" * 80)
    print(f"Generated: {datetime.utcnow().isoformat()}Z")
    print()

    settled = load_settled_positions()
    if not settled:
        print("No settled T1 positions. Run after Apr 25 actuals drop.")
        return 0

    print(f"Total settled: {len(settled)}")
    total_cost = sum(p["open"]["cost_usd"] + p["open"].get("fee_usd", 0)
                     for p in settled)
    total_pnl = sum(p["close"]["realized_pnl_usd"] for p in settled)
    total_contracts = sum(p["open"]["size"] for p in settled)
    pnl_per_contract = total_pnl / total_contracts if total_contracts else 0
    total_wins = sum(1 for p in settled if p["close"]["outcome"] == "win")
    total_losses = sum(1 for p in settled if p["close"]["outcome"] == "loss")
    total_fees = sum(p["open"].get("fee_usd", 0) + p["close"].get("fee_usd", 0)
                     for p in settled)
    sum_model_win_prob = sum(_model_win_prob(p["open"]) for p in settled)
    print(f"  W/L: {total_wins}/{total_losses}")
    print(f"  Cost basis (incl. fees):  ${total_cost:,.2f}")
    print(f"  Realized P&L:             ${total_pnl:+.2f}")
    print(f"  P&L per contract:         ${pnl_per_contract:+.4f}  ({total_contracts} contracts)")
    print(f"  Gross P&L (excl. fees):   ${total_pnl + total_fees:+.2f}")
    print(f"  Total fees:               ${total_fees:.2f}  ({total_fees/total_cost*100:.1f}% of cost)")
    print(f"  Hit rate:                 {total_wins/len(settled)*100:.1f}%  "
          f"(model said {sum_model_win_prob/len(settled)*100:.1f}%)")
    if sum_model_win_prob > 0:
        cal = total_wins / sum_model_win_prob
        print(f"  Calibration ratio:        {cal:.2f}  "
              f"(side-aware: wins / Σ model P(this-bet-wins); "
              f"1.0 = perfect; <0.7 over-confident; >1.3 under-confident)")
    print()

    # ----------------------------------------------------------------
    # 1b. By side (YES vs NO) — exposes asymmetric calibration
    # ----------------------------------------------------------------
    print("-" * 80)
    print("1b. BY SIDE (model_win_prob = our_p for YES, 1-our_p for NO)")
    print("-" * 80)
    by_side = defaultdict(lambda: {
        "n": 0, "wins": 0, "pnl": 0.0, "cost": 0.0,
        "sum_mwp": 0.0, "sum_our_p_yes": 0.0,
    })
    for p in settled:
        side = (p["open"].get("side") or "").upper()
        d = by_side[side]
        d["n"] += 1
        d["pnl"] += p["close"]["realized_pnl_usd"]
        d["cost"] += p["open"]["cost_usd"]
        d["sum_mwp"] += _model_win_prob(p["open"])
        d["sum_our_p_yes"] += float(
            (p["open"].get("signal_metadata") or {}).get("our_p", 0) or 0
        )
        if p["close"]["outcome"] == "win":
            d["wins"] += 1
    print(f"  {'side':<5} {'n':>4} {'W':>3} {'hit%':>6} {'avg_our_p':>10} "
          f"{'avg_mwp':>8} {'cal':>5} {'cost':>8} {'P&L':>8}")
    for side, d in sorted(by_side.items()):
        n = d["n"]
        if n == 0:
            continue
        hit_pct = d["wins"] / n * 100
        avg_our_p = d["sum_our_p_yes"] / n * 100
        avg_mwp = d["sum_mwp"] / n * 100
        cal = d["wins"] / d["sum_mwp"] if d["sum_mwp"] > 0 else float("nan")
        print(f"  {side:<5} {n:>4} {d['wins']:>3} {hit_pct:>5.1f}% "
              f"{avg_our_p:>9.1f}% {avg_mwp:>7.1f}% {cal:>5.2f} "
              f"${d['cost']:>+6.2f} ${d['pnl']:>+6.2f}")
    print()
    print("  Read: cal=hit_rate / avg_mwp. cal=1 calibrated. cal>1 model under-")
    print("        confident on this side. cal<1 over-confident. avg_mwp is the")
    print("        side-aware claim — it's what 'cal' is checked against.")
    print()

    # ----------------------------------------------------------------
    # 1. By station
    # ----------------------------------------------------------------
    print("-" * 80)
    print("1. BY STATION")
    print("-" * 80)
    by_station = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0,
        "cost": 0.0, "sum_mwp": 0.0,
    })
    for p in settled:
        meta = p["open"].get("signal_metadata", {}) or {}
        s = meta.get("station", "?")
        by_station[s]["n"] += 1
        by_station[s]["pnl"] += p["close"]["realized_pnl_usd"]
        by_station[s]["fees"] += p["open"].get("fee_usd", 0) + p["close"].get("fee_usd", 0)
        by_station[s]["cost"] += p["open"]["cost_usd"]
        by_station[s]["sum_mwp"] += _model_win_prob(p["open"])
        if p["close"]["outcome"] == "win":
            by_station[s]["wins"] += 1
        elif p["close"]["outcome"] == "loss":
            by_station[s]["losses"] += 1
    print(f"  {'station':<8} {'n':>4} {'W':>3} {'L':>3} {'hit%':>6} "
          f"{'model%':>7} {'cal':>5} {'cost':>9} {'P&L':>9} {'%cost':>7}")
    for s, d in sorted(by_station.items()):
        n = d["n"]
        hit_pct = d["wins"] / n * 100 if n else 0
        claim_pct = d["sum_mwp"] / n * 100 if n else 0
        cal = d["wins"] / d["sum_mwp"] if d["sum_mwp"] > 0 else float("nan")
        pct_cost = d["pnl"] / d["cost"] * 100 if d["cost"] else 0
        print(f"  {s:<8} {n:>4} {d['wins']:>3} {d['losses']:>3} "
              f"{hit_pct:>5.1f}% {claim_pct:>6.1f}% {cal:>5.2f} "
              f"${d['cost']:>7.2f} ${d['pnl']:>+7.2f} {pct_cost:>+6.1f}%")
    print()

    # ----------------------------------------------------------------
    # 2. By edge bucket
    # ----------------------------------------------------------------
    print("-" * 80)
    print("2. BY EDGE BUCKET")
    print("-" * 80)
    buckets = [
        ("0.05-0.08", 0.05, 0.08),
        ("0.08-0.12", 0.08, 0.12),
        ("0.12-0.20", 0.12, 0.20),
        ("0.20+",     0.20, 999),
    ]
    by_bucket = {label: {"n": 0, "wins": 0, "pnl": 0.0, "sum_mwp": 0.0}
                 for label, _, _ in buckets}
    for p in settled:
        meta = p["open"].get("signal_metadata", {}) or {}
        edge = meta.get("edge", 0)
        for label, lo, hi in buckets:
            if lo <= edge < hi:
                by_bucket[label]["n"] += 1
                by_bucket[label]["pnl"] += p["close"]["realized_pnl_usd"]
                by_bucket[label]["sum_mwp"] += _model_win_prob(p["open"])
                if p["close"]["outcome"] == "win":
                    by_bucket[label]["wins"] += 1
                break
    print(f"  {'bucket':<11} {'n':>4} {'W':>3} {'hit%':>6} {'model%':>7} "
          f"{'cal':>5} {'P&L':>9} {'avg P&L':>9}")
    for label, _, _ in buckets:
        d = by_bucket[label]
        n = d["n"]
        if n == 0:
            print(f"  {label:<11} {0:>4}")
            continue
        hit_pct = d["wins"] / n * 100
        claim_pct = d["sum_mwp"] / n * 100
        cal = d["wins"] / d["sum_mwp"] if d["sum_mwp"] > 0 else float("nan")
        avg = d["pnl"] / n
        print(f"  {label:<11} {n:>4} {d['wins']:>3} {hit_pct:>5.1f}% "
              f"{claim_pct:>6.1f}% {cal:>5.2f} "
              f"${d['pnl']:>+7.2f} ${avg:>+7.2f}")
    print()

    # ----------------------------------------------------------------
    # 3. Per-cluster
    # ----------------------------------------------------------------
    print("-" * 80)
    print("3. PER CLUSTER (station × target_date)")
    print("-" * 80)
    clusters = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for p in settled:
        meta = p["open"].get("signal_metadata", {}) or {}
        s = meta.get("station", "?")
        td = meta.get("target_date", "?")
        clusters[(s, td)]["n"] += 1
        clusters[(s, td)]["pnl"] += p["close"]["realized_pnl_usd"]
        if p["close"]["outcome"] == "win":
            clusters[(s, td)]["wins"] += 1
    print(f"  {'cluster':<22} {'n':>3} {'W':>2} {'P&L':>9}")
    for key, d in sorted(clusters.items(), key=lambda x: -abs(x[1]["pnl"])):
        s, td = key
        flag = ""
        if d["pnl"] < -3:
            flag = "← MISS"
        elif d["pnl"] > 3:
            flag = "← STRONG"
        print(f"  {s} {td:<14} {d['n']:>3} {d['wins']:>2} ${d['pnl']:>+7.2f}  {flag}")
    print()

    # ----------------------------------------------------------------
    # 4. Capped-vs-fired counts (from paper trader log)
    # ----------------------------------------------------------------
    print("-" * 80)
    print("4. PAPER TRADER ACTIVITY (cap effectiveness)")
    print("-" * 80)
    fires = 0
    caps = 0
    skip_dups = 0
    if PAPER_LOG.exists():
        with open(PAPER_LOG) as f:
            for line in f:
                if "[FIRE]" in line:
                    fires += 1
                elif "[cap]" in line:
                    caps += 1
                elif "[skip] already open" in line:
                    skip_dups += 1
    print(f"  Total [FIRE] events (life of log):  {fires}")
    print(f"  Total [cap] events (life of log):   {caps}")
    print(f"  Total [skip-already-open] events:   {skip_dups}")
    if fires > 0:
        print(f"  Cap rate: {caps / (fires + caps) * 100:.1f}% of would-be fires were capped")
    print()

    # ----------------------------------------------------------------
    # 4b. Cool/warm regime bias flag — per (station, target_date, metric)
    # ----------------------------------------------------------------
    print("-" * 80)
    print("4b. REGIME BIAS FLAGS (ensemble vs actual residuals)")
    print("-" * 80)
    actuals_idx = load_actuals_index()
    # Group settled positions by (station, target_date, which_metric)
    # to extract ensemble_mean (single value per group) and compare to actual.
    bias_rows = defaultdict(lambda: {"ensemble_mean": None, "n_positions": 0,
                                     "pnl": 0.0})
    for p in settled:
        meta = p["open"].get("signal_metadata", {}) or {}
        s = meta.get("station")
        td = meta.get("target_date")
        # which_metric isn't in metadata directly — derive from ticker
        ticker = p["open"].get("ticker", "")
        if "HIGH" in ticker.upper():
            which = "high"
        elif "LOW" in ticker.upper():
            which = "low"
        else:
            continue
        if not s or not td:
            continue
        key = (s, td, which)
        bias_rows[key]["ensemble_mean"] = meta.get("ensemble_mean")
        bias_rows[key]["n_positions"] += 1
        bias_rows[key]["pnl"] += p["close"]["realized_pnl_usd"]

    flags_found = 0
    for (s, td, which), row in sorted(bias_rows.items()):
        actual = actuals_idx.get((s, td), {}).get(f"{which}_f")
        ens = row["ensemble_mean"]
        if actual is None or ens is None:
            continue
        residual = actual - ens
        if abs(residual) >= 2.0 and row["n_positions"] >= 3:
            direction = "WARM" if residual > 0 else "COOL"
            print(f"  {direction} BIAS: {s} {td} {which:<5} "
                  f"ensemble={ens:.1f}°  actual={actual:.1f}°  "
                  f"residual={residual:+.1f}°F  "
                  f"({row['n_positions']} positions, P&L=${row['pnl']:+.2f})")
            flags_found += 1
    if flags_found == 0:
        print("  (no regime biases ≥2°F across 3+ positions in this sample)")
    print()

    # ----------------------------------------------------------------
    # 5. By target date (per-day calibration)
    # ----------------------------------------------------------------
    print("-" * 80)
    print("5. BY TARGET DATE")
    print("-" * 80)
    by_date = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "sum_mwp": 0.0})
    for p in settled:
        meta = p["open"].get("signal_metadata", {}) or {}
        td = meta.get("target_date", "?")
        by_date[td]["n"] += 1
        by_date[td]["pnl"] += p["close"]["realized_pnl_usd"]
        by_date[td]["sum_mwp"] += _model_win_prob(p["open"])
        if p["close"]["outcome"] == "win":
            by_date[td]["wins"] += 1
    print(f"  {'date':<14} {'n':>4} {'W':>3} {'hit%':>6} {'model%':>7} {'cal':>5} {'P&L':>9}")
    for td, d in sorted(by_date.items()):
        n = d["n"]
        hit_pct = d["wins"] / n * 100 if n else 0
        claim_pct = d["sum_mwp"] / n * 100 if n else 0
        cal = d["wins"] / d["sum_mwp"] if d["sum_mwp"] > 0 else float("nan")
        print(f"  {td:<14} {n:>4} {d['wins']:>3} {hit_pct:>5.1f}% {claim_pct:>6.1f}% "
              f"{cal:>5.2f} ${d['pnl']:>+7.2f}")
    print()

    # ----------------------------------------------------------------
    # Verdict
    # ----------------------------------------------------------------
    print("=" * 80)
    print("VERDICT")
    print("=" * 80)
    if sum_model_win_prob > 0:
        cal = total_wins / sum_model_win_prob
        if 0.7 <= cal <= 1.3:
            print(f"  CALIBRATED: ratio {cal:.2f} in band [0.7, 1.3]")
        elif cal < 0.7:
            print(f"  OVER-CONFIDENT: ratio {cal:.2f} < 0.7 — model claiming too many wins")
        else:
            print(f"  UNDER-CONFIDENT: ratio {cal:.2f} > 1.3 — could be more aggressive")
    pct_pnl = total_pnl / total_cost * 100 if total_cost else 0
    print(f"  P&L: ${total_pnl:+.2f} on ${total_cost:.2f} cost ({pct_pnl:+.1f}%)")
    if total_pnl > 0:
        print(f"  → engine PROFITABLE on this sample")
    else:
        print(f"  → engine NEGATIVE on this sample (could be variance, need more N)")

    n = len(settled)
    if n < 50:
        print(f"  WARNING: only {n} settlements. Need ~100+ for statistical confidence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
