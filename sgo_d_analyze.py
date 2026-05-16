#!/usr/bin/env python3
"""SGO D analysis — Pinnacle vs retail signals from open/close data.

Reads terminal7_data/sgo_open_close_30d.jsonl. Computes three signals
that test the $299/mo decision rule.

Steve's decision framing (locked 2026-05-10):
  Pinnacle's open→close move LARGER in SAME DIRECTION than retail =
    Pinnacle is incorporating more information = structural value =
    SUBSCRIBE.
  Same direction + same magnitude = retail has captured the same
    signal = no edge = CANCEL.

Three signals:
  Signal-1 (move-magnitude):
    For each game: |Δ_pinn| / |Δ_retail| where Δ is (close - open) in
    implied prob. Median ratio across games. >1.2 = Pinnacle moves
    consistently farther; ~1.0 = same; <0.8 = retail moves farther.

  Signal-2 (directional alignment):
    Fraction of games where sign(Δ_pinn) == sign(Δ_retail). High
    alignment + small magnitude difference = retail mirrors Pinnacle.

  Signal-3 (lastUpdatedAt timing):
    Median (Pinn_lastUpdatedAt - Retail_lastUpdatedAt) in seconds.
    Negative = Pinnacle updated later than retail (sub-spread / lag).
    Positive = Pinnacle updates more recently (active book).
    Note: lastUpdatedAt is the LATEST change, not the only change.
    A coarse signal; useful for "who moves last" not "who moves first."

Output: human-readable report + JSON summary at the bottom.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DOCS = Path.home() / "Documents"
INPATH = DOCS / "terminal7_data" / "sgo_open_close_30d.jsonl"
OUTREPORT = DOCS / "sgo_d_decision_report.md"


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def main():
    # Load + dedupe by eventID (in case chunked pulls overlapped)
    events = {}
    with open(INPATH) as f:
        for line in f:
            r = json.loads(line)
            eid = r.get("eventID")
            if eid:
                events[eid] = r

    print(f"loaded {len(events)} unique events from {INPATH.name}")

    # Filter to events with all required fields
    usable = []
    for ev in events.values():
        if (ev.get("openBookOdds_implied_p") is None
            or ev.get("pinnacle") is None
            or ev["pinnacle"].get("implied_p") is None):
            continue
        # Need at least one retail book
        retail_ps = []
        for bk in ("draftkings", "fanduel", "betmgm"):
            b = ev.get(bk)
            if b and b.get("implied_p") is not None:
                retail_ps.append(b["implied_p"])
        if not retail_ps:
            continue
        ev["_retail_close_p"] = sum(retail_ps) / len(retail_ps)
        ev["_n_retail_books"] = len(retail_ps)
        usable.append(ev)

    print(f"usable: {len(usable)} (dropped {len(events) - len(usable)} with missing fields)")

    # Compute deltas (in pp = 100x prob diff)
    by_game = []
    for ev in usable:
        open_p = ev["openBookOdds_implied_p"]
        pinn_close = ev["pinnacle"]["implied_p"]
        retail_close = ev["_retail_close_p"]
        d_pinn = pinn_close - open_p   # signed prob delta
        d_retail = retail_close - open_p
        by_game.append({
            "eventID": ev["eventID"],
            "startsAt": ev["startsAt"],
            "open_p": open_p,
            "pinn_close_p": pinn_close,
            "retail_close_p": retail_close,
            "d_pinn": d_pinn,
            "d_retail": d_retail,
            "abs_d_pinn": abs(d_pinn),
            "abs_d_retail": abs(d_retail),
            "same_direction": (d_pinn * d_retail) > 0 if (d_pinn != 0 and d_retail != 0) else None,
            "pinn_minus_retail_sec": (
                (parse_iso(ev["pinnacle"].get("lastUpdatedAt")) -
                 parse_iso(ev[max(("draftkings","fanduel","betmgm"),
                                   key=lambda b: parse_iso((ev.get(b) or {}).get("lastUpdatedAt"))
                                                  or datetime.min.replace(tzinfo=parse_iso(ev["pinnacle"].get("lastUpdatedAt")).tzinfo)
                                                  if parse_iso(ev["pinnacle"].get("lastUpdatedAt")) else datetime.min)].get("lastUpdatedAt"))).total_seconds()
                if (parse_iso(ev["pinnacle"].get("lastUpdatedAt"))
                    and any(parse_iso((ev.get(b) or {}).get("lastUpdatedAt")) for b in ("draftkings","fanduel","betmgm")))
                else None
            ),
            "n_retail_books": ev["_n_retail_books"],
        })

    # ============================ Signal-1 ============================
    # Magnitude: |Δ_pinn| vs |Δ_retail|. Use the ratio when |Δ_retail| > 1pp;
    # below that threshold the ratio explodes for noise reasons.
    NOISE_FLOOR = 0.01  # 1pp
    pinn_bigger = 0
    retail_bigger = 0
    similar = 0  # within ±0.5pp difference
    ratios = []
    abs_diff = []
    for g in by_game:
        d_pinn = g["abs_d_pinn"]
        d_retail = g["abs_d_retail"]
        diff = d_pinn - d_retail
        abs_diff.append(diff)
        if max(d_pinn, d_retail) < 0.005:
            similar += 1
            continue
        if d_retail > NOISE_FLOOR:
            ratios.append(d_pinn / d_retail)
        if abs(diff) < 0.005:
            similar += 1
        elif d_pinn > d_retail:
            pinn_bigger += 1
        else:
            retail_bigger += 1

    # ============================ Signal-2 ============================
    # Direction alignment
    same_dir = sum(1 for g in by_game if g["same_direction"] is True)
    diff_dir = sum(1 for g in by_game if g["same_direction"] is False)
    no_move = sum(1 for g in by_game if g["same_direction"] is None)

    # ============================ Signal-3 ============================
    # Pinnacle vs latest-retail-book timing — REDO simply
    timing_diffs_sec = []
    for ev in usable:
        pinn_t = parse_iso(ev["pinnacle"].get("lastUpdatedAt"))
        retail_ts = []
        for bk in ("draftkings", "fanduel", "betmgm"):
            b = ev.get(bk) or {}
            t = parse_iso(b.get("lastUpdatedAt"))
            if t:
                retail_ts.append(t)
        if pinn_t and retail_ts:
            # use median of retail timestamps as the comparator
            retail_ts.sort()
            mid = retail_ts[len(retail_ts) // 2]
            diff = (pinn_t - mid).total_seconds()
            timing_diffs_sec.append(diff)

    # ====================== Print human report ======================
    print("=" * 72)
    print(" SGO D — Pinnacle vs retail (DK/FD/MGM consensus) — open→close")
    print("=" * 72)
    print(f"  events analyzed: {len(by_game)}")
    print()

    print("  SIGNAL-1: open→close move magnitude")
    print(f"    games where |Δ_pinn| > |Δ_retail|+0.5pp : {pinn_bigger:>4}  ({pinn_bigger/len(by_game)*100:.1f}%)")
    print(f"    games where |Δ_retail| > |Δ_pinn|+0.5pp : {retail_bigger:>4}  ({retail_bigger/len(by_game)*100:.1f}%)")
    print(f"    games similar (within 0.5pp)            : {similar:>4}  ({similar/len(by_game)*100:.1f}%)")
    if ratios:
        ratios.sort()
        print(f"    median |Δ_pinn|/|Δ_retail| ratio        : {statistics.median(ratios):.3f}")
        print(f"    25th–75th pct ratio                     : {ratios[len(ratios)//4]:.3f} – {ratios[3*len(ratios)//4]:.3f}")
    abs_diff_pp = [d*100 for d in abs_diff]
    if abs_diff_pp:
        abs_diff_pp.sort()
        print(f"    median (|Δ_pinn| − |Δ_retail|) pp        : {statistics.median(abs_diff_pp):+.3f}pp")
        print(f"    mean (|Δ_pinn| − |Δ_retail|) pp          : {sum(abs_diff_pp)/len(abs_diff_pp):+.3f}pp")
    print()

    print("  SIGNAL-2: directional alignment")
    n = len(by_game)
    print(f"    same direction                          : {same_dir:>4}  ({same_dir/n*100:.1f}%)")
    print(f"    opposite direction                      : {diff_dir:>4}  ({diff_dir/n*100:.1f}%)")
    print(f"    no move on at least one side            : {no_move:>4}  ({no_move/n*100:.1f}%)")
    print()

    print("  SIGNAL-3: lastUpdatedAt timing (Pinn − retail-median)")
    if timing_diffs_sec:
        timing_diffs_sec.sort()
        med_sec = statistics.median(timing_diffs_sec)
        n_tt = len(timing_diffs_sec)
        p25 = timing_diffs_sec[n_tt // 4]
        p75 = timing_diffs_sec[3 * n_tt // 4]
        pinn_later = sum(1 for d in timing_diffs_sec if d > 0)
        pinn_earlier = sum(1 for d in timing_diffs_sec if d < 0)
        same = sum(1 for d in timing_diffs_sec if d == 0)
        print(f"    n compared                              : {n_tt}")
        print(f"    median Δ                                : {med_sec:+.0f}s ({med_sec/60:+.1f}min)")
        print(f"    25th–75th pct                           : {p25:+.0f}s – {p75:+.0f}s")
        print(f"    Pinnacle updated AFTER retail (lag)     : {pinn_later:>4}  ({pinn_later/n_tt*100:.1f}%)")
        print(f"    Pinnacle updated BEFORE retail (lead)   : {pinn_earlier:>4}  ({pinn_earlier/n_tt*100:.1f}%)")
        print(f"    same time                               : {same:>4}")
    print()

    # ====================== Decision verdict ======================
    print("  ─" * 36)
    print("  DECISION INPUTS:")

    # Magnitude rule per Steve: Pinnacle larger in same direction = structural value
    pinn_larger_same_dir = sum(
        1 for g in by_game
        if g["same_direction"] is True and g["abs_d_pinn"] > g["abs_d_retail"] + 0.005
    )
    pinn_larger_pct = pinn_larger_same_dir / max(1, n) * 100

    print(f"    games where Pinnacle moved farther in SAME direction: "
          f"{pinn_larger_same_dir} / {n} ({pinn_larger_pct:.1f}%)")

    median_excess_move = (
        statistics.median([g["abs_d_pinn"] - g["abs_d_retail"]
                           for g in by_game if g["same_direction"] is True]) * 100
        if same_dir > 0 else 0
    )
    print(f"    median Pinnacle excess move (when same direction): {median_excess_move:+.3f}pp")

    print()
    if pinn_larger_pct >= 40 and median_excess_move >= 0.5:
        verdict = "TENTATIVE_SUBSCRIBE — Pinnacle moves consistently farther"
    elif pinn_larger_pct < 25 or abs(median_excess_move) < 0.2:
        verdict = "TENTATIVE_CANCEL — Pinnacle and retail move similarly"
    else:
        verdict = "INCONCLUSIVE — need timing signal from forward poll (B)"

    print(f"  D-VERDICT (proxy): {verdict}")
    print(f"  Note: D answers 'who moved farther' only — NOT 'how long until catchup'.")
    print(f"  B (forward poll) needed for the timing question regardless.")
    print("=" * 72)

    # JSON summary at end of stdout for programmatic consumption
    summary = {
        "n_events": n,
        "signal_1": {
            "pinn_bigger": pinn_bigger,
            "retail_bigger": retail_bigger,
            "similar": similar,
            "median_ratio": statistics.median(ratios) if ratios else None,
            "median_abs_diff_pp": statistics.median([d*100 for d in abs_diff]) if abs_diff else None,
        },
        "signal_2": {
            "same_direction": same_dir,
            "opposite_direction": diff_dir,
            "no_move": no_move,
        },
        "signal_3": {
            "n": len(timing_diffs_sec),
            "median_sec": statistics.median(timing_diffs_sec) if timing_diffs_sec else None,
            "pinn_later_pct": (
                sum(1 for d in timing_diffs_sec if d > 0) / len(timing_diffs_sec) * 100
                if timing_diffs_sec else None
            ),
        },
        "decision": {
            "pinn_larger_same_dir_pct": pinn_larger_pct,
            "median_excess_move_pp": median_excess_move,
            "verdict": verdict,
        },
    }
    print()
    print("JSON_SUMMARY=" + json.dumps(summary))


if __name__ == "__main__":
    main()
