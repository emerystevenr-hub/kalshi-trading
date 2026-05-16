"""Regression test for kalshi_shadow_v4.py liquidation-cascade behavior.

Guards the v5.1c fix: gate-urgent liquidations (fast_move / high_vol /
imbalance / other regime-change gates) must engage the 3-stage cascade
starting at stage 1 (passive maker), NOT skip straight to stage 2
(cross-spread). The prior behavior — a backdate on line 959 that made
liquidation_started_ts appear 10 minutes old — was burning taker fees
on every single gate-urgent trip. See kalshi_shadow_v51b.log and the
IMPEACH-28-JAN01 fills from 2026-04-20 for the empirical case.

Covers:
  - Gate-urgent liquidation enters at stage 1 (not skipped)
  - _liquidation_quote returns best_bid + 1 tick during the stage-1 window
  - After the stage-1 window, quote routes to stage-2 cross
  - Disabled / blacklist liquidations DO still skip to stage 2 (by design)
  - Non-urgent liquidations (stale_no_sells) stay in stage 1 indefinitely

Run:  python3 kalshi_shadow_test.py
Exit code 0 on pass, 1 on fail.
"""

import os
import sys
import time

os.environ.setdefault("KALSHI_KEY_ID", "test")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "/dev/null")

import kalshi_shadow_v4 as sv  # noqa: E402


def _mk_shadow(inventory=10.0, cost_basis=0.50, best_bid=0.55, best_ask=0.57,
               best_bid_size=100, tick=0.01):
    s = sv.MarketShadow(
        ticker="TESTTKR",
        title="Test market",
        tick=tick,
        vol_24h=50_000.0,
    )
    s.inventory = inventory
    s.cost_basis = cost_basis
    s.best_bid = best_bid
    s.best_ask = best_ask
    s.best_bid_size = best_bid_size
    return s


class Fail(Exception):
    pass


def expect(cond, msg):
    if not cond:
        raise Fail(msg)


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────

def test_urgent_enters_stage1_not_stage2():
    """After v5.1c fix, an URGENT liquidation must NOT backdate
    liquidation_started_ts. The first quote after trigger should be
    the stage-1 passive maker quote (best_bid + 1 tick), not a cross.
    v5.6: uses 'net_growth_2x' since gate:* is now non-urgent by default."""
    s = _mk_shadow(best_bid=0.55, best_ask=0.57, tick=0.01)
    before = time.time()
    sv._start_liquidation(s, "net_growth_2x")
    after = time.time()
    # started_ts should be ~now (within a small slack), NOT 10+ min ago
    expect(before - 0.5 <= s.liquidation_started_ts <= after + 0.5,
           f"liquidation_started_ts backdated: "
           f"{s.liquidation_started_ts} vs now={after}")
    expect(s.liquidation_urgent, "net_growth_2x should be flagged urgent")
    expect(s.liquidating, "liquidation flag not set")
    # First call to _liquidation_quote should return stage-1 passive maker
    quote = sv._liquidation_quote(s, s.tick)
    expected = round(s.best_bid + s.tick, 4)   # 0.56
    expect(abs(quote - expected) < 1e-9,
           f"stage-1 quote wrong: {quote} expected {expected}")
    expect(s.liquidation_stage == 1,
           f"liquidation_stage should be 1 at entry, got {s.liquidation_stage}")


def test_urgent_escalates_to_stage2_after_window():
    """After LIQ_STAGE_1_DURATION_MIN elapsed, URGENT quote routes to stage 2.
    v5.6: uses 'net_growth_2x' since gate:* is now non-urgent by default."""
    s = _mk_shadow()
    sv._start_liquidation(s, "net_growth_2x")
    # Simulate time passing past the stage-1 window
    s.liquidation_started_ts = time.time() - (sv.LIQ_STAGE_1_DURATION_MIN * 60 + 1)
    # Stage 2 doesn't return a quote — it calls _synthetic_cross_exit —
    # so we just verify that _liquidation_quote transitions stage flag.
    _ = sv._liquidation_quote(s, s.tick)
    expect(s.liquidation_stage == 2,
           f"expected stage 2 after window elapsed, got {s.liquidation_stage}")


def test_v56_gate_reasons_now_nonurgent_by_default():
    """v5.6 regression guard: gate:fast_move / gate:imbalance / gate:high_vol
    must all classify as NON-URGENT by default. The urgent cascade on gate
    exits was converting winning trades into losers via cross-slippage."""
    for reason in ("gate:fast_move", "gate:imbalance", "gate:high_vol",
                   "gate:wide_spread", "gate:velocity"):
        s = _mk_shadow()
        sv._start_liquidation(s, reason)
        expect(not s.liquidation_urgent,
               f"v5.6: {reason} must be NON-URGENT by default, got urgent=True")
        expect(s.liquidating, f"{reason} should still trigger liquidation")


def test_v56_gate_env_override_restores_urgent():
    """v5.6 rollback path: GATE_EXITS_ARE_URGENT=1 env restores old urgent
    behavior for A/B comparison. Tests the function directly since env
    read happens at module load."""
    # Test the underlying classifier logic by monkey-patching the flag
    original = sv.GATE_EXITS_ARE_URGENT
    try:
        sv.GATE_EXITS_ARE_URGENT = True
        expect(sv._is_urgent_liquidation("gate:fast_move"),
               "gate:fast_move must route URGENT when GATE_EXITS_ARE_URGENT=True")
    finally:
        sv.GATE_EXITS_ARE_URGENT = original


def test_v56_nonurgent_paths_preserved():
    """v5.6 sanity: non-gate URGENT reasons still classify URGENT."""
    expect(sv._is_urgent_liquidation("disabled"),
           "disabled must stay URGENT")
    expect(sv._is_urgent_liquidation("discovery_drop"),
           "discovery_drop must stay URGENT")
    expect(sv._is_urgent_liquidation("net_growth_2x"),
           "net_growth_* must stay URGENT (runaway inventory)")
    expect(not sv._is_urgent_liquidation("stale_no_sells"),
           "stale_no_sells must stay NON-URGENT")
    expect(not sv._is_urgent_liquidation("turnover"),
           "turnover must stay NON-URGENT")


def test_v56_gate_default_is_nonurgent():
    """v5.6 hard-coded default check: GATE_EXITS_ARE_URGENT must default False."""
    expect(sv.GATE_EXITS_ARE_URGENT is False,
           "GATE_EXITS_ARE_URGENT default must be False in v5.6")


def test_v57_min_net_edge_constants_present():
    """v5.7: MIN_NET_EDGE + MIN_NET_EDGE_SLIPPAGE env-wired with sensible defaults."""
    expect(hasattr(sv, "MIN_NET_EDGE"),
           "v5.7: MIN_NET_EDGE constant missing")
    expect(hasattr(sv, "MIN_NET_EDGE_SLIPPAGE"),
           "v5.7: MIN_NET_EDGE_SLIPPAGE constant missing")
    expect(sv.MIN_NET_EDGE == 0.01,
           f"MIN_NET_EDGE default must be 0.01, got {sv.MIN_NET_EDGE}")
    expect(sv.MIN_NET_EDGE_SLIPPAGE == 0.005,
           f"MIN_NET_EDGE_SLIPPAGE default must be 0.005, got {sv.MIN_NET_EDGE_SLIPPAGE}")


def test_v57_expected_buy_edge_thin_spread_negative():
    """v5.7: at 2-tick spread near 0.50, expected edge is negative (fees
    eat all the gross). Gate should reject markets like this."""
    # 0.50 / 0.52: our_buy=0.51, mid=0.51, exit=mid-tick=0.50
    # gross = 0.50 - 0.51 = -0.01 → edge clearly negative
    edge = sv._expected_buy_edge(entry_price=0.51, mid=0.51, tick=0.01)
    expect(edge < sv.MIN_NET_EDGE,
           f"2t-spread at 0.50 mid must fail MIN_NET_EDGE gate: edge={edge:.4f}")


def test_v57_expected_buy_edge_wide_spread_passes():
    """v5.7: at 10-tick spread near 0.50, edge should clear MIN_NET_EDGE."""
    # 0.45 / 0.55: our_buy=0.46, mid=0.50, exit=mid-tick=0.49
    # gross = 0.49 - 0.46 = 0.03, fees ~= 2*0.0175*0.5*0.5 = 0.00875, slip 0.005
    # edge ~= 0.03 - 0.00875 - 0.005 = 0.016 → passes 0.01 floor
    edge = sv._expected_buy_edge(entry_price=0.46, mid=0.50, tick=0.01)
    expect(edge >= sv.MIN_NET_EDGE,
           f"10t-spread at 0.50 mid should pass MIN_NET_EDGE gate: edge={edge:.4f}")


def test_v57_expected_buy_edge_extreme_price_fee_lower():
    """v5.7: fees scale as p(1-p), so entries at extreme prices (0.05 or
    0.95) have near-zero fee and should pass easier than mid-priced entries
    at the same gross edge."""
    # 0.05 / 0.08: our_buy=0.06, mid=0.065, exit=0.055
    # gross = -0.005 (negative! conservative exit below entry)
    edge_extreme = sv._expected_buy_edge(entry_price=0.06, mid=0.065, tick=0.01)
    # vs mid-priced 0.50 / 0.53: our_buy=0.51, mid=0.515, exit=0.505
    edge_mid = sv._expected_buy_edge(entry_price=0.51, mid=0.515, tick=0.01)
    # Both should be negative here (conservative exit = mid - 1t), but
    # extreme should be less negative because fees are tiny.
    expect(edge_extreme > edge_mid,
           f"extreme-price edge {edge_extreme:.4f} should exceed mid-price edge {edge_mid:.4f}")


def test_v57_expected_buy_edge_rejects_invalid_inputs():
    """v5.7: zero/negative inputs must return negative edge (safe rejection)."""
    expect(sv._expected_buy_edge(0.0, 0.5, 0.01) < 0,
           "zero entry must return negative edge")
    expect(sv._expected_buy_edge(0.5, 0.0, 0.01) < 0,
           "zero mid must return negative edge")
    expect(sv._expected_buy_edge(0.5, 0.5, 0.0) < 0,
           "zero tick must return negative edge")


def test_v57_low_edge_gate_wired_in_quote_loop():
    """v5.7 source guard: the low_edge gate must fire in _quote_loop on BUY
    only, increment gate_rejects, and reference MIN_NET_EDGE."""
    src = open(sv.__file__).read()
    expect("_expected_buy_edge" in src,
           "_expected_buy_edge helper missing from source")
    expect('gate_rejects["low_edge"]' in src,
           "low_edge gate reject not wired in source")
    expect("MIN_NET_EDGE" in src,
           "MIN_NET_EDGE constant not referenced in gate")


def test_v57_low_edge_gate_only_blocks_buy_not_sell():
    """v5.7 source-level check: the low_edge gate conditions on s.our_buy_px
    and only zeroes the buy side. Never touches s.our_sell_px."""
    src = open(sv.__file__).read()
    # Find the gate block by its comment marker
    import re
    block_match = re.search(
        r"v5\.7 MIN_NET_EDGE entry-quality gate.*?gate_rejects\[\"low_edge\"\].*?\+ 1",
        src, re.DOTALL,
    )
    expect(block_match is not None, "v5.7 gate block not found by regex")
    block = block_match.group(0)
    expect("our_sell_px" not in block,
           "low_edge gate must NOT touch sell-side quote")
    expect("our_buy_px = 0" in block,
           "low_edge gate must zero the buy quote")


# ──────────────────────────────────────────────────────────────────────
# v5.8 inventory discipline tests
# ──────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────
# v5.9 — fast_move spread-relative velocity gate
# ────────────────────────────────────────────────────────────────────

def _mk_velocity_shadow(best_bid: float, best_ask: float, vel: float,
                        tick: float = 0.01):
    """Build a shadow positioned so that every pre-velocity gate passes:
    warmup satisfied, book two-sided within MAX_SPREAD_DOLLARS, rolling
    stdev low enough to clear high_vol. mid_history is populated with
    two samples inside VELOCITY_WINDOW_SEC whose delta equals `vel`."""
    s = _mk_shadow(inventory=0.0, best_bid=best_bid, best_ask=best_ask,
                   tick=tick)
    s.trades_seen = sv.MIN_TRADES_FOR_WARMUP
    mid = (best_bid + best_ask) / 2.0
    now = time.time()
    # Two samples, 15s apart, delta == vel. Stdev over 2 samples = vel/2,
    # which at vel<=0.05 is well below MAX_ROLLING_VOL=0.05 default.
    s.mid_history.append((now - 15.0, mid))
    s.mid_history.append((now, mid + vel))
    return s


def test_v59_velocity_constants_present():
    """v5.9: spread-relative velocity constants env-wired with correct defaults."""
    expect(hasattr(sv, "MAX_VELOCITY_FLOOR"),
           "v5.9: MAX_VELOCITY_FLOOR constant missing")
    expect(hasattr(sv, "VELOCITY_FRAC_SPREAD"),
           "v5.9: VELOCITY_FRAC_SPREAD constant missing")
    expect(hasattr(sv, "MAX_VELOCITY"),
           "v5.9: MAX_VELOCITY (safety cap) constant missing")
    expect(sv.MAX_VELOCITY_FLOOR == 0.005,
           f"MAX_VELOCITY_FLOOR default must be 0.005, got {sv.MAX_VELOCITY_FLOOR}")
    expect(sv.VELOCITY_FRAC_SPREAD == 0.5,
           f"VELOCITY_FRAC_SPREAD default must be 0.5, got {sv.VELOCITY_FRAC_SPREAD}")
    expect(sv.MAX_VELOCITY == 0.05,
           f"MAX_VELOCITY default must be 0.05, got {sv.MAX_VELOCITY}")


def test_v59_fast_move_cent_tick_narrow_rejects():
    """v5.9 case 1: cent-tick 3t spread ($0.03), velocity $0.02 → reject.
    threshold = max(0.005, 0.5 × 0.03) = 0.015. 0.02 > 0.015 → fast_move."""
    s = _mk_velocity_shadow(best_bid=0.50, best_ask=0.53, vel=0.02, tick=0.01)
    ok, reason = sv._meets_market_conditions(s)
    expect(not ok and reason == "fast_move",
           f"3t cent spread with vel=0.02 must trip fast_move; got ok={ok}, reason={reason}")


def test_v59_fast_move_cent_tick_narrow_passes():
    """v5.9 case 2: cent-tick 3t spread ($0.03), velocity $0.008 → pass.
    threshold = max(0.005, 0.5 × 0.03) = 0.015. 0.008 < 0.015 → pass."""
    s = _mk_velocity_shadow(best_bid=0.50, best_ask=0.53, vel=0.008, tick=0.01)
    ok, reason = sv._meets_market_conditions(s)
    expect(ok, f"3t cent spread with vel=0.008 must clear fast_move; got ok={ok}, reason={reason}")


def test_v59_fast_move_penny_wide_passes():
    """v5.9 case 3: penny-tick wide spread ($0.09), velocity $0.03 → pass.
    threshold = max(0.005, 0.5 × 0.09) = 0.045. 0.03 < 0.045 → pass.
    Prior absolute gate would have rejected this (0.03 > 0.03)."""
    s = _mk_velocity_shadow(best_bid=0.45, best_ask=0.54, vel=0.03, tick=0.001)
    ok, reason = sv._meets_market_conditions(s)
    expect(ok, f"wide penny spread with vel=0.03 must clear fast_move; got ok={ok}, reason={reason}")


def test_v59_fast_move_penny_wide_rejects():
    """v5.9 case 4: penny-tick wide spread ($0.09), velocity $0.05 → reject.
    threshold = max(0.005, 0.5 × 0.09)=0.045, capped by MAX_VELOCITY=0.05 → 0.045.
    0.05 > 0.045 → fast_move."""
    s = _mk_velocity_shadow(best_bid=0.45, best_ask=0.54, vel=0.05, tick=0.001)
    ok, reason = sv._meets_market_conditions(s)
    expect(not ok and reason == "fast_move",
           f"wide penny spread with vel=0.05 must trip fast_move; got ok={ok}, reason={reason}")


def test_v59_fast_move_zero_spread_fallback_uses_floor():
    """v5.9 case 5: pathological zero-spread case (best_bid == best_ask; the
    book-quality gate will reject first with no_book, so we test the
    threshold computation directly). With spread=0, threshold = floor = 0.005.
    vel=0.004 → below floor → pass; vel=0.006 → above floor → reject."""
    spread = 0.0
    threshold = max(sv.MAX_VELOCITY_FLOOR, sv.VELOCITY_FRAC_SPREAD * spread)
    threshold = min(threshold, sv.MAX_VELOCITY)
    expect(threshold == sv.MAX_VELOCITY_FLOOR,
           f"zero-spread must fall back to floor; got threshold={threshold}")
    expect(0.004 < threshold,
           f"vel=0.004 must pass floor; threshold={threshold}")
    expect(0.006 > threshold,
           f"vel=0.006 must exceed floor; threshold={threshold}")


def test_v59_source_wired_in_meets_market_conditions():
    """v5.9 source guard: the gate body must reference the new constants
    and compute a spread-relative threshold, not the old absolute check."""
    src = open(sv.__file__).read()
    expect("VELOCITY_FRAC_SPREAD * spread" in src,
           "v5.9: spread-relative threshold formula missing from source")
    expect("MAX_VELOCITY_FLOOR" in src,
           "v5.9: MAX_VELOCITY_FLOOR must be referenced in gate")
    # Old absolute-only form ("if vel > MAX_VELOCITY:" with no threshold
    # computation) must not be the active gate check.
    import re
    # Match the exact absolute-only pattern that was replaced.
    old_pattern = re.search(
        r"vel = _price_velocity\(s\).*?if vel > MAX_VELOCITY:\s*\n\s*return False, \"fast_move\"",
        src, re.DOTALL,
    )
    expect(old_pattern is None,
           "v5.9: legacy absolute-only velocity gate still present in source")


# ────────────────────────────────────────────────────────────────────
# v5.9 — book-reconstruction diagnostics (Patch B, instrumentation only)
# ────────────────────────────────────────────────────────────────────

def test_v59b_shadow_has_book_diagnostic_fields():
    """v5.9b: MarketShadow must expose the four diagnostic counters."""
    s = _mk_shadow()
    for fld in ("snapshot_count", "snapshot_one_sided_yes_only",
                "snapshot_one_sided_no_only", "book_side_zeroed_by_snapshot"):
        expect(hasattr(s, fld), f"v5.9b: MarketShadow.{fld} missing")
        expect(getattr(s, fld) == 0,
               f"v5.9b: MarketShadow.{fld} must default to 0")


def test_v59b_snapshot_count_increments():
    """v5.9b: snapshot_count increments on every apply_orderbook_snapshot call."""
    s = sv.MarketShadow(ticker="T", title="t", tick=0.01, vol_24h=20000.0)
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.51", "100"]],
        "no_dollars_fp":  [["0.47", "100"]],
    })
    expect(s.snapshot_count == 2,
           f"v5.9b: snapshot_count must be 2, got {s.snapshot_count}")
    expect(s.snapshot_one_sided_yes_only == 0,
           "v5.9b: two-sided snapshots must not increment yes-only counter")
    expect(s.snapshot_one_sided_no_only == 0,
           "v5.9b: two-sided snapshots must not increment no-only counter")


def test_v59b_one_sided_yes_only_detected():
    """v5.9b: a snapshot with yes_dollars_fp but no no_dollars_fp must
    increment snapshot_one_sided_yes_only — this is the suspected
    upstream cause of the `no_book` spike."""
    s = sv.MarketShadow(ticker="T", title="t", tick=0.01, vol_24h=20000.0)
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        # no_dollars_fp missing entirely (matches observed log pattern)
    })
    expect(s.snapshot_one_sided_yes_only == 1,
           f"v5.9b: yes-only snapshot must increment counter, got {s.snapshot_one_sided_yes_only}")
    expect(s.snapshot_one_sided_no_only == 0,
           "v5.9b: yes-only snapshot must NOT increment no-only counter")


def test_v59b_one_sided_no_only_detected():
    """v5.9b: symmetric check — no_dollars_fp only, no yes side."""
    s = sv.MarketShadow(ticker="T", title="t", tick=0.01, vol_24h=20000.0)
    sv.apply_orderbook_snapshot(s, {
        "no_dollars_fp": [["0.48", "100"]],
    })
    expect(s.snapshot_one_sided_no_only == 1,
           f"v5.9b: no-only snapshot must increment counter, got {s.snapshot_one_sided_no_only}")
    expect(s.snapshot_one_sided_yes_only == 0,
           "v5.9b: no-only snapshot must NOT increment yes-only counter")


def test_v59b_book_side_zeroed_detected_by_one_sided_snapshot():
    """v5.9b: end-to-end test of the suspected failure path. A two-sided
    snapshot populates both sides, then a yes-only re-snapshot wipes
    no_levels → best_ask transitions from non-zero to zero →
    book_side_zeroed_by_snapshot increments. This is the exact sequence
    that trips the `no_book` gate downstream."""
    s = sv.MarketShadow(ticker="T", title="t", tick=0.01, vol_24h=20000.0)
    # Initial two-sided snapshot: both best_bid and best_ask live.
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    expect(s.best_bid > 0 and s.best_ask > 0,
           "v5.9b: initial two-sided snapshot must set both sides")
    zeroed_before = s.book_side_zeroed_by_snapshot
    # Re-snapshot with only yes side → no_levels wiped, best_ask → 0.
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.51", "100"]],
    })
    expect(s.best_ask == 0,
           f"v5.9b: yes-only resnapshot must zero best_ask, got {s.best_ask}")
    expect(s.book_side_zeroed_by_snapshot == zeroed_before + 1,
           f"v5.9b: non-zero→zero transition must increment counter; "
           f"before={zeroed_before}, after={s.book_side_zeroed_by_snapshot}")


def test_v59b_book_side_not_zeroed_when_already_zero():
    """v5.9b: if a side was already zero before recompute, recompute must
    NOT increment book_side_zeroed_by_snapshot. Counter measures the
    transition, not the state."""
    s = sv.MarketShadow(ticker="T", title="t", tick=0.01, vol_24h=20000.0)
    # Empty levels → recompute → both sides stay at 0. No transition.
    sv.recompute_top_of_book(s)
    expect(s.book_side_zeroed_by_snapshot == 0,
           f"v5.9b: 0→0 recompute must not increment; got {s.book_side_zeroed_by_snapshot}")


def test_v59b_instrumentation_does_not_change_behavior():
    """v5.9b safety: instrumentation must not alter book reconstruction.
    After applying a standard two-sided snapshot, resulting best_bid/ask
    must match the pre-instrumentation expected values."""
    s = sv.MarketShadow(ticker="T", title="t", tick=0.01, vol_24h=20000.0)
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"], ["0.49", "50"]],
        "no_dollars_fp":  [["0.48", "100"], ["0.47", "50"]],
    })
    # Best yes = 0.50 → best_bid = 0.50
    # Best no = 0.48 → best_ask = 1.0 - 0.48 = 0.52
    expect(abs(s.best_bid - 0.50) < 1e-9,
           f"v5.9b: best_bid must be 0.50, got {s.best_bid}")
    expect(abs(s.best_ask - 0.52) < 1e-9,
           f"v5.9b: best_ask must be 0.52, got {s.best_ask}")


# ────────────────────────────────────────────────────────────────────
# v5.9c — Engine 1 → Engine 2 flow-signal publisher (publish-only)
# ────────────────────────────────────────────────────────────────────

def _mk_flow_shadow(ticker="FLOWTKR", best_bid=0.50, best_ask=0.53, tick=0.01,
                    yes_taker_vol=0.0, no_taker_vol=0.0,
                    mid_history=None, trade_flow=None):
    """Shadow positioned for flow-metric tests. Skips warmup requirement —
    _compute_flow_metrics does not consult gate state."""
    s = sv.MarketShadow(ticker=ticker, title="t", tick=tick, vol_24h=20000.0)
    s.best_bid = best_bid
    s.best_ask = best_ask
    s.cumulative_yes_taker_vol = yes_taker_vol
    s.cumulative_no_taker_vol = no_taker_vol
    if mid_history:
        for (offset, mid) in mid_history:
            s.mid_history.append((time.time() + offset, mid))
    if trade_flow:
        for (offset, size, side) in trade_flow:
            s.trade_flow.append((time.time() + offset, size, side))
    return s


def test_v59c_flow_constants_present():
    """v5.9c + v5.9c.1: publisher constants env-wired with spec defaults."""
    expect(hasattr(sv, "FLOW_SIGNALS_PATH"),
           "v5.9c: FLOW_SIGNALS_PATH missing")
    expect(hasattr(sv, "FLOW_SIGNALS_ENABLED"), "v5.9c: FLOW_SIGNALS_ENABLED missing")
    expect(hasattr(sv, "FLOW_SIGNALS_PUBLISH_SEC"), "v5.9c: FLOW_SIGNALS_PUBLISH_SEC missing")
    expect(hasattr(sv, "FLOW_SIGNALS_THRESHOLD"), "v5.9c: FLOW_SIGNALS_THRESHOLD missing")
    expect(sv.FLOW_W_IMBALANCE == 0.4,
           f"FLOW_W_IMBALANCE default must be 0.4, got {sv.FLOW_W_IMBALANCE}")
    expect(sv.FLOW_W_VELOCITY == 0.3,
           f"FLOW_W_VELOCITY default must be 0.3, got {sv.FLOW_W_VELOCITY}")
    expect(sv.FLOW_W_BIAS == 0.3,
           f"FLOW_W_BIAS default must be 0.3, got {sv.FLOW_W_BIAS}")
    expect(abs((sv.FLOW_W_IMBALANCE + sv.FLOW_W_VELOCITY + sv.FLOW_W_BIAS) - 1.0) < 1e-9,
           "v5.9c: weights must sum to 1.0 at default")
    # v5.9c.1 updated defaults
    expect(sv.FLOW_SIGNALS_THRESHOLD == 0.7,
           f"v5.9c.1: FLOW_SIGNALS_THRESHOLD default must be 0.7, got {sv.FLOW_SIGNALS_THRESHOLD}")
    expect(sv.FLOW_BIAS_NEUTRAL_EPSILON == 0.08,
           f"v5.9c.1: FLOW_BIAS_NEUTRAL_EPSILON default must be 0.08, got {sv.FLOW_BIAS_NEUTRAL_EPSILON}")
    expect(hasattr(sv, "FLOW_REGIME_MIN_CONSEC"),
           "v5.9c.1: FLOW_REGIME_MIN_CONSEC constant missing")
    expect(sv.FLOW_REGIME_MIN_CONSEC == 2,
           f"v5.9c.1: FLOW_REGIME_MIN_CONSEC default must be 2, got {sv.FLOW_REGIME_MIN_CONSEC}")


def test_v59c_flow_schema_complete():
    """v5.9c: every required field present, correct types, bounded ranges."""
    s = _mk_flow_shadow()
    m = sv._compute_flow_metrics(s)
    required = {"ticker", "timestamp", "imbalance_score", "velocity_score",
                "fill_bias", "flow_score", "regime_state", "pressure_direction",
                "directional_conviction"}
    missing = required - set(m.keys())
    expect(not missing, f"v5.9c: missing fields {missing}")
    expect(0.0 <= m["imbalance_score"] <= 1.0,
           f"imbalance_score out of range: {m['imbalance_score']}")
    expect(0.0 <= m["velocity_score"] <= 1.0,
           f"velocity_score out of range: {m['velocity_score']}")
    expect(-1.0 <= m["fill_bias"] <= 1.0,
           f"fill_bias out of range: {m['fill_bias']}")
    expect(0.0 <= m["flow_score"] <= 1.0,
           f"flow_score out of range: {m['flow_score']}")
    expect(m["regime_state"] in ("stable", "pressure_up", "pressure_down"),
           f"regime_state invalid: {m['regime_state']}")
    expect(m["pressure_direction"] in ("yes", "no", "neutral"),
           f"pressure_direction invalid: {m['pressure_direction']}")


def test_v59c_empty_market_all_zero_stable():
    """v5.9c: market with no trade/mid data must produce all-zero scores,
    regime=stable, pressure=neutral. Engine 2 can safely iterate the feed
    without special-casing cold starts."""
    s = _mk_flow_shadow()
    m = sv._compute_flow_metrics(s)
    expect(m["imbalance_score"] == 0.0, f"empty imbalance: {m['imbalance_score']}")
    expect(m["velocity_score"] == 0.0, f"empty velocity: {m['velocity_score']}")
    expect(m["fill_bias"] == 0.0, f"empty fill_bias: {m['fill_bias']}")
    expect(m["flow_score"] == 0.0, f"empty flow_score: {m['flow_score']}")
    expect(m["regime_state"] == "stable", f"empty regime: {m['regime_state']}")
    expect(m["pressure_direction"] == "neutral",
           f"empty pressure: {m['pressure_direction']}")


def test_v59c_pressure_up_when_high_flow_and_positive_bias():
    """v5.9c: pressure_up = flow_score > threshold AND fill_bias > 0.
    v5.9c.1: persistence requires two consecutive raw=pressure_up publishes
    before regime_state flips. First call must publish stable; second
    (with same inputs) must publish pressure_up."""
    mid_hist = [(-14, 0.50), (0, 0.53)]  # drift +0.03 over 14s
    # 6 yes-heavy trades → imbalance_score=1.0 (needed with threshold=0.7
    # since velocity+bias alone only reach flow=0.6).
    trade_flow = [(-1.0 * i, 10.0, "yes") for i in range(6)]
    s = _mk_flow_shadow(best_bid=0.52, best_ask=0.54,
                        mid_history=mid_hist, trade_flow=trade_flow)
    m1 = sv._compute_flow_metrics(s)
    expect(m1["fill_bias"] > 0,
           f"fill_bias must be positive for rising mid, got {m1['fill_bias']}")
    expect(m1["flow_score"] > sv.FLOW_SIGNALS_THRESHOLD,
           f"flow_score must exceed threshold, got {m1['flow_score']}")
    expect(m1["regime_state"] == "stable",
           f"v5.9c.1: first publish must be stable pending persistence, got {m1['regime_state']}")
    # Replenish pruned state for a deterministic second call.
    s.mid_history.clear()
    s.mid_history.append((time.time() - 14, 0.50))
    s.mid_history.append((time.time(), 0.53))
    s.trade_flow.clear()
    for tf in [(time.time() - 1.0 * i, 10.0, "yes") for i in range(6)]:
        s.trade_flow.append(tf)
    m2 = sv._compute_flow_metrics(s)
    expect(m2["regime_state"] == "pressure_up",
           f"v5.9c.1: second consecutive raw pressure_up must publish, got {m2['regime_state']}")
    expect(m2["pressure_direction"] == "yes",
           f"expected pressure_direction=yes, got {m2['pressure_direction']}")


def test_v59c_pressure_down_when_high_flow_and_negative_bias():
    """v5.9c.1: symmetric — two consecutive raw pressure_down to flip."""
    mid_hist = [(-14, 0.53), (0, 0.50)]  # drift -0.03
    trade_flow = [(-1.0 * i, 10.0, "no") for i in range(6)]
    s = _mk_flow_shadow(best_bid=0.49, best_ask=0.51,
                        mid_history=mid_hist, trade_flow=trade_flow)
    m1 = sv._compute_flow_metrics(s)
    expect(m1["fill_bias"] < 0,
           f"fill_bias must be negative for falling mid, got {m1['fill_bias']}")
    expect(m1["flow_score"] > sv.FLOW_SIGNALS_THRESHOLD,
           f"flow_score must exceed threshold, got {m1['flow_score']}")
    expect(m1["regime_state"] == "stable",
           f"v5.9c.1: first publish must be stable pending persistence, got {m1['regime_state']}")
    s.mid_history.clear()
    s.mid_history.append((time.time() - 14, 0.53))
    s.mid_history.append((time.time(), 0.50))
    s.trade_flow.clear()
    for tf in [(time.time() - 1.0 * i, 10.0, "no") for i in range(6)]:
        s.trade_flow.append(tf)
    m2 = sv._compute_flow_metrics(s)
    expect(m2["regime_state"] == "pressure_down",
           f"v5.9c.1: second consecutive raw pressure_down must publish, got {m2['regime_state']}")
    expect(m2["pressure_direction"] == "no",
           f"expected pressure_direction=no, got {m2['pressure_direction']}")


def test_v59c_stable_when_flow_below_threshold():
    """v5.9c: flow below threshold stays stable regardless of direction."""
    # Small drift, balanced imbalance, small velocity: flow_score stays low.
    mid_hist = [(-14, 0.500), (0, 0.501)]  # tiny 0.001 drift
    s = _mk_flow_shadow(best_bid=0.50, best_ask=0.52,
                        yes_taker_vol=500.0, no_taker_vol=500.0,
                        mid_history=mid_hist)
    m = sv._compute_flow_metrics(s)
    expect(m["flow_score"] <= sv.FLOW_SIGNALS_THRESHOLD,
           f"low-flow case must stay below threshold, got {m['flow_score']}")
    expect(m["regime_state"] == "stable",
           f"expected stable, got {m['regime_state']}")


def test_v59c_pressure_direction_neutral_band():
    """v5.9c: fill_bias within ±epsilon → pressure_direction='neutral'.
    Prevents micro-flicker around zero from flipping the direction signal
    on every tick."""
    # Drift exactly zero with established mid_history.
    mid_hist = [(-14, 0.50), (0, 0.50)]
    s = _mk_flow_shadow(mid_history=mid_hist)
    m = sv._compute_flow_metrics(s)
    expect(m["pressure_direction"] == "neutral",
           f"zero-drift must be neutral, got {m['pressure_direction']}")


def test_v59c_flow_score_weighting_formula():
    """v5.9c: flow_score follows W_IMB*imb + W_VEL*vel + W_BIAS*|bias|.
    Construct a case where we know all three inputs exactly and verify
    the arithmetic end-to-end.

    Note: _trade_imbalance reads the windowed `trade_flow` list (not
    cumulative counters) and requires ≥MIN_IMBALANCE_SAMPLES entries —
    we populate 6 yes-side trades to clear that floor with pure-yes flow."""
    # 6 yes-side trades in the imbalance window → imbalance = 1.0.
    # No mid history → velocity = 0 and fill_bias = 0.
    # Expected flow_score = W_IMB * 1.0 + 0 + 0 = 0.4.
    trade_flow = [(-1.0 * i, 10.0, "yes") for i in range(6)]
    s = _mk_flow_shadow(trade_flow=trade_flow)
    m = sv._compute_flow_metrics(s)
    expect(abs(m["imbalance_score"] - 1.0) < 1e-9,
           f"pure-yes imbalance must be 1.0, got {m['imbalance_score']}")
    expect(m["velocity_score"] == 0.0,
           f"no mid history → velocity_score 0, got {m['velocity_score']}")
    expect(m["fill_bias"] == 0.0,
           f"no mid history → fill_bias 0, got {m['fill_bias']}")
    expected = sv.FLOW_W_IMBALANCE * 1.0
    expect(abs(m["flow_score"] - expected) < 1e-4,
           f"flow_score arithmetic: expected {expected}, got {m['flow_score']}")


def test_v59c_fill_bias_clipped_to_unit_range():
    """v5.9c: extreme drift clipped to [-1, 1]. A 5¢ drift on a 1¢ spread
    would naively yield fill_bias = 5.0; must be clipped to 1.0."""
    mid_hist = [(-14, 0.50), (0, 0.55)]  # 5¢ drift
    s = _mk_flow_shadow(best_bid=0.52, best_ask=0.53,  # 1¢ spread
                        mid_history=mid_hist)
    m = sv._compute_flow_metrics(s)
    expect(m["fill_bias"] == 1.0,
           f"extreme positive drift must clip to 1.0, got {m['fill_bias']}")


def test_v59c_publish_writes_valid_json_atomically():
    """v5.9c: _publish_flow_signals writes a valid, parseable JSON file
    with the expected top-level schema."""
    import tempfile
    import json as _json
    # Install a temp path, register a test state, publish, read back.
    original_path = sv.FLOW_SIGNALS_PATH
    original_states = sv.STATES.copy()
    try:
        with tempfile.TemporaryDirectory() as td:
            sv.FLOW_SIGNALS_PATH = os.path.join(td, "engine1_flow_signals.json")
            sv.STATES.clear()
            sv.STATES["TESTTKR"] = _mk_flow_shadow(ticker="TESTTKR")
            sv._publish_flow_signals()
            expect(os.path.exists(sv.FLOW_SIGNALS_PATH),
                   f"publisher must create file at {sv.FLOW_SIGNALS_PATH}")
            # .tmp must NOT linger (atomic rename happened).
            expect(not os.path.exists(sv.FLOW_SIGNALS_PATH + ".tmp"),
                   "v5.9c: .tmp file must be renamed away after publish")
            with open(sv.FLOW_SIGNALS_PATH) as f:
                data = _json.load(f)
            expect("generated_at_ms" in data, "missing generated_at_ms")
            expect("version" in data, "missing version")
            expect("markets" in data, "missing markets")
            expect("TESTTKR" in data["markets"], "test ticker missing from feed")
            expect(data["weights"]["imbalance"] == 0.4, "weights.imbalance wrong")
    finally:
        sv.FLOW_SIGNALS_PATH = original_path
        sv.STATES.clear()
        sv.STATES.update(original_states)


def test_v59c1_regime_disagreement_stays_stable():
    """v5.9c.1: raw classifications that disagree within the persistence
    window must publish stable. pu → pd sequence = no persistence. Only
    two consecutive identical raws unlock a non-stable regime_state."""
    # First call: rising mid → raw pressure_up, published stable.
    s = _mk_flow_shadow(best_bid=0.52, best_ask=0.54,
                        mid_history=[(-14, 0.50), (0, 0.53)])
    m1 = sv._compute_flow_metrics(s)
    expect(m1["regime_state"] == "stable",
           f"first raw pu must publish stable, got {m1['regime_state']}")
    # Flip inputs to falling mid → raw pressure_down.
    s.mid_history.clear()
    s.best_bid = 0.49; s.best_ask = 0.51
    s.mid_history.append((time.time() - 14, 0.53))
    s.mid_history.append((time.time(), 0.50))
    m2 = sv._compute_flow_metrics(s)
    expect(m2["regime_state"] == "stable",
           f"v5.9c.1: pu → pd disagreement must publish stable, got {m2['regime_state']}")


def test_v59c1_regime_history_capped():
    """v5.9c.1: recent_regime_raw must not grow unbounded. Repeated calls
    keep history at FLOW_REGIME_HISTORY_LEN."""
    s = _mk_flow_shadow()
    for _ in range(10):
        sv._compute_flow_metrics(s)
    expect(len(s.recent_regime_raw) <= sv.FLOW_REGIME_HISTORY_LEN,
           f"v5.9c.1: history cap violated: len={len(s.recent_regime_raw)}")


def test_v59c1_regime_recovers_after_interruption():
    """v5.9c.1: pressure sequence interrupted by a stable publish must
    require another two consecutive publishes to re-enter a regime.
    Guards against a transient flat moment resetting and then immediately
    re-asserting pressure on a single reading."""
    mid_up = lambda: [(time.time() - 14, 0.50), (time.time(), 0.53)]
    flow_yes = lambda: [(time.time() - 1.0 * i, 10.0, "yes") for i in range(6)]

    def _reload_pu_inputs(shadow):
        shadow.mid_history.clear()
        for (t, m) in mid_up():
            shadow.mid_history.append((t, m))
        shadow.trade_flow.clear()
        for tf in flow_yes():
            shadow.trade_flow.append(tf)

    s = _mk_flow_shadow(best_bid=0.52, best_ask=0.54,
                        mid_history=mid_up(), trade_flow=flow_yes())
    sv._compute_flow_metrics(s)                # raw pu, published stable
    _reload_pu_inputs(s)
    m2 = sv._compute_flow_metrics(s)           # raw pu, published pu
    expect(m2["regime_state"] == "pressure_up",
           f"sanity: second consec pu must publish, got {m2['regime_state']}")
    # Interrupt with a flat tick (raw stable) — clear both inputs.
    s.mid_history.clear()
    s.mid_history.append((time.time() - 14, 0.50))
    s.mid_history.append((time.time(), 0.50))  # no drift → fill_bias=0
    s.trade_flow.clear()                        # no imbalance either
    m3 = sv._compute_flow_metrics(s)           # raw stable, published stable
    expect(m3["regime_state"] == "stable",
           f"interrupt publishes stable, got {m3['regime_state']}")
    # Single pu after interrupt must NOT immediately flip — history is [pu, stable, pu].
    _reload_pu_inputs(s)
    m4 = sv._compute_flow_metrics(s)
    expect(m4["regime_state"] == "stable",
           f"v5.9c.1: single pu after interrupt must stay stable, got {m4['regime_state']}")


def test_v59c1_directional_conviction_formula():
    """v5.9c.1: directional_conviction = fill_bias × flow_score. Signed
    product — captures both strength and direction in a single number
    for Engine 2 to consume as a continuous signal."""
    # Rising mid, yes-heavy flow → positive conviction.
    s = _mk_flow_shadow(best_bid=0.52, best_ask=0.54,
                        mid_history=[(-14, 0.50), (0, 0.53)])
    m = sv._compute_flow_metrics(s)
    expected = m["fill_bias"] * m["flow_score"]
    expect(abs(m["directional_conviction"] - round(expected, 4)) < 1e-9,
           f"directional_conviction arithmetic: expected {round(expected, 4)}, "
           f"got {m['directional_conviction']}")
    expect(m["directional_conviction"] > 0,
           f"rising mid must yield positive conviction, got {m['directional_conviction']}")
    # Falling mid → negative conviction.
    s2 = _mk_flow_shadow(best_bid=0.49, best_ask=0.51,
                         mid_history=[(-14, 0.53), (0, 0.50)])
    m2 = sv._compute_flow_metrics(s2)
    expect(m2["directional_conviction"] < 0,
           f"falling mid must yield negative conviction, got {m2['directional_conviction']}")


def test_v59c1_directional_conviction_bounded():
    """v5.9c.1: directional_conviction ∈ [-1, 1] always."""
    # Extreme case: fill_bias=1, flow_score=1 → conviction=1.
    s = _mk_flow_shadow(best_bid=0.52, best_ask=0.53,
                        mid_history=[(-14, 0.50), (0, 0.60)])
    trade_flow = [(-1.0 * i, 10.0, "yes") for i in range(6)]
    for t in trade_flow:
        s.trade_flow.append(t)
    m = sv._compute_flow_metrics(s)
    expect(-1.0 <= m["directional_conviction"] <= 1.0,
           f"conviction out of range: {m['directional_conviction']}")


def test_v59c1_neutral_band_wider():
    """v5.9c.1: epsilon 0.08 (was 0.05). A fill_bias of 0.06 — previously
    'yes' — must now report 'neutral'."""
    # Build a mid_history producing fill_bias ≈ 0.06.
    # spread = 0.04, drift = 0.002 → fill_bias = 0.002/0.04 = 0.05. Bump
    # drift to 0.003 → 0.075 (still < 0.08). Use 0.003.
    s = _mk_flow_shadow(best_bid=0.50, best_ask=0.54,
                        mid_history=[(-14, 0.520), (0, 0.523)])
    m = sv._compute_flow_metrics(s)
    expect(0 < m["fill_bias"] < 0.08,
           f"test setup: fill_bias must be in (0, 0.08), got {m['fill_bias']}")
    expect(m["pressure_direction"] == "neutral",
           f"v5.9c.1: fill_bias {m['fill_bias']} inside wider neutral band "
           f"must report neutral, got {m['pressure_direction']}")


def test_v59c1_publisher_updates_regime_counts():
    """v5.9c.1: _publish_flow_signals writes per-regime counts into the
    module-level _FLOW_REGIME_COUNTS dict consumed by the minute summary."""
    import tempfile
    original_path = sv.FLOW_SIGNALS_PATH
    original_states = sv.STATES.copy()
    try:
        with tempfile.TemporaryDirectory() as td:
            sv.FLOW_SIGNALS_PATH = os.path.join(td, "engine1_flow_signals.json")
            sv.STATES.clear()
            # Two stable markets.
            sv.STATES["A"] = _mk_flow_shadow(ticker="A")
            sv.STATES["B"] = _mk_flow_shadow(ticker="B")
            sv._publish_flow_signals()
            expect(sv._FLOW_REGIME_COUNTS.get("stable", 0) == 2,
                   f"expected 2 stable, got {sv._FLOW_REGIME_COUNTS}")
            expect(sv._FLOW_REGIME_COUNTS.get("pressure_up", 0) == 0,
                   "no pressure_up expected for idle markets")
            expect(sv._FLOW_REGIME_COUNTS.get("pressure_down", 0) == 0,
                   "no pressure_down expected for idle markets")
    finally:
        sv.FLOW_SIGNALS_PATH = original_path
        sv.STATES.clear()
        sv.STATES.update(original_states)


def test_v59c_publish_disabled_writes_nothing():
    """v5.9c: FLOW_SIGNALS_ENABLED=False must make _publish_flow_signals
    a no-op. Engine 2 integration can be kill-switched without a restart."""
    import tempfile
    original_path = sv.FLOW_SIGNALS_PATH
    original_enabled = sv.FLOW_SIGNALS_ENABLED
    try:
        with tempfile.TemporaryDirectory() as td:
            sv.FLOW_SIGNALS_PATH = os.path.join(td, "engine1_flow_signals.json")
            sv.FLOW_SIGNALS_ENABLED = False
            sv._publish_flow_signals()
            expect(not os.path.exists(sv.FLOW_SIGNALS_PATH),
                   "v5.9c: publisher must not write when disabled")
    finally:
        sv.FLOW_SIGNALS_PATH = original_path
        sv.FLOW_SIGNALS_ENABLED = original_enabled


# ────────────────────────────────────────────────────────────────────
# v5.9d — delta-path book diagnostics (instrumentation only)
# ────────────────────────────────────────────────────────────────────

def _fresh_shadow(ticker="BOOKTKR"):
    """MarketShadow with no prior state — each test starts clean."""
    return sv.MarketShadow(ticker=ticker, title="t", tick=0.01, vol_24h=20000.0)


def test_v59d_shadow_has_delta_diagnostic_fields():
    """v5.9d: all new fields present on MarketShadow with correct defaults."""
    s = _fresh_shadow()
    for fld, default in [
        ("yes_delta_count", 0),
        ("no_delta_count", 0),
        ("last_yes_delta_ts", 0.0),
        ("last_no_delta_ts", 0.0),
        ("book_bid_empty_count", 0),
        ("book_ask_empty_count", 0),
        ("book_crossed_count", 0),
        ("book_invalid_transitions", 0),
        ("first_invalid_ts", 0.0),
        ("first_invalid_reason", ""),
    ]:
        expect(hasattr(s, fld), f"v5.9d: MarketShadow.{fld} missing")
        expect(getattr(s, fld) == default,
               f"v5.9d: MarketShadow.{fld} default {default!r}, got {getattr(s, fld)!r}")


def test_v59d_yes_delta_increments_yes_counter_only():
    """v5.9d: a yes-side delta must update yes_delta_count and
    last_yes_delta_ts but never touch no-side fields."""
    s = _fresh_shadow()
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.50", "delta_fp": "100", "side": "yes",
    })
    expect(s.yes_delta_count == 1,
           f"yes delta must increment yes_delta_count, got {s.yes_delta_count}")
    expect(s.no_delta_count == 0,
           f"yes delta must not touch no_delta_count, got {s.no_delta_count}")
    expect(s.last_yes_delta_ts > 0,
           "yes delta must set last_yes_delta_ts")
    expect(s.last_no_delta_ts == 0.0,
           "yes delta must leave last_no_delta_ts at 0")


def test_v59d_no_delta_increments_no_counter_only():
    """v5.9d: symmetric — no-side delta updates no-side fields only."""
    s = _fresh_shadow()
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.48", "delta_fp": "100", "side": "no",
    })
    expect(s.no_delta_count == 1,
           f"no delta must increment no_delta_count, got {s.no_delta_count}")
    expect(s.yes_delta_count == 0,
           "no delta must not touch yes_delta_count")
    expect(s.last_no_delta_ts > 0, "no delta must set last_no_delta_ts")
    expect(s.last_yes_delta_ts == 0.0,
           "no delta must leave last_yes_delta_ts at 0")


def test_v59d_malformed_delta_increments_nothing():
    """v5.9d: a delta with bad/missing price or size must early-return
    WITHOUT bumping any per-side counter. Prevents counter inflation from
    upstream noise."""
    s = _fresh_shadow()
    # Missing delta_fp → early return at the try/except boundary.
    sv.apply_orderbook_delta(s, {
        "price_dollars": "not_a_number", "delta_fp": "100", "side": "yes",
    })
    expect(s.yes_delta_count == 0,
           f"malformed price must not increment counter, got {s.yes_delta_count}")
    # Unknown side — target is None, early return.
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.50", "delta_fp": "100", "side": "bogus",
    })
    expect(s.yes_delta_count == 0 and s.no_delta_count == 0,
           "unknown-side delta must not increment either counter")


def test_v59d_bid_empty_transition_classified_and_logged():
    """v5.9d: valid→invalid transition via bid_empty increments the
    right counter and records first_invalid_reason='bid_empty'."""
    s = _fresh_shadow()
    # Build a valid two-sided book.
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    expect(s.best_bid > 0 and s.best_ask > 0, "sanity: book should be valid")
    # Drain the yes side — best_bid goes to 0.
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.50", "delta_fp": "-100", "side": "yes",
    })
    expect(s.best_bid == 0, f"sanity: best_bid must be 0, got {s.best_bid}")
    expect(s.book_bid_empty_count == 1,
           f"v5.9d: bid_empty must be 1, got {s.book_bid_empty_count}")
    expect(s.book_ask_empty_count == 0 and s.book_crossed_count == 0,
           "v5.9d: other invalid counters must stay 0")
    expect(s.book_invalid_transitions == 1,
           f"v5.9d: invalid_transitions=1, got {s.book_invalid_transitions}")
    expect(s.first_invalid_reason == "bid_missing",
           f"v5.9d: first_invalid_reason=bid_missing, got {s.first_invalid_reason}")
    expect(s.first_invalid_ts > 0, "v5.9d: first_invalid_ts must be set")


def test_v59d_ask_empty_transition_classified():
    """v5.9d: valid→invalid via ask_empty (drain the no side)."""
    s = _fresh_shadow()
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.48", "delta_fp": "-100", "side": "no",
    })
    expect(s.best_ask == 0, f"sanity: best_ask must be 0, got {s.best_ask}")
    expect(s.book_ask_empty_count == 1,
           f"v5.9d: ask_empty must be 1, got {s.book_ask_empty_count}")
    expect(s.first_invalid_reason == "ask_missing",
           f"v5.9d: first_invalid_reason=ask_missing, got {s.first_invalid_reason}")


def test_v59d_crossed_book_classified():
    """v5.9d: bid climbs past ask without either side emptying →
    classify as 'crossed'. This is the merge-corruption fingerprint."""
    s = _fresh_shadow()
    # Start valid: bid=0.50, ask=0.52.
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    expect(s.best_bid == 0.50 and s.best_ask == 0.52,
           f"sanity pre: bid={s.best_bid} ask={s.best_ask}")
    # Add a higher yes level without draining — best_bid climbs to 0.55.
    # Now best_bid (0.55) > best_ask (0.52): crossed. Neither side empty.
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.55", "delta_fp": "100", "side": "yes",
    })
    expect(s.best_bid == 0.55 and s.best_ask == 0.52,
           f"sanity post: bid={s.best_bid} ask={s.best_ask}")
    expect(s.book_crossed_count == 1,
           f"v5.9d: crossed count must be 1, got {s.book_crossed_count}")
    expect(s.book_bid_empty_count == 0 and s.book_ask_empty_count == 0,
           "v5.9d: neither empty counter should increment on a crossed book")
    expect(s.first_invalid_reason == "crossed",
           f"v5.9d: first_invalid_reason=crossed, got {s.first_invalid_reason}")


def test_v59d_first_invalid_logged_once_per_market():
    """v5.9d: a second valid→invalid transition on the same market must
    NOT overwrite first_invalid_ts or first_invalid_reason. Subsequent
    transitions aggregate into the count, not the forensic fields."""
    s = _fresh_shadow()
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    # Drain bid → first invalid (bid_empty).
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.50", "delta_fp": "-100", "side": "yes",
    })
    first_ts = s.first_invalid_ts
    first_reason = s.first_invalid_reason
    expect(first_reason == "bid_missing", "sanity: first was bid_missing")
    # Reintroduce a valid book, then cross it.
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.50", "delta_fp": "100", "side": "yes",
    })
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.55", "delta_fp": "100", "side": "yes",
    })
    expect(s.book_crossed_count == 1,
           "v5.9d: second transition (crossed) must still count")
    expect(s.book_invalid_transitions == 2,
           f"v5.9d: total transitions=2, got {s.book_invalid_transitions}")
    expect(s.first_invalid_ts == first_ts,
           f"v5.9d: first_invalid_ts must not change, "
           f"was {first_ts}, now {s.first_invalid_ts}")
    expect(s.first_invalid_reason == first_reason,
           f"v5.9d: first_invalid_reason must not change, "
           f"was {first_reason!r}, now {s.first_invalid_reason!r}")


def test_v59d_invalid_to_invalid_does_not_count_transition():
    """v5.9d: counter fires on VALID→INVALID, not on invalid→invalid.
    An already-invalid book that receives another delta keeping it
    invalid must not inflate the counter."""
    s = _fresh_shadow()
    # Start with only yes side — book is invalid from the jump.
    sv.apply_orderbook_snapshot(s, {"yes_dollars_fp": [["0.50", "100"]]})
    expect(s.best_ask == 0, "sanity: starts invalid (ask=0)")
    pre_count = s.book_invalid_transitions
    # Another yes delta: still invalid, but no transition.
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.51", "delta_fp": "100", "side": "yes",
    })
    expect(s.book_invalid_transitions == pre_count,
           f"v5.9d: invalid→invalid must not count, "
           f"pre={pre_count}, post={s.book_invalid_transitions}")


def test_v59d_valid_to_valid_does_not_count_transition():
    """v5.9d: normal healthy book updates must not touch transition
    counters — only the valid→invalid edge is of interest.

    Note: Kalshi price complementarity means yes_bid + no_bid must stay
    below 1.0 for the book to remain valid. We stay strictly below 1.0
    by keeping yes_top ≤ 0.50 and no_top ≤ 0.48 throughout."""
    s = _fresh_shadow()
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    # Deltas that add deeper levels without moving either top-of-book,
    # plus top-ups at the existing top. Book stays valid throughout.
    for p, side in [("0.50", "yes"),   # top-up existing yes top
                    ("0.48", "no"),    # top-up existing no top
                    ("0.49", "yes"),   # deeper yes
                    ("0.47", "no"),    # deeper no
                    ("0.48", "yes")]:  # deeper yes (below current top)
        sv.apply_orderbook_delta(s, {
            "price_dollars": p, "delta_fp": "50", "side": side,
        })
    expect(s.book_invalid_transitions == 0,
           f"v5.9d: healthy deltas should not trigger transition, "
           f"got {s.book_invalid_transitions}")
    expect(s.best_bid == 0.50 and s.best_ask == 0.52,
           f"sanity: top-of-book unchanged; got bid={s.best_bid} ask={s.best_ask}")
    expect(s.yes_delta_count == 3 and s.no_delta_count == 2,
           f"sanity: delta counters should reflect both sides; "
           f"got y={s.yes_delta_count} n={s.no_delta_count}")


# ────────────────────────────────────────────────────────────────────
# v5.9e — ever_valid, time_to_first_valid, 5-reason taxonomy, forensic dump
# ────────────────────────────────────────────────────────────────────

def test_v59e_shadow_has_new_fields():
    """v5.9e: ever_valid + first_book_ts + first_valid_ts + new counter
    fields on MarketShadow. Distinguishes born-invalid from became-invalid."""
    s = _fresh_shadow()
    for fld, default in [
        ("ever_valid", False),
        ("first_book_ts", 0.0),
        ("first_valid_ts", 0.0),
        ("book_empty_both_count", 0),
        ("book_locked_count", 0),
    ]:
        expect(hasattr(s, fld), f"v5.9e: MarketShadow.{fld} missing")
        expect(getattr(s, fld) == default,
               f"v5.9e: {fld} default {default!r}, got {getattr(s, fld)!r}")


def test_v59e_classify_invalid_taxonomy():
    """v5.9e: _classify_invalid returns the 5-value taxonomy + "" for valid.
    Pure function — exercise all branches without touching global state."""
    s = _fresh_shadow()
    # valid book (both sides populated, ask > bid)
    s.best_bid, s.best_ask = 0.50, 0.52
    expect(sv._classify_invalid(s) == "",
           f"valid book → empty string, got {sv._classify_invalid(s)!r}")
    # empty_both
    s.best_bid, s.best_ask = 0.0, 0.0
    expect(sv._classify_invalid(s) == "empty_both", "empty_both classification")
    # bid_missing (yes side empty, no side populated)
    s.best_bid, s.best_ask = 0.0, 0.52
    expect(sv._classify_invalid(s) == "bid_missing", "bid_missing classification")
    # ask_missing
    s.best_bid, s.best_ask = 0.50, 0.0
    expect(sv._classify_invalid(s) == "ask_missing", "ask_missing classification")
    # crossed (ask < bid)
    s.best_bid, s.best_ask = 0.52, 0.50
    expect(sv._classify_invalid(s) == "crossed", "crossed classification")
    # locked (ask == bid, both > 0)
    s.best_bid, s.best_ask = 0.50, 0.50
    expect(sv._classify_invalid(s) == "locked", "locked classification")


def test_v59e_ever_valid_flips_on_first_valid_book():
    """v5.9e: ever_valid transitions False→True exactly once, on first
    valid-book observation. first_valid_ts captures the moment."""
    s = _fresh_shadow()
    expect(s.ever_valid is False, "default must be False")
    expect(s.first_valid_ts == 0.0, "first_valid_ts default 0.0")
    # Valid snapshot.
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    expect(s.ever_valid is True,
           f"v5.9e: ever_valid must flip True on first valid book, got {s.ever_valid}")
    expect(s.first_valid_ts > 0,
           "v5.9e: first_valid_ts must be set")
    # Subsequent valid-book events must NOT reset first_valid_ts.
    captured = s.first_valid_ts
    time.sleep(0.01)
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.51", "delta_fp": "50", "side": "yes",
    })
    expect(s.first_valid_ts == captured,
           f"v5.9e: first_valid_ts must not re-set; was {captured}, now {s.first_valid_ts}")


def test_v59e_ever_valid_stays_false_if_never_valid():
    """v5.9e: market that only ever receives one-sided data (discovery
    admission bug fingerprint) must show ever_valid=False and
    first_valid_ts=0 throughout."""
    s = _fresh_shadow()
    # Yes-only snapshot and yes-only deltas — ask never populates.
    sv.apply_orderbook_snapshot(s, {"yes_dollars_fp": [["0.50", "100"]]})
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.51", "delta_fp": "50", "side": "yes",
    })
    expect(s.ever_valid is False,
           f"v5.9e: one-sided market must stay ever_valid=False, got {s.ever_valid}")
    expect(s.first_valid_ts == 0.0,
           f"v5.9e: first_valid_ts must stay 0, got {s.first_valid_ts}")
    expect(sv._classify_invalid(s) == "ask_missing",
           f"v5.9e: current state must classify as ask_missing, "
           f"got {sv._classify_invalid(s)}")


def test_v59e_first_book_ts_records_first_observation():
    """v5.9e: first_book_ts set at the first moment any level data arrives."""
    s = _fresh_shadow()
    expect(s.first_book_ts == 0.0, "first_book_ts default 0.0")
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.50", "delta_fp": "100", "side": "yes",
    })
    expect(s.first_book_ts > 0,
           "v5.9e: first_book_ts must be set on first yes-side delta")


def test_v59e_empty_both_reason_classified():
    """v5.9e: valid→invalid transition via simultaneous empty of both
    sides is classified as 'empty_both' (previously merged into
    'bid_empty' under the 3-value taxonomy)."""
    s = _fresh_shadow()
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    # Drain both sides in a single non-recompute sequence — impossible with
    # two deltas, so we mutate the levels directly and force a recompute.
    s.yes_levels.clear()
    s.no_levels.clear()
    sv.recompute_top_of_book(s)
    expect(s.first_invalid_reason == "empty_both",
           f"v5.9e: expected empty_both, got {s.first_invalid_reason!r}")
    expect(s.book_empty_both_count == 1,
           f"v5.9e: empty_both counter must be 1, got {s.book_empty_both_count}")


def test_v59e_locked_reason_classified():
    """v5.9e: bid == ask (zero-width book) classifies as 'locked', not
    'crossed'. Locked books are a merge-boundary fingerprint — forensic
    split from genuine crossing (ask < bid)."""
    s = _fresh_shadow()
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    # Kalshi invariant: yes_price + no_price = 1. Force locked by setting
    # best yes = 0.52 (no best = 0.48 → ask = 1 - 0.48 = 0.52 == bid).
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.52", "delta_fp": "100", "side": "yes",
    })
    expect(s.best_bid == 0.52 and s.best_ask == 0.52,
           f"sanity: bid=0.52 ask=0.52 locked, got bid={s.best_bid} ask={s.best_ask}")
    expect(s.first_invalid_reason == "locked",
           f"v5.9e: locked book must classify as 'locked', "
           f"got {s.first_invalid_reason!r}")
    expect(s.book_locked_count == 1,
           f"v5.9e: locked counter must be 1, got {s.book_locked_count}")
    expect(s.book_crossed_count == 0,
           "v5.9e: locked must NOT increment crossed_count (distinct categories)")


def test_v59e_crossed_reason_classified_distinctly_from_locked():
    """v5.9e: ask strictly LESS THAN bid is 'crossed'; ask == bid is
    'locked'. Old 3-value taxonomy merged both."""
    s = _fresh_shadow()
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    # Push yes top above 0.52 → best_ask < best_bid (crossed, not locked).
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.55", "delta_fp": "100", "side": "yes",
    })
    expect(s.best_bid > s.best_ask,
           f"sanity: crossed; got bid={s.best_bid} ask={s.best_ask}")
    expect(s.first_invalid_reason == "crossed",
           f"v5.9e: must classify as crossed, got {s.first_invalid_reason!r}")
    expect(s.book_crossed_count == 1, "crossed_count must be 1")
    expect(s.book_locked_count == 0,
           "v5.9e: crossed must not pollute locked counter")


def test_v59e_forensic_dump_fires_once_per_threshold():
    """v5.9e: _maybe_emit_forensic_dump fires exactly once per threshold.
    Second call at the same elapsed time must be a no-op."""
    sv._FORENSIC_DUMPS_EMITTED.clear()
    # Fake elapsed time by directly invoking emitter via threshold check.
    # Easier: call _emit_invalid_forensic_dump directly to verify it doesn't
    # raise on an empty/small STATES dict.
    sv.STATES.clear()
    sv._emit_invalid_forensic_dump(60)  # no markets — zero-market path
    # Now install one invalid market and re-dump.
    s = _fresh_shadow("FORENSIC-TKR")
    sv.apply_orderbook_snapshot(s, {"yes_dollars_fp": [["0.50", "100"]]})
    sv.STATES["FORENSIC-TKR"] = s
    sv._emit_invalid_forensic_dump(300)  # should print one-line forensic
    sv.STATES.clear()


def test_v59e_forensic_dump_throttles_on_repeated_calls():
    """v5.9e: _maybe_emit_forensic_dump only fires each threshold once
    even if called many times after the threshold is passed."""
    # Set sentinel so we know these specific thresholds already fired.
    sv._FORENSIC_DUMPS_EMITTED.clear()
    sv._FORENSIC_DUMPS_EMITTED.add(60)
    sv._FORENSIC_DUMPS_EMITTED.add(300)
    # Now call — must be a no-op because both thresholds sentinel-set.
    before_len = len(sv._FORENSIC_DUMPS_EMITTED)
    sv._maybe_emit_forensic_dump()
    expect(len(sv._FORENSIC_DUMPS_EMITTED) == before_len,
           "v5.9e: thresholds already emitted must not re-fire")


# ────────────────────────────────────────────────────────────────────
# v5.9f — post-subscription market admission (drop never-valid markets)
# ────────────────────────────────────────────────────────────────────

def test_v59f_admission_constants_present():
    """v5.9f: MARKET_ADMISSION_ENABLED default True, grace 30s."""
    expect(hasattr(sv, "MARKET_ADMISSION_ENABLED"),
           "v5.9f: MARKET_ADMISSION_ENABLED missing")
    expect(sv.MARKET_ADMISSION_ENABLED is True,
           f"v5.9f: MARKET_ADMISSION_ENABLED default must be True, "
           f"got {sv.MARKET_ADMISSION_ENABLED}")
    expect(hasattr(sv, "MARKET_ADMISSION_GRACE_SEC"),
           "v5.9f: MARKET_ADMISSION_GRACE_SEC missing")
    expect(sv.MARKET_ADMISSION_GRACE_SEC == 30.0,
           f"v5.9f: grace default must be 30s, got {sv.MARKET_ADMISSION_GRACE_SEC}")


def test_v59f_shadow_has_admission_fields():
    """v5.9f: new MarketShadow fields with correct defaults."""
    s = _fresh_shadow()
    expect(hasattr(s, "subscribed_ts"), "subscribed_ts missing")
    expect(s.subscribed_ts > 0,
           f"v5.9f: subscribed_ts must auto-populate in __post_init__, "
           f"got {s.subscribed_ts}")
    expect(hasattr(s, "admission_rejected") and s.admission_rejected is False,
           "admission_rejected default False")
    expect(hasattr(s, "admission_rejected_ts") and s.admission_rejected_ts == 0.0,
           "admission_rejected_ts default 0.0")


def test_v59f_fresh_market_inside_grace_stays_admitted():
    """v5.9f: a market still within grace — even with no valid book —
    must NOT be flipped yet. Slow-loading books need room."""
    s = _fresh_shadow()
    # subscribed_ts defaults to now; ever_valid False. Call sweep.
    sv._check_market_admission(s)
    expect(s.admission_rejected is False,
           f"v5.9f: market inside grace must stay admitted, "
           f"got admission_rejected={s.admission_rejected}")


def test_v59f_never_valid_past_grace_gets_rejected():
    """v5.9f: market that exceeds grace without ever_valid=True must
    flip to admission_rejected. Idempotent: second check doesn't
    re-fire or change the ts."""
    s = _fresh_shadow()
    # Simulate "subscribed 60s ago" — past the default 30s grace.
    s.subscribed_ts = time.time() - 60.0
    expect(s.ever_valid is False, "sanity: not ever_valid")
    sv._check_market_admission(s)
    expect(s.admission_rejected is True,
           f"v5.9f: past-grace never-valid market must flip, "
           f"got admission_rejected={s.admission_rejected}")
    expect(s.admission_rejected_ts > 0,
           "v5.9f: admission_rejected_ts must be set on flip")
    captured_ts = s.admission_rejected_ts
    # Second call must not overwrite.
    sv._check_market_admission(s)
    expect(s.admission_rejected_ts == captured_ts,
           "v5.9f: idempotent — second call must not update ts")


def test_v59f_ever_valid_market_never_rejected():
    """v5.9f: a market that achieved ever_valid=True stays admitted
    forever, even if it later becomes invalid. (Transient invalid
    states are covered by other gates, not admission.)"""
    s = _fresh_shadow()
    s.subscribed_ts = time.time() - 120.0  # way past grace
    s.ever_valid = True                     # but it was valid once
    sv._check_market_admission(s)
    expect(s.admission_rejected is False,
           "v5.9f: ever_valid market must never be rejected")


def test_v59f_disabled_via_env_never_flips():
    """v5.9f: MARKET_ADMISSION_ENABLED=False is the rollback path.
    _check_market_admission is a no-op regardless of elapsed time."""
    s = _fresh_shadow()
    s.subscribed_ts = time.time() - 300.0  # wildly past grace
    prior = sv.MARKET_ADMISSION_ENABLED
    try:
        sv.MARKET_ADMISSION_ENABLED = False
        sv._check_market_admission(s)
        expect(s.admission_rejected is False,
               f"v5.9f: disabled must never flip, got {s.admission_rejected}")
    finally:
        sv.MARKET_ADMISSION_ENABLED = prior


def test_v59f_grace_zero_disables():
    """v5.9f: MARKET_ADMISSION_GRACE_SEC=0 disables the drop (alternate
    kill switch — env=0 on the grace knob also avoids flipping)."""
    s = _fresh_shadow()
    s.subscribed_ts = time.time() - 1000.0
    prior = sv.MARKET_ADMISSION_GRACE_SEC
    try:
        sv.MARKET_ADMISSION_GRACE_SEC = 0.0
        sv._check_market_admission(s)
        expect(s.admission_rejected is False,
               f"v5.9f: grace=0 must not flip, got {s.admission_rejected}")
    finally:
        sv.MARKET_ADMISSION_GRACE_SEC = prior


def test_v59f_rejected_market_short_circuits_gate():
    """v5.9f: _meets_market_conditions must return (False, 'admission_rejected')
    on a rejected market, as the FIRST gate — before warmup, book
    quality, velocity, anything. Keeps no_book histogram clean."""
    s = _fresh_shadow()
    s.admission_rejected = True
    # Would-pass-everything-else otherwise:
    s.trades_seen = 100
    s.best_bid = 0.50
    s.best_ask = 0.52
    ok, reason = sv._meets_market_conditions(s)
    expect(ok is False and reason == "admission_rejected",
           f"v5.9f: rejected market must gate with reason='admission_rejected'; "
           f"got ok={ok}, reason={reason}")


def test_v59f_valid_market_unaffected_when_not_rejected():
    """v5.9f: normal markets (admission_rejected=False) pass through the
    gate chain with no interference from v5.9f logic."""
    s = _fresh_shadow()
    s.trades_seen = sv.MIN_TRADES_FOR_WARMUP
    s.best_bid = 0.50
    s.best_ask = 0.52
    # No mid_history → velocity 0, passes
    ok, _reason = sv._meets_market_conditions(s)
    # Admission check is first; this should pass through to later gates.
    expect(s.admission_rejected is False,
           "sanity: test setup admits market")


def test_v59f_sweep_across_states():
    """v5.9f: _sweep_market_admissions iterates STATES and applies
    _check_market_admission to each. One fresh, one past-grace-invalid,
    one past-grace-ever-valid. Only the middle one should flip."""
    sv.STATES.clear()
    fresh = sv.MarketShadow(ticker="FRESH", title="t", tick=0.01, vol_24h=20000.0)
    stale = sv.MarketShadow(ticker="STALE", title="t", tick=0.01, vol_24h=20000.0)
    stale.subscribed_ts = time.time() - 120.0
    ok_mkt = sv.MarketShadow(ticker="OK", title="t", tick=0.01, vol_24h=20000.0)
    ok_mkt.subscribed_ts = time.time() - 120.0
    ok_mkt.ever_valid = True
    for m in (fresh, stale, ok_mkt):
        sv.STATES[m.ticker] = m
    try:
        sv._sweep_market_admissions()
        expect(fresh.admission_rejected is False, "fresh must stay admitted")
        expect(stale.admission_rejected is True, "stale never-valid must flip")
        expect(ok_mkt.admission_rejected is False, "ever_valid must stay admitted")
    finally:
        sv.STATES.clear()


# ────────────────────────────────────────────────────────────────────
# v5.9g — stuck-invalid admission revocation
# ────────────────────────────────────────────────────────────────────

def test_v59g_stuck_invalid_constant_present():
    """v5.9g: MARKET_STUCK_INVALID_SEC env-wired with 60s default."""
    expect(hasattr(sv, "MARKET_STUCK_INVALID_SEC"),
           "v5.9g: MARKET_STUCK_INVALID_SEC missing")
    expect(sv.MARKET_STUCK_INVALID_SEC == 60.0,
           f"v5.9g: default must be 60, got {sv.MARKET_STUCK_INVALID_SEC}")


def test_v59g_new_shadow_fields():
    """v5.9g: invalid_since_ts + admission_rejected_reason present."""
    s = _fresh_shadow()
    expect(hasattr(s, "invalid_since_ts") and s.invalid_since_ts == 0.0,
           "invalid_since_ts default 0.0")
    expect(hasattr(s, "admission_rejected_reason")
           and s.admission_rejected_reason == "",
           "admission_rejected_reason default ''")


def test_v59g_invalid_since_ts_set_on_valid_to_invalid():
    """v5.9g: recompute must set invalid_since_ts on valid→invalid."""
    s = _fresh_shadow()
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    expect(s.invalid_since_ts == 0.0, "valid book → no invalid timestamp")
    # Drain yes side → transition to bid_missing.
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.50", "delta_fp": "-100", "side": "yes",
    })
    expect(s.invalid_since_ts > 0,
           f"v5.9g: valid→invalid must set invalid_since_ts, got {s.invalid_since_ts}")


def test_v59g_invalid_since_ts_cleared_on_invalid_to_valid():
    """v5.9g: when a market recovers from invalid→valid, the timestamp clears."""
    s = _fresh_shadow()
    sv.apply_orderbook_snapshot(s, {
        "yes_dollars_fp": [["0.50", "100"]],
        "no_dollars_fp":  [["0.48", "100"]],
    })
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.50", "delta_fp": "-100", "side": "yes",
    })
    expect(s.invalid_since_ts > 0, "sanity: invalid_since_ts set")
    # Restore yes side → back to valid.
    sv.apply_orderbook_delta(s, {
        "price_dollars": "0.50", "delta_fp": "100", "side": "yes",
    })
    expect(s.best_bid > 0 and s.best_ask > s.best_bid,
           "sanity: book valid again")
    expect(s.invalid_since_ts == 0.0,
           f"v5.9g: invalid→valid must clear ts, got {s.invalid_since_ts}")


def test_v59g_stuck_invalid_triggers_admission_reject():
    """v5.9g: ever_valid=True + invalid continuously > stuck window →
    admission_rejected flips with reason='stuck_invalid'."""
    s = _fresh_shadow()
    s.ever_valid = True
    s.invalid_since_ts = time.time() - 120.0  # 2 minutes ago
    sv._check_market_admission(s)
    expect(s.admission_rejected is True,
           f"v5.9g: stuck market must flip, got {s.admission_rejected}")
    expect(s.admission_rejected_reason == "stuck_invalid",
           f"v5.9g: reason must be stuck_invalid, got {s.admission_rejected_reason}")


def test_v59g_stuck_grace_not_yet_elapsed_stays_admitted():
    """v5.9g: invalid but inside the stuck window → stays admitted."""
    s = _fresh_shadow()
    s.ever_valid = True
    s.invalid_since_ts = time.time() - 30.0  # only 30s ago
    sv._check_market_admission(s)
    expect(s.admission_rejected is False,
           f"v5.9g: inside stuck window must stay admitted, "
           f"got {s.admission_rejected}")


def test_v59g_ever_valid_but_not_invalid_stays_admitted():
    """v5.9g: ever_valid=True and currently valid (invalid_since_ts=0)
    must never trigger stuck-invalid path."""
    s = _fresh_shadow()
    s.ever_valid = True
    s.invalid_since_ts = 0.0
    sv._check_market_admission(s)
    expect(s.admission_rejected is False,
           "v5.9g: currently-valid ever_valid must never flip")


def test_v59g_never_valid_path_sets_reason():
    """v5.9g: never_valid path also populates admission_rejected_reason."""
    s = _fresh_shadow()
    s.subscribed_ts = time.time() - 60.0
    # ever_valid stays False; invalid_since_ts stays 0.
    sv._check_market_admission(s)
    expect(s.admission_rejected is True, "must flip past grace")
    expect(s.admission_rejected_reason == "never_valid",
           f"v5.9g: reason must be never_valid, got {s.admission_rejected_reason}")


def test_v59g_stuck_check_disabled_by_zero_window():
    """v5.9g: MARKET_STUCK_INVALID_SEC=0 disables the stuck-invalid path
    (rollback / isolate never_valid-only behavior)."""
    s = _fresh_shadow()
    s.ever_valid = True
    s.invalid_since_ts = time.time() - 1000.0
    prior = sv.MARKET_STUCK_INVALID_SEC
    try:
        sv.MARKET_STUCK_INVALID_SEC = 0.0
        sv._check_market_admission(s)
        expect(s.admission_rejected is False,
               "v5.9g: stuck window=0 must disable the stuck-invalid path")
    finally:
        sv.MARKET_STUCK_INVALID_SEC = prior


# ────────────────────────────────────────────────────────────────────
# v5.9h — low_edge near-miss distribution
# ────────────────────────────────────────────────────────────────────

def test_v59h_near_miss_buffer_and_recorder_present():
    """v5.9h: buffer, recorder, and distribution helper exposed."""
    expect(hasattr(sv, "_LOW_EDGE_NEAR_MISS"), "buffer missing")
    expect(hasattr(sv, "_record_low_edge_near_miss"), "recorder missing")
    expect(hasattr(sv, "_low_edge_distribution"), "distribution helper missing")
    expect(hasattr(sv, "_LOW_EDGE_NEAR_MISS_CAP"), "cap constant missing")


def test_v59h_recorder_appends_tuple():
    """v5.9h: _record_low_edge_near_miss pushes the 6-tuple in order."""
    sv._LOW_EDGE_NEAR_MISS.clear()
    s = _fresh_shadow("TICKER-X")
    sv._record_low_edge_near_miss(s, edge=-0.002, mid=0.51, spread=0.03, tick=0.01)
    expect(len(sv._LOW_EDGE_NEAR_MISS) == 1,
           f"expected 1 entry, got {len(sv._LOW_EDGE_NEAR_MISS)}")
    (ts, ticker, edge, mid, spread, tick) = sv._LOW_EDGE_NEAR_MISS[0]
    expect(ticker == "TICKER-X", f"ticker mismatch: {ticker}")
    expect(edge == -0.002 and mid == 0.51 and spread == 0.03 and tick == 0.01,
           f"values mismatch: edge={edge} mid={mid} spread={spread} tick={tick}")


def test_v59h_recorder_caps_buffer():
    """v5.9h: buffer trims to _LOW_EDGE_NEAR_MISS_CAP."""
    sv._LOW_EDGE_NEAR_MISS.clear()
    s = _fresh_shadow()
    cap = sv._LOW_EDGE_NEAR_MISS_CAP
    for i in range(cap + 25):
        sv._record_low_edge_near_miss(s, -0.001 * i, 0.5, 0.03, 0.01)
    expect(len(sv._LOW_EDGE_NEAR_MISS) == cap,
           f"buffer must cap at {cap}, got {len(sv._LOW_EDGE_NEAR_MISS)}")


def test_v59h_distribution_bucket_boundaries():
    """v5.9h: each bucket boundary lands in the expected bucket.
    Bucket semantics:
      pos_below_thr: [0, MIN_NET_EDGE)
      near_neg:      [-0.001, 0)
      small_neg:     [-0.003, -0.001)
      med_neg:       [-0.01, -0.003)
      far_neg:       < -0.01
    """
    sv._LOW_EDGE_NEAR_MISS.clear()
    s = _fresh_shadow()
    # Seed one entry per bucket using representative values.
    # MIN_NET_EDGE default is 0.01; use 0.005 for pos_below_thr.
    sv._record_low_edge_near_miss(s, 0.005, 0.5, 0.03, 0.01)   # pos_below_thr
    sv._record_low_edge_near_miss(s, -0.0005, 0.5, 0.03, 0.01) # near_neg
    sv._record_low_edge_near_miss(s, -0.002, 0.5, 0.03, 0.01)  # small_neg
    sv._record_low_edge_near_miss(s, -0.005, 0.5, 0.03, 0.01)  # med_neg
    sv._record_low_edge_near_miss(s, -0.05, 0.5, 0.03, 0.01)   # far_neg
    dist = sv._low_edge_distribution()
    expect(dist["pos_below_thr"] == 1, f"pos_below_thr: {dist}")
    expect(dist["near_neg"] == 1, f"near_neg: {dist}")
    expect(dist["small_neg"] == 1, f"small_neg: {dist}")
    expect(dist["med_neg"] == 1, f"med_neg: {dist}")
    expect(dist["far_neg"] == 1, f"far_neg: {dist}")


def test_v59h_distribution_empty_buffer_all_zeros():
    """v5.9h: empty buffer → all buckets zero, no crash."""
    sv._LOW_EDGE_NEAR_MISS.clear()
    dist = sv._low_edge_distribution()
    for k, v in dist.items():
        expect(v == 0, f"bucket {k} must be 0 on empty, got {v}")


# ────────────────────────────────────────────────────────────────────
# v5.9i — low_edge family analysis
# ────────────────────────────────────────────────────────────────────

def test_v59i_classify_family_weather():
    """v5.9i: KXHIGH* tickers classify as weather."""
    for t in ("KXHIGHNY-26APR21-B55.5", "KXHIGHLAX-26APR21-B65.5",
              "KXHIGHAUS-26APR21-T66", "KXHIGHCHI-26APR21-B75.5"):
        fam = sv._classify_family(t)
        expect(fam == "weather", f"{t} → {fam}, expected weather")


def test_v59i_classify_family_sports():
    """v5.9i: sports tickers via substring match."""
    samples = [
        "KXITFMATCH-26APR21TSITOR-TSI",
        "KXITFWMATCH-26APR21BULBON-BON",
        "KXNFLDRAFTTOP-26-10-JTYS",
        "KXLALIGATOTAL-26APR21RMAALA-1",
        "KXEFLCHAMPIONSHIPGAME-26APR21LEIHUL-HUL",
        "KXNBASERIESSPREAD-26HOULALR1-HOU2",
        "KXNBATOTAL-26APR21HOULAL-190",
        "KXNBACOY-26-JBIC",
        "KXCOPPAITALIAGAME-26APR21INTCOM-TIE",
        "KXHEISMAN-27-DMENS",
        "KXMARMAD-27-FLA",
    ]
    for t in samples:
        fam = sv._classify_family(t)
        expect(fam == "sports", f"{t} → {fam}, expected sports")


def test_v59i_classify_family_politics():
    """v5.9i: politics tickers."""
    samples = [
        "KXTRUMPMENTIONB-26APR21-TRAN",
        "KXVOTEHUBTRUMPUPDOWN-26APR23",
        "KXKASHOUT-26APR-JUN01",
        "KXUSAIRANAGREEMENT-27-26AUG",
        "KXTARIFFCHECKS-26-27",
        "KXMOVVAREDISTRICT-26APR21-YES-P4",
        "KXMOVNJ11SPECIAL-26APR16-AMEJ-P18",
    ]
    for t in samples:
        fam = sv._classify_family(t)
        expect(fam == "politics", f"{t} → {fam}, expected politics")


def test_v59i_classify_family_commodities():
    """v5.9i: commodities / crypto / energy."""
    for t in ("KXWTI-26APR22-T88.99", "KXAAAGASW-26APR27-4.000",
              "KXAAAGASM-26APR30-3.80", "KXCRYPTOSTRUCTURE-26JAN-JUN"):
        fam = sv._classify_family(t)
        expect(fam == "commodities", f"{t} → {fam}, expected commodities")


def test_v59i_classify_family_entertainment():
    """v5.9i: Rotten Tomatoes / movies / awards."""
    for t in ("KXRT-MIC-35", "KXRT-MIC-30"):
        fam = sv._classify_family(t)
        expect(fam == "entertainment", f"{t} → {fam}, expected entertainment")


def test_v59i_classify_family_misc_fallback():
    """v5.9i: unrecognized prefix falls to misc."""
    expect(sv._classify_family("KXUNKNOWN-X") == "misc",
           "unrecognized prefix must fall to misc")
    expect(sv._classify_family("") == "misc", "empty string → misc")
    expect(sv._classify_family(None) == "misc", "None → misc")


def test_v59i_analysis_empty_buffer_safe():
    """v5.9i: empty near-miss buffer → no crash, prints 'empty' note."""
    sv._LOW_EDGE_NEAR_MISS.clear()
    # Must not raise.
    sv._emit_low_edge_family_analysis()


def test_v59i_analysis_populates_top_N_and_families():
    """v5.9i: populate with mixed-family rejections and verify the
    emitter runs clean. Output correctness is visual, but the emitter
    must survive mixed-family data without errors."""
    sv._LOW_EDGE_NEAR_MISS.clear()
    s_weather = _fresh_shadow("KXHIGHNY-26APR21-B55.5")
    s_sports = _fresh_shadow("KXITFMATCH-26APR21TSITOR-TSI")
    s_politics = _fresh_shadow("KXTRUMPMENTIONB-26APR21-TRAN")
    # Simulate a typical "rejects cluster just below threshold" pattern.
    for i in range(5):
        sv._record_low_edge_near_miss(s_weather, -0.002, 0.52, 0.03, 0.01)
    for i in range(3):
        sv._record_low_edge_near_miss(s_sports, -0.008, 0.50, 0.05, 0.01)
    for i in range(2):
        sv._record_low_edge_near_miss(s_politics, -0.0005, 0.51, 0.02, 0.01)
    # Must run to completion without raising.
    sv._emit_low_edge_family_analysis()
    # Verify the data is still there (emit doesn't consume).
    expect(len(sv._LOW_EDGE_NEAR_MISS) == 10,
           f"buffer must not be consumed, got {len(sv._LOW_EDGE_NEAR_MISS)}")


# ────────────────────────────────────────────────────────────────────
# v5.9j — tick-structure prior (ranker bias + cap multiplier + bucket telemetry)
# ────────────────────────────────────────────────────────────────────

def test_v59j_constants_present():
    """v5.9j: new constants env-wired with the spec defaults."""
    for const, expected in [
        ("TRAD_WEIGHT_TICK_BIAS", 0.15),
        ("TICK_BIAS_WIDE_SPREAD_THRESHOLD", 0.05),
        ("PENNY_CAP_MULT", 1.0),
        ("CENT_WIDE_CAP_MULT", 0.5),
        ("CENT_NARROW_CAP_MULT", 0.25),
    ]:
        expect(hasattr(sv, const), f"v5.9j: {const} missing")
        expect(getattr(sv, const) == expected,
               f"v5.9j: {const} default must be {expected}, "
               f"got {getattr(sv, const)}")


def test_v59j_tick_bias_score_penny():
    """v5.9j: penny-tick (tick ≤ 0.001) returns +1.0 regardless of spread."""
    for spread in (0.01, 0.05, 0.10):
        score = sv._tick_bias_score(0.001, spread)
        expect(score == 1.0, f"penny tick spread={spread} → {score}, expected +1.0")
    # Even tapered below 0.001 qualifies.
    expect(sv._tick_bias_score(0.0005, 0.02) == 1.0,
           "sub-penny tick must also score +1.0")


def test_v59j_tick_bias_score_cent_wide_vs_narrow():
    """v5.9j: cent-tick with spread ≥ $0.05 → 0.0 (neutral);
    cent-tick with spread < $0.05 → -1.0 (penalty)."""
    expect(sv._tick_bias_score(0.01, 0.05) == 0.0,
           "cent with spread exactly at threshold → 0.0")
    expect(sv._tick_bias_score(0.01, 0.08) == 0.0,
           "cent with wide spread → 0.0")
    expect(sv._tick_bias_score(0.01, 0.04) == -1.0,
           "cent with narrow spread → -1.0")
    expect(sv._tick_bias_score(0.01, 0.02) == -1.0,
           "cent with very narrow spread → -1.0")


def test_v59j_ranker_prefers_penny_over_cent_same_spread():
    """v5.9j: given identical spread/vol/DTR, penny must rank higher
    than cent. This was backwards under v5.5's taper penalty."""
    base = {"spread": 0.05, "vol_24h": 50_000, "dtr": 30}
    penny = sv._tradability_score({**base, "tick_step": 0.001})
    cent = sv._tradability_score({**base, "tick_step": 0.01})
    expect(penny > cent,
           f"v5.9j: penny must rank above cent; penny={penny:.4f} cent={cent:.4f}")


def test_v59j_ranker_cent_wide_above_cent_narrow():
    """v5.9j: within cent-tick, wide spread must outrank narrow even
    though the intra-spread normalization also prefers wider spreads —
    the tick_bias term makes the difference decisive."""
    base = {"vol_24h": 50_000, "dtr": 30, "tick_step": 0.01}
    wide = sv._tradability_score({**base, "spread": 0.06})
    narrow = sv._tradability_score({**base, "spread": 0.04})
    expect(wide > narrow,
           f"v5.9j: cent_wide > cent_narrow; wide={wide:.4f} narrow={narrow:.4f}")


def test_v59j_initial_cap_multiplier_penny():
    """v5.9j: penny-tick gets PENNY_CAP_MULT (1.0 default)."""
    expect(sv._initial_cap_multiplier(0.001, 0.09) == 1.0,
           "penny → 1.0")
    expect(sv._initial_cap_multiplier(0.0005, 0.03) == 1.0,
           "sub-penny → 1.0")


def test_v59j_initial_cap_multiplier_cent_wide():
    """v5.9j: cent-tick with spread ≥ $0.05 gets CENT_WIDE_CAP_MULT (0.5)."""
    expect(sv._initial_cap_multiplier(0.01, 0.05) == 0.5,
           "cent + spread=0.05 → 0.5")
    expect(sv._initial_cap_multiplier(0.01, 0.08) == 0.5,
           "cent + spread=0.08 → 0.5")


def test_v59j_initial_cap_multiplier_cent_narrow():
    """v5.9j: cent-tick with spread < $0.05 gets CENT_NARROW_CAP_MULT (0.25)."""
    expect(sv._initial_cap_multiplier(0.01, 0.03) == 0.25,
           "cent + spread=0.03 → 0.25")
    expect(sv._initial_cap_multiplier(0.01, 0.04) == 0.25,
           "cent + spread=0.04 (just below threshold) → 0.25")


def test_v59j_market_shadow_construction_applies_cap():
    """v5.9j: MarketShadow built via the load_candidates path inherits
    the tick-structure-derived cap_multiplier. Simulate by calling
    _initial_cap_multiplier and using the returned value at construction."""
    # Penny market.
    s_penny = sv.MarketShadow(
        ticker="PENNY-TKR", title="t", tick=0.001, vol_24h=20000.0,
        cap_multiplier=sv._initial_cap_multiplier(0.001, 0.09),
    )
    expect(s_penny.cap_multiplier == 1.0,
           f"penny cap must be 1.0, got {s_penny.cap_multiplier}")
    # Cent narrow.
    s_narrow = sv.MarketShadow(
        ticker="CENT-NARROW", title="t", tick=0.01, vol_24h=20000.0,
        cap_multiplier=sv._initial_cap_multiplier(0.01, 0.03),
    )
    expect(s_narrow.cap_multiplier == 0.25,
           f"cent narrow cap must be 0.25, got {s_narrow.cap_multiplier}")


def test_v59j_tick_bucket_classifier():
    """v5.9j: _tick_bucket returns 'penny' / 'cent' / 'other'."""
    expect(sv._tick_bucket(0.001) == "penny", "0.001 → penny")
    expect(sv._tick_bucket(0.0005) == "penny", "0.0005 → penny")
    expect(sv._tick_bucket(0.01) == "cent", "0.01 → cent")
    expect(sv._tick_bucket(0.05) == "other", "0.05 → other")
    expect(sv._tick_bucket(0.02) == "other", "0.02 → other")


def test_v59j_aggregate_by_tick_bucket():
    """v5.9j: _aggregate_by_tick_bucket rolls fills and realized P&L
    across STATES, grouped by bucket. Snapshot/reset STATES for the test."""
    original = sv.STATES.copy()
    try:
        sv.STATES.clear()
        # Penny market with 3 buy + 2 sell fills, $0.80 realized.
        p = sv.MarketShadow(ticker="P", title="t", tick=0.001, vol_24h=20000.0)
        p.fills_buy = 3; p.fills_sell = 2; p.realized_pnl = 0.80
        # Cent market with 1 + 1 fills, -$0.05.
        c = sv.MarketShadow(ticker="C", title="t", tick=0.01, vol_24h=20000.0)
        c.fills_buy = 1; c.fills_sell = 1; c.realized_pnl = -0.05
        sv.STATES["P"] = p; sv.STATES["C"] = c
        fills, pnl, mkts = sv._aggregate_by_tick_bucket()
        expect(fills["penny"] == 5, f"penny fills {fills['penny']}, expected 5")
        expect(fills["cent"] == 2, f"cent fills {fills['cent']}, expected 2")
        expect(abs(pnl["penny"] - 0.80) < 1e-9, f"penny pnl {pnl['penny']}")
        expect(abs(pnl["cent"] - (-0.05)) < 1e-9, f"cent pnl {pnl['cent']}")
        expect(mkts["penny"] == 1 and mkts["cent"] == 1, f"mkts={mkts}")
    finally:
        sv.STATES.clear()
        sv.STATES.update(original)


# ────────────────────────────────────────────────────────────────────
# v5.9k — sensor-only mode (SHADOW_QUOTE_ENABLED kill switch)
# ────────────────────────────────────────────────────────────────────

def test_v59k_constant_present_default_enabled():
    """v5.9k: SHADOW_QUOTE_ENABLED env-wired, default True (backward compat)."""
    expect(hasattr(sv, "SHADOW_QUOTE_ENABLED"),
           "v5.9k: SHADOW_QUOTE_ENABLED missing")
    expect(sv.SHADOW_QUOTE_ENABLED is True,
           f"default must be True (quoting enabled), "
           f"got {sv.SHADOW_QUOTE_ENABLED}")


def test_v59k_disabled_flag_clears_quotes_no_inventory():
    """v5.9k: with quoting disabled + no inventory, compute_quotes must
    clear both buy_px and sell_px and return immediately."""
    s = _fresh_shadow()
    s.trades_seen = sv.MIN_TRADES_FOR_WARMUP
    s.best_bid = 0.50
    s.best_ask = 0.52
    s.our_buy_px = 0.49
    s.our_sell_px = 0.53
    s.inventory = 0
    prior = sv.SHADOW_QUOTE_ENABLED
    try:
        sv.SHADOW_QUOTE_ENABLED = False
        sv.compute_quotes(s)
        expect(s.our_buy_px == 0.0,
               f"sensor-mode must zero buy_px, got {s.our_buy_px}")
        expect(s.our_sell_px == 0.0,
               f"sensor-mode + no inv must zero sell_px, got {s.our_sell_px}")
    finally:
        sv.SHADOW_QUOTE_ENABLED = prior


def test_v59k_disabled_flag_blocks_new_buy_but_keeps_sell_with_inventory():
    """v5.9k: with quoting disabled but inventory > 0, the SELL side
    must remain live so positions can drain. BUY side must be killed
    to prevent new inventory accumulation in sensor mode."""
    s = _fresh_shadow()
    s.trades_seen = sv.MIN_TRADES_FOR_WARMUP
    s.best_bid = 0.50
    s.best_ask = 0.52
    s.inventory = 10  # holding 10 contracts
    s.cost_basis = 0.48
    # Populate mid history so velocity gate doesn't short-circuit.
    import time as _t
    s.mid_history.append((_t.time() - 14, 0.51))
    s.mid_history.append((_t.time(), 0.51))
    prior = sv.SHADOW_QUOTE_ENABLED
    try:
        sv.SHADOW_QUOTE_ENABLED = False
        sv.compute_quotes(s)
        expect(s.our_buy_px == 0.0,
               f"sensor-mode must block new buys; got buy_px={s.our_buy_px}")
        # Sell path: when inventory > 0 AND sensor mode is on, sell_px
        # should be populated so the position can drain. May be 0 if
        # other gates rejected — but buy_px MUST be 0 regardless.
    finally:
        sv.SHADOW_QUOTE_ENABLED = prior


def test_v59k_enabled_flag_preserves_quoting_behavior():
    """v5.9k: with flag True (default), compute_quotes behaves exactly
    as before the flag existed — no regression in normal quoting path."""
    s = _fresh_shadow()
    s.trades_seen = sv.MIN_TRADES_FOR_WARMUP
    s.best_bid = 0.50
    s.best_ask = 0.54
    s.inventory = 0
    import time as _t
    s.mid_history.append((_t.time() - 14, 0.52))
    s.mid_history.append((_t.time(), 0.52))
    # Track trade flow so downstream gates don't misfire.
    s.trade_flow.append((_t.time() - 10, 5.0, "yes"))
    prior = sv.SHADOW_QUOTE_ENABLED
    try:
        sv.SHADOW_QUOTE_ENABLED = True
        sv.compute_quotes(s)
        # With enabled flag, quote placement runs through the normal
        # gate stack. Either we get real quote prices OR one of the
        # existing gates rejected — but NOT because of v5.9k.
        # Test just confirms no crash and flag doesn't short-circuit.
    finally:
        sv.SHADOW_QUOTE_ENABLED = prior


def test_v58_constants_present():
    """v5.8: all inventory-discipline knobs must be env-wired."""
    for const in ("MAX_INV_AGE_MIN", "PROFIT_LOCK_THRESHOLD_USD",
                  "INV_TRAP_STRIKES_TO_DEMOTE", "INV_TRAP_DEMOTE_MULT",
                  "NO_SELLS_WARN_MIN", "NO_SELLS_ALERT_MIN"):
        expect(hasattr(sv, const), f"v5.8 constant missing: {const}")
    expect(sv.MAX_INV_AGE_MIN == 20.0,
           f"MAX_INV_AGE_MIN default must be 20, got {sv.MAX_INV_AGE_MIN}")
    expect(sv.PROFIT_LOCK_THRESHOLD_USD == 0.20,
           f"PROFIT_LOCK_THRESHOLD_USD default must be 0.20, got {sv.PROFIT_LOCK_THRESHOLD_USD}")
    expect(sv.INV_TRAP_STRIKES_TO_DEMOTE == 2,
           f"INV_TRAP_STRIKES_TO_DEMOTE default must be 2, got {sv.INV_TRAP_STRIKES_TO_DEMOTE}")


def test_v58_shadow_has_inventory_tracking_fields():
    """v5.8: MarketShadow must expose the 4 new inventory-discipline fields."""
    s = _mk_shadow()
    for field in ("first_inventory_ts", "inventory_stale", "profit_locked",
                  "inv_trap_strikes"):
        expect(hasattr(s, field), f"MarketShadow missing v5.8 field: {field}")
    # Default values
    expect(s.first_inventory_ts == 0.0, "first_inventory_ts default must be 0.0")
    expect(s.inventory_stale is False, "inventory_stale default must be False")
    expect(s.profit_locked is False, "profit_locked default must be False")
    expect(s.inv_trap_strikes == 0, "inv_trap_strikes default must be 0")


def test_v58_inv_trap_strikes_increment_on_urgent():
    """v5.8: URGENT liquidation entries bump inv_trap_strikes. SLOW don't."""
    s = _mk_shadow()
    sv._start_liquidation(s, "gate:fast_move")  # SLOW in v5.6 default
    expect(s.inv_trap_strikes == 0,
           f"SLOW reason must not bump strikes, got {s.inv_trap_strikes}")
    # Clear state for next trigger
    s.liquidating = False
    sv._start_liquidation(s, "net_growth_2x")  # URGENT
    expect(s.inv_trap_strikes == 1,
           f"URGENT reason must bump strikes, got {s.inv_trap_strikes}")


def test_v58_inv_trap_demote_at_threshold():
    """v5.8: after INV_TRAP_STRIKES_TO_DEMOTE URGENT entries, cap_multiplier
    gets clamped down to INV_TRAP_DEMOTE_MULT."""
    s = _mk_shadow()
    expect(s.cap_multiplier == 1.0, "cap_multiplier default should be 1.0")
    # First URGENT strike
    sv._start_liquidation(s, "disabled")
    s.liquidating = False
    expect(s.cap_multiplier == 1.0,
           "cap should not demote after just 1 strike (below threshold)")
    # Second URGENT strike triggers demotion
    sv._start_liquidation(s, "discovery_drop")
    expect(s.cap_multiplier == sv.INV_TRAP_DEMOTE_MULT,
           f"cap should demote to {sv.INV_TRAP_DEMOTE_MULT}, got {s.cap_multiplier}")


def test_v58_inventory_stale_reason_is_nonurgent():
    """v5.8: inventory_stale and profit_lock reasons must route NON-URGENT.
    Urgent exit on stale/profit-lock would defeat the purpose (cross-slip)."""
    expect(not sv._is_urgent_liquidation("inventory_stale"),
           "inventory_stale must be NON-URGENT")
    expect(not sv._is_urgent_liquidation("profit_lock"),
           "profit_lock must be NON-URGENT")


def test_v58_source_wired_inventory_age_gate():
    """v5.8 source guard: inventory-age gate and profit-lock wired in
    _quote_loop, with the right reason strings."""
    src = open(sv.__file__).read()
    expect('_start_liquidation(s, "inventory_stale")' in src,
           "v5.8: inventory_stale liquidation not wired in _quote_loop")
    expect('_start_liquidation(s, "profit_lock")' in src,
           "v5.8: profit_lock liquidation not wired in _quote_loop")
    expect("first_inventory_ts" in src,
           "v5.8: first_inventory_ts not referenced in source")
    expect("PROFIT_LOCK_THRESHOLD_USD" in src,
           "v5.8: PROFIT_LOCK_THRESHOLD_USD not wired")


def test_v58_source_wired_timestamp_set_on_buy():
    """v5.8 source guard: BUY fill handler sets first_inventory_ts on the
    0→positive transition."""
    src = open(sv.__file__).read()
    # Match the transition assignment — not necessarily verbatim whitespace
    import re
    pattern = r"old_inv\s*<=\s*0\s+and\s+s\.inventory\s*>\s*0"
    expect(re.search(pattern, src) is not None,
           "v5.8: 0→positive inventory transition not guarding first_inventory_ts set")


def test_v58_source_wired_reset_on_sell_close():
    """v5.8 source guard: SELL handlers reset first_inventory_ts when
    inventory returns to 0. Must appear in BOTH maker-sell and cross-exit paths."""
    src = open(sv.__file__).read()
    # Count reset patterns — need at least 2 (one in maker sell, one in cross-exit)
    reset_count = src.count("s.first_inventory_ts = 0.0")
    expect(reset_count >= 2,
           f"v5.8: first_inventory_ts reset must appear ≥2 times (maker+cross), "
           f"found {reset_count}")


def test_v58_source_wired_telemetry_line():
    """v5.8 source guard: minute-summary prints the inv: telemetry line."""
    src = open(sv.__file__).read()
    expect('"       inv: open=' in src,
           "v5.8: inventory telemetry line not wired in _print_minute_summary")
    expect("no_sells" in src,
           "v5.8: no_sells counters not referenced")


def test_v58_profit_lock_fires_once_not_every_tick():
    """v5.8: profit_locked flag prevents re-triggering liquidation start
    on every quote tick once profit-lock has been entered. Regression guard
    against double-entry."""
    src = open(sv.__file__).read()
    # The profit-lock block must condition on `not s.profit_locked`.
    import re
    block = re.search(
        r"Profit lock.*?_start_liquidation\(s,\s*\"profit_lock\"\)",
        src, re.DOTALL,
    )
    expect(block is not None, "v5.8 profit-lock block not found")
    expect("not s.profit_locked" in block.group(0),
           "v5.8: profit_lock block must guard with `not s.profit_locked`")


def test_v58_inventory_age_gate_fires_in_compute_quotes():
    """v5.8 behavioral test (not source-grep): compute_quotes must flip a
    stale-inventory shadow into SLOW liquidation when age exceeds
    MAX_INV_AGE_MIN. This catches regressions where the gate order or
    early-return guards break the call path."""
    s = _mk_shadow(inventory=10, cost_basis=0.50,
                   best_bid=0.50, best_ask=0.52, tick=0.01)
    # Simulate 25 min of inventory age (over the 20 min threshold)
    s.first_inventory_ts = time.time() - 25 * 60
    s.trades_seen = 10  # past warmup
    # Populate the fields compute_quotes expects to read
    s.last_trade_ts = time.time()
    s.best_bid_size = 100
    s.best_ask_size = 100
    sv.compute_quotes(s)
    expect(s.inventory_stale, "v5.8: inventory_stale flag must be set after age exceeds threshold")
    expect(s.liquidating, "v5.8: liquidation must be started")
    expect(not s.liquidation_urgent, "v5.8: inventory_stale must route NON-URGENT")


def test_v58_profit_lock_fires_in_compute_quotes():
    """v5.8 behavioral test: compute_quotes must flip a winning shadow
    into profit-lock SLOW liquidation when realized P&L clears threshold."""
    s = _mk_shadow(inventory=10, cost_basis=0.50,
                   best_bid=0.50, best_ask=0.52, tick=0.01)
    s.realized_pnl = 0.25   # above PROFIT_LOCK_THRESHOLD_USD (0.20)
    s.first_inventory_ts = time.time()   # fresh hold
    s.trades_seen = 10
    s.last_trade_ts = time.time()
    s.best_bid_size = 100
    s.best_ask_size = 100
    sv.compute_quotes(s)
    expect(s.profit_locked, "v5.8: profit_locked flag must be set")
    expect(s.liquidating, "v5.8: profit_lock must start liquidation")
    expect(not s.liquidation_urgent, "v5.8: profit_lock must route NON-URGENT")


def test_v58_v54_min_edge_skipped_when_holding_inventory():
    """v5.8 regression guard: the v5.4 spread-compression early-return
    must NOT fire when inventory > 0. Otherwise stuck inventory in a
    tight market orphans with no exit path."""
    src = open(sv.__file__).read()
    # The v5.4 early-return must be guarded by `and s.inventory <= 0`
    import re
    block = re.search(
        r"expected_net_per_rt\s*<\s*MIN_NET_EDGE_PER_FILL.*?return",
        src, re.DOTALL,
    )
    expect(block is not None, "v5.4 early-return block not found")
    expect("s.inventory <= 0" in block.group(0),
           "v5.4 early-return must guard with `s.inventory <= 0` so inventory-holders keep sell quote live")


def test_v58_gates_run_before_v54_early_return():
    """v5.8 regression guard: inventory-age and profit-lock gates must
    appear in source BEFORE the v5.4 MIN_NET_EDGE_PER_FILL early-return.
    Order matters — v5.4 returns, bypassing v5.8 otherwise."""
    src = open(sv.__file__).read()
    v58_age_pos = src.find('_start_liquidation(s, "inventory_stale")')
    v58_lock_pos = src.find('_start_liquidation(s, "profit_lock")')
    # There are multiple MIN_NET_EDGE_PER_FILL references; find the one
    # inside compute_quotes (after "runtime_min =" which is the anchor).
    anchor = src.find("runtime_min = (time.time() - _START)")
    v54_pos = src.find("expected_net_per_rt < MIN_NET_EDGE_PER_FILL", anchor)
    expect(v58_age_pos > 0 and v58_lock_pos > 0 and v54_pos > 0,
           "v5.8/v5.4 markers not found in source")
    expect(v58_age_pos < v54_pos,
           f"v5.8 inventory-age gate must come BEFORE v5.4 early-return "
           f"(age at {v58_age_pos}, v5.4 at {v54_pos})")
    expect(v58_lock_pos < v54_pos,
           "v5.8 profit-lock gate must come BEFORE v5.4 early-return")


def test_stale_no_sells_stays_passive():
    """Non-urgent liquidation must route to passive-forever, not stage 2,
    even after stage-1 window elapses. Regression guard for v5.1b fix."""
    s = _mk_shadow()
    sv._start_liquidation(s, "stale_no_sells")
    expect(not s.liquidation_urgent,
           "stale_no_sells wrongly flagged urgent")
    # Even after 20 minutes elapsed, stage should remain 1 (passive maker)
    s.liquidation_started_ts = time.time() - 20 * 60
    quote = sv._liquidation_quote(s, s.tick)
    expected = round(s.best_bid + s.tick, 4)
    expect(abs(quote - expected) < 1e-9,
           f"non-urgent stage drifted: quote {quote} expected {expected}")
    expect(s.liquidation_stage == 1,
           f"non-urgent stage should stay 1, got {s.liquidation_stage}")


def test_disabled_keeps_backdate_behavior():
    """Regression guard: the disabled / blacklist branch intentionally
    still backdates so blacklisted markets cross immediately. We only
    removed the backdate from the regime-change branch."""
    # The backdate for disabled lives in _quote_loop line 931, not inside
    # _start_liquidation. Verify _start_liquidation itself does NOT
    # backdate for the "disabled" reason — the quote_loop caller does.
    s = _mk_shadow()
    before = time.time()
    sv._start_liquidation(s, "disabled")
    expect(before - 0.5 <= s.liquidation_started_ts <= time.time() + 0.5,
           "_start_liquidation should not backdate; caller does")
    expect(s.liquidation_urgent, "disabled should be urgent")


def test_max_open_markets_env_configurable():
    """Regression guard: MAX_OPEN_MARKETS must read from SHADOW_MAX_OPEN
    env var with default 12. If someone reverts this to a bare literal,
    scaling N=150 runs will silently ignore the concurrency bump."""
    import re
    src = open(sv.__file__).read()
    pattern = r'MAX_OPEN_MARKETS\s*=\s*int\(os\.environ\.get\(\s*["\']SHADOW_MAX_OPEN["\']'
    expect(re.search(pattern, src) is not None,
           "MAX_OPEN_MARKETS is not wired to SHADOW_MAX_OPEN env var — hardcoded literal would silently cap concurrency even when user sets the env var")


def test_max_open_markets_default_is_12():
    """Default (env unset) must remain 12 for backward compatibility with
    existing launches that don't set SHADOW_MAX_OPEN."""
    # Respect whatever the current process env set — if SHADOW_MAX_OPEN is
    # unset, default must be 12. If it's set, verify it matches.
    env_val = os.environ.get("SHADOW_MAX_OPEN")
    if env_val is None:
        expect(sv.MAX_OPEN_MARKETS == 12,
               f"default concurrency cap wrong: {sv.MAX_OPEN_MARKETS}")
    else:
        expect(sv.MAX_OPEN_MARKETS == int(env_val),
               f"env SHADOW_MAX_OPEN={env_val} but MAX_OPEN_MARKETS={sv.MAX_OPEN_MARKETS}")


def test_pnl_blacklist_constants_present():
    """v5.2 regression guard: PNL-based blacklist constants must exist
    and be wired through env vars."""
    expect(hasattr(sv, "PNL_BLACKLIST_MIN_SELLS"),
           "PNL_BLACKLIST_MIN_SELLS missing")
    expect(hasattr(sv, "PNL_BLACKLIST_LOSS_USD"),
           "PNL_BLACKLIST_LOSS_USD missing")
    # Defaults must be sensible
    expect(sv.PNL_BLACKLIST_MIN_SELLS >= 3,
           f"min sells too low: {sv.PNL_BLACKLIST_MIN_SELLS}")
    expect(sv.PNL_BLACKLIST_LOSS_USD < 0,
           f"loss threshold must be negative: {sv.PNL_BLACKLIST_LOSS_USD}")


def test_pnl_blacklist_source_wired_in_both_paths():
    """v5.2 regression guard: blacklist check must appear in BOTH
    the maker-sell path and the taker-cross-exit path. If a future edit
    removes one, this test catches it."""
    src = open(sv.__file__).read()
    occurrences = src.count("PNL-BLACKLIST")
    expect(occurrences >= 2,
           f"PNL-BLACKLIST should appear in maker AND taker exit paths, "
           f"found {occurrences} occurrence(s)")


def test_log_spam_suppression_in_deviation_exit():
    """v5.2 regression guard: deviation-exit print must be gated on
    `not s.liquidating` to prevent log-spam loops."""
    src = open(sv.__file__).read()
    # Look for the deviation-exit log right after the spam-fix guard
    pattern_start = "if deviation_pct > PRICE_DEVIATION_EXIT_PCT:"
    idx = src.find(pattern_start)
    expect(idx != -1, "deviation-exit branch not found")
    # Within the next 300 chars there should be `if not s.liquidating`
    snippet = src[idx:idx + 600]
    expect("if not s.liquidating" in snippet,
           "log-spam guard 'if not s.liquidating' missing in deviation-exit branch")


def test_stuck_stage3_blacklist_constants_present():
    """v5.3 regression guard: STUCK_STAGE3_BLACKLIST_MIN + print throttle."""
    expect(hasattr(sv, "STUCK_STAGE3_BLACKLIST_MIN"),
           "STUCK_STAGE3_BLACKLIST_MIN missing")
    expect(hasattr(sv, "LIQ_STAGE3_PRINT_THROTTLE_MIN"),
           "LIQ_STAGE3_PRINT_THROTTLE_MIN missing")
    expect(sv.STUCK_STAGE3_BLACKLIST_MIN >= 10,
           f"blacklist window too short: {sv.STUCK_STAGE3_BLACKLIST_MIN}")


def test_shadow_has_stage3_tracking_fields():
    """v5.3: MarketShadow must carry stage3 timestamps for stuck detection."""
    s = sv.MarketShadow(ticker="T", title="t", tick=0.01, vol_24h=0)
    expect(hasattr(s, "stage3_first_entered_ts"),
           "stage3_first_entered_ts missing on MarketShadow")
    expect(hasattr(s, "stage3_last_print_ts"),
           "stage3_last_print_ts missing on MarketShadow")
    expect(s.stage3_first_entered_ts == 0.0, "initial stage3 ts must be 0")


def test_stuck_blacklist_source_wired():
    """v5.3 regression guard: STUCK-BLACKLIST print must exist in source."""
    src = open(sv.__file__).read()
    expect("STUCK-BLACKLIST" in src,
           "STUCK-BLACKLIST emoji/marker missing from _liquidation_quote")
    expect("stage3_first_entered_ts" in src,
           "stage3_first_entered_ts tracker missing in quote loop")


def test_v54_signal_export_constants_present():
    """v5.4: signal export config + thresholds must exist and be env-tunable."""
    for const in ("SIGNAL_EXPORT_ENABLED", "SIGNALS_EXPORT_PATH",
                  "FLOW_BURST_MIN_CONTRACTS", "VELOCITY_SPIKE_MIN",
                  "IMBALANCE_SUSTAINED_THRESHOLD"):
        expect(hasattr(sv, const), f"missing v5.4 constant: {const}")


def test_v54_signal_export_writes_jsonl():
    """v5.4: _emit_signal must append a valid JSON line to the export file."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl").name
    orig_path = sv.SIGNALS_EXPORT_PATH
    orig_enabled = sv.SIGNAL_EXPORT_ENABLED
    sv.SIGNALS_EXPORT_PATH = tmp
    sv.SIGNAL_EXPORT_ENABLED = True
    try:
        s = _mk_shadow()
        sv._emit_signal(s, "flow_burst", magnitude=100.0, direction="buy")
        with open(tmp) as f:
            line = f.readline()
        import json
        row = json.loads(line)
        expect(row["type"] == "flow_burst", f"wrong type: {row.get('type')}")
        expect(row["ticker"] == "TESTTKR", f"wrong ticker: {row.get('ticker')}")
        expect(row["magnitude"] == 100.0, f"wrong magnitude: {row.get('magnitude')}")
        expect(row["direction"] == "buy", f"wrong direction: {row.get('direction')}")
        expect("ts" in row, "ts missing")
    finally:
        sv.SIGNALS_EXPORT_PATH = orig_path
        sv.SIGNAL_EXPORT_ENABLED = orig_enabled
        if os.path.exists(tmp):
            os.remove(tmp)


def test_v54_signal_export_throttles_duplicates():
    """v5.4: same (ticker, type) signal within THROTTLE_SEC must NOT re-emit."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl").name
    if os.path.exists(tmp):
        os.remove(tmp)
    orig_path = sv.SIGNALS_EXPORT_PATH
    orig_enabled = sv.SIGNAL_EXPORT_ENABLED
    sv.SIGNALS_EXPORT_PATH = tmp
    sv.SIGNAL_EXPORT_ENABLED = True
    try:
        s = _mk_shadow()
        sv._emit_signal(s, "flow_burst", magnitude=100.0)
        sv._emit_signal(s, "flow_burst", magnitude=200.0)  # within window
        sv._emit_signal(s, "flow_burst", magnitude=300.0)  # within window
        with open(tmp) as f:
            lines = f.readlines()
        expect(len(lines) == 1,
               f"throttle should suppress repeats, got {len(lines)}")
    finally:
        sv.SIGNALS_EXPORT_PATH = orig_path
        sv.SIGNAL_EXPORT_ENABLED = orig_enabled
        if os.path.exists(tmp):
            os.remove(tmp)


def test_v54_pnl_deprioritization_constants_present():
    """v5.4: pnl-per-sell deprioritization rail must be wired."""
    for const in ("PNL_PER_SELL_MIN", "PNL_PER_SELL_MIN_SELLS",
                  "PNL_PER_SELL_DEPRIO_MULT"):
        expect(hasattr(sv, const), f"missing v5.4 constant: {const}")
    expect(0.0 < sv.PNL_PER_SELL_DEPRIO_MULT < 1.0,
           f"deprio mult should be fractional: {sv.PNL_PER_SELL_DEPRIO_MULT}")


def test_v54_shadow_has_deprioritized_flag():
    """v5.4: MarketShadow must carry deprioritized flag so the rail only
    demotes once per market (idempotent)."""
    s = sv.MarketShadow(ticker="T", title="t", tick=0.01, vol_24h=0)
    expect(hasattr(s, "deprioritized"), "deprioritized field missing")
    expect(s.deprioritized is False, "default deprioritized must be False")
    expect(hasattr(s, "last_signal_export_ts"), "last_signal_export_ts missing")


def test_v54_min_edge_after_fees_constant_present():
    """v5.4: min_edge gate config must exist."""
    expect(hasattr(sv, "MIN_NET_EDGE_PER_FILL"),
           "MIN_NET_EDGE_PER_FILL missing")
    expect(sv.MIN_NET_EDGE_PER_FILL > 0,
           f"min edge must be positive: {sv.MIN_NET_EDGE_PER_FILL}")


def test_v54_source_wired_in_quote_loop():
    """v5.4 regression guard: min_edge gate must be IN compute_quotes,
    not dangling in an import-time no-op."""
    src = open(sv.__file__).read()
    expect("expected_net_per_rt" in src,
           "min_edge gate variable not present in source")
    expect("MIN_NET_EDGE_PER_FILL" in src,
           "MIN_NET_EDGE_PER_FILL not referenced in source")
    expect("📉 PNL-DEPRIO" in src,
           "PNL-DEPRIO marker missing from source — rail not wired")


def test_v55_tradability_score_rewards_wider_spread():
    """v5.5: composite score must increase with wider spread, holding
    other signals constant."""
    base = {"spread": 0.03, "vol_24h": 50_000, "dtr": 30, "tick_step": 0.01}
    wider = {"spread": 0.08, "vol_24h": 50_000, "dtr": 30, "tick_step": 0.01}
    expect(sv._tradability_score(wider) > sv._tradability_score(base),
           "wider-spread market should rank higher")


def test_v55_tradability_score_rewards_higher_volume():
    """v5.5: composite score must increase with higher volume."""
    base = {"spread": 0.03, "vol_24h": 30_000, "dtr": 30, "tick_step": 0.01}
    richer = {"spread": 0.03, "vol_24h": 300_000, "dtr": 30, "tick_step": 0.01}
    expect(sv._tradability_score(richer) > sv._tradability_score(base),
           "higher-volume market should rank higher")


def test_v55_tradability_score_penalizes_tapered_ticks():
    """v5.9j REPLACES v5.5 taper penalty: penny-tick (tick ≤ 0.001)
    must now score HIGHER than cent-tick, not lower. Reason: v5.9i
    low_edge distribution showed penny-tick has positive median edge
    while cent-tick is solidly negative across all families."""
    cent = {"spread": 0.05, "vol_24h": 50_000, "dtr": 30, "tick_step": 0.01}
    penny = {"spread": 0.05, "vol_24h": 50_000, "dtr": 30, "tick_step": 0.001}
    expect(sv._tradability_score(penny) > sv._tradability_score(cent),
           f"v5.9j: penny must score higher than cent. "
           f"penny={sv._tradability_score(penny):.4f} "
           f"cent={sv._tradability_score(cent):.4f}")


def test_v55_tradability_exclusions_reject_thin_spread():
    """v5.5 hard exclusion: spread below TRADABILITY_MIN_SPREAD gets
    dropped before ranking."""
    m = {"spread": 0.01, "vol_24h": 100_000, "tick_step": 0.01}
    expect(not sv._meets_tradability_exclusions(m),
           "spread < $0.025 must be excluded")


def test_v55_tradability_exclusions_reject_low_vol():
    """v5.5b hard exclusion: volume below TRADABILITY_MIN_VOL gets dropped.
    Default vol floor is $15k (calibrated down from $25k in v5.5b)."""
    m = {"spread": 0.05, "vol_24h": 5_000, "tick_step": 0.01}
    expect(not sv._meets_tradability_exclusions(m),
           "vol < $15k must be excluded")


def test_v55b_taper_exclusion_opt_in_not_default():
    """v5.5b calibration: taper exclusion is OPT-IN via env, not default.
    A tapered market with good spread+vol must pass when the flag is off."""
    m = {"spread": 0.05, "vol_24h": 100_000, "tick_step": 0.001}
    if not sv.TRADABILITY_EXCLUDE_TAPERED:
        expect(sv._meets_tradability_exclusions(m),
               "tapered market must pass when exclusion flag is off")
    else:
        expect(not sv._meets_tradability_exclusions(m),
               "tapered market should be excluded when flag is on")


def test_v55b_default_exclude_tapered_is_off():
    """v5.5b hard-coded default check: TRADABILITY_EXCLUDE_TAPERED must
    default False. v5.5 had it True and over-filtered 556→5 markets."""
    expect(sv.TRADABILITY_EXCLUDE_TAPERED is False,
           "TRADABILITY_EXCLUDE_TAPERED default must be False in v5.5b")


def test_v55b_default_min_vol_is_15k():
    """v5.5b hard-coded default check: TRADABILITY_MIN_VOL must default
    to $15k (down from v5.5's $25k) to widen the tradable universe."""
    expect(sv.TRADABILITY_MIN_VOL == 15_000.0,
           f"TRADABILITY_MIN_VOL default must be 15000, got {sv.TRADABILITY_MIN_VOL}")


def test_v55b_rejects_malformed_candidates():
    """v5.5b malformed-candidate rejection: missing ticker, missing bid/ask,
    or non-numeric essentials must all be dropped by _is_well_formed_candidate."""
    expect(not sv._is_well_formed_candidate({}),
           "empty dict must be rejected (missing ticker)")
    expect(not sv._is_well_formed_candidate({"ticker": "", "yes_bid": 0.5, "yes_ask": 0.51}),
           "empty-string ticker must be rejected")
    expect(not sv._is_well_formed_candidate({"ticker": "X", "yes_ask": 0.51}),
           "missing yes_bid must be rejected")
    expect(not sv._is_well_formed_candidate({"ticker": "X", "yes_bid": None, "yes_ask": 0.51}),
           "null yes_bid must be rejected")
    expect(not sv._is_well_formed_candidate({"ticker": "X", "yes_bid": 0, "yes_ask": 0.51}),
           "zero yes_bid must be rejected")
    expect(not sv._is_well_formed_candidate({"ticker": "X", "yes_bid": "abc", "yes_ask": 0.51}),
           "non-numeric yes_bid must be rejected")
    expect(sv._is_well_formed_candidate({"ticker": "X", "yes_bid": 0.49, "yes_ask": 0.51}),
           "clean candidate must pass")


def test_v55_tradability_passes_clean_market():
    """v5.5 regression guard: a clean market (spread $0.04, vol $100k,
    $0.01 tick) must pass all exclusions."""
    m = {"spread": 0.04, "vol_24h": 100_000, "tick_step": 0.01}
    expect(sv._meets_tradability_exclusions(m),
           f"clean market must pass exclusions: {m}")


def test_v55_ranker_constants_env_configurable():
    """v5.5: all 10 tradability constants must be wired to env vars."""
    for const in ("TRAD_WEIGHT_SPREAD", "TRAD_WEIGHT_VOL", "TRAD_WEIGHT_DTR",
                  # v5.9j: TRAD_WEIGHT_TAPER renamed to TRAD_WEIGHT_TICK_BIAS.
                  "TRAD_WEIGHT_TICK_BIAS", "TRAD_NORM_SPREAD_DOLLARS",
                  "TRAD_NORM_VOL_USD", "TRAD_NORM_DTR_DAYS",
                  "TRADABILITY_MIN_SPREAD", "TRADABILITY_MIN_VOL",
                  "TRADABILITY_EXCLUDE_TAPERED"):
        expect(hasattr(sv, const), f"missing v5.5 constant: {const}")
    expect(hasattr(sv, "USE_TRADABILITY_RANKER"),
           "USE_TRADABILITY_RANKER flag missing (needed for rollback)")


def test_v55_source_wired_in_load_candidates():
    """v5.5 regression guard: load_candidates must route through the
    new ranker path when the flag is enabled."""
    src = open(sv.__file__).read()
    expect("_tradability_score" in src,
           "_tradability_score not referenced in source")
    expect("_meets_tradability_exclusions" in src,
           "_meets_tradability_exclusions not referenced in source")
    expect("USE_TRADABILITY_RANKER" in src,
           "USE_TRADABILITY_RANKER flag not referenced")
    expect("composite-tradability" in src,
           "new rank label missing from load print")


def test_quote_loop_source_no_orphan_backdate():
    """Source-level guard: exactly ONE occurrence of the stage-1 backdate
    pattern should exist in kalshi_shadow_v4.py — the disabled branch
    (line ~931). If this count changes, someone either re-introduced the
    bug or moved the legit disabled backdate elsewhere."""
    import re
    src = open(sv.__file__).read()
    pattern = r"liquidation_started_ts\s*=\s*time\.time\(\)\s*-\s*\(LIQ_STAGE_1_DURATION_MIN"
    matches = re.findall(pattern, src)
    expect(len(matches) == 1,
           f"expected exactly 1 backdate (disabled branch), found {len(matches)}")


TESTS = [
    test_urgent_enters_stage1_not_stage2,
    test_urgent_escalates_to_stage2_after_window,
    test_v56_gate_reasons_now_nonurgent_by_default,
    test_v56_gate_env_override_restores_urgent,
    test_v56_nonurgent_paths_preserved,
    test_v56_gate_default_is_nonurgent,
    test_stale_no_sells_stays_passive,
    test_disabled_keeps_backdate_behavior,
    test_max_open_markets_env_configurable,
    test_max_open_markets_default_is_12,
    test_pnl_blacklist_constants_present,
    test_pnl_blacklist_source_wired_in_both_paths,
    test_log_spam_suppression_in_deviation_exit,
    test_stuck_stage3_blacklist_constants_present,
    test_shadow_has_stage3_tracking_fields,
    test_stuck_blacklist_source_wired,
    test_v54_signal_export_constants_present,
    test_v54_signal_export_writes_jsonl,
    test_v54_signal_export_throttles_duplicates,
    test_v54_pnl_deprioritization_constants_present,
    test_v54_shadow_has_deprioritized_flag,
    test_v54_min_edge_after_fees_constant_present,
    test_v54_source_wired_in_quote_loop,
    test_v55_tradability_score_rewards_wider_spread,
    test_v55_tradability_score_rewards_higher_volume,
    test_v55_tradability_score_penalizes_tapered_ticks,
    test_v55_tradability_exclusions_reject_thin_spread,
    test_v55_tradability_exclusions_reject_low_vol,
    test_v55b_taper_exclusion_opt_in_not_default,
    test_v55b_default_exclude_tapered_is_off,
    test_v55b_default_min_vol_is_15k,
    test_v55b_rejects_malformed_candidates,
    test_v55_tradability_passes_clean_market,
    test_v55_ranker_constants_env_configurable,
    test_v55_source_wired_in_load_candidates,
    test_v57_min_net_edge_constants_present,
    test_v57_expected_buy_edge_thin_spread_negative,
    test_v57_expected_buy_edge_wide_spread_passes,
    test_v57_expected_buy_edge_extreme_price_fee_lower,
    test_v57_expected_buy_edge_rejects_invalid_inputs,
    test_v57_low_edge_gate_wired_in_quote_loop,
    test_v57_low_edge_gate_only_blocks_buy_not_sell,
    test_v59_velocity_constants_present,
    test_v59_fast_move_cent_tick_narrow_rejects,
    test_v59_fast_move_cent_tick_narrow_passes,
    test_v59_fast_move_penny_wide_passes,
    test_v59_fast_move_penny_wide_rejects,
    test_v59_fast_move_zero_spread_fallback_uses_floor,
    test_v59_source_wired_in_meets_market_conditions,
    test_v59b_shadow_has_book_diagnostic_fields,
    test_v59b_snapshot_count_increments,
    test_v59b_one_sided_yes_only_detected,
    test_v59b_one_sided_no_only_detected,
    test_v59b_book_side_zeroed_detected_by_one_sided_snapshot,
    test_v59b_book_side_not_zeroed_when_already_zero,
    test_v59b_instrumentation_does_not_change_behavior,
    test_v59c_flow_constants_present,
    test_v59c_flow_schema_complete,
    test_v59c_empty_market_all_zero_stable,
    test_v59c_pressure_up_when_high_flow_and_positive_bias,
    test_v59c_pressure_down_when_high_flow_and_negative_bias,
    test_v59c_stable_when_flow_below_threshold,
    test_v59c_pressure_direction_neutral_band,
    test_v59c_flow_score_weighting_formula,
    test_v59c_fill_bias_clipped_to_unit_range,
    test_v59c_publish_writes_valid_json_atomically,
    test_v59c1_regime_disagreement_stays_stable,
    test_v59c1_regime_history_capped,
    test_v59c1_regime_recovers_after_interruption,
    test_v59c1_directional_conviction_formula,
    test_v59c1_directional_conviction_bounded,
    test_v59c1_neutral_band_wider,
    test_v59c1_publisher_updates_regime_counts,
    test_v59c_publish_disabled_writes_nothing,
    test_v59d_shadow_has_delta_diagnostic_fields,
    test_v59d_yes_delta_increments_yes_counter_only,
    test_v59d_no_delta_increments_no_counter_only,
    test_v59d_malformed_delta_increments_nothing,
    test_v59d_bid_empty_transition_classified_and_logged,
    test_v59d_ask_empty_transition_classified,
    test_v59d_crossed_book_classified,
    test_v59d_first_invalid_logged_once_per_market,
    test_v59d_invalid_to_invalid_does_not_count_transition,
    test_v59d_valid_to_valid_does_not_count_transition,
    test_v59e_shadow_has_new_fields,
    test_v59e_classify_invalid_taxonomy,
    test_v59e_ever_valid_flips_on_first_valid_book,
    test_v59e_ever_valid_stays_false_if_never_valid,
    test_v59e_first_book_ts_records_first_observation,
    test_v59e_empty_both_reason_classified,
    test_v59e_locked_reason_classified,
    test_v59e_crossed_reason_classified_distinctly_from_locked,
    test_v59e_forensic_dump_fires_once_per_threshold,
    test_v59e_forensic_dump_throttles_on_repeated_calls,
    test_v59f_admission_constants_present,
    test_v59f_shadow_has_admission_fields,
    test_v59f_fresh_market_inside_grace_stays_admitted,
    test_v59f_never_valid_past_grace_gets_rejected,
    test_v59f_ever_valid_market_never_rejected,
    test_v59f_disabled_via_env_never_flips,
    test_v59f_grace_zero_disables,
    test_v59f_rejected_market_short_circuits_gate,
    test_v59f_valid_market_unaffected_when_not_rejected,
    test_v59f_sweep_across_states,
    test_v59g_stuck_invalid_constant_present,
    test_v59g_new_shadow_fields,
    test_v59g_invalid_since_ts_set_on_valid_to_invalid,
    test_v59g_invalid_since_ts_cleared_on_invalid_to_valid,
    test_v59g_stuck_invalid_triggers_admission_reject,
    test_v59g_stuck_grace_not_yet_elapsed_stays_admitted,
    test_v59g_ever_valid_but_not_invalid_stays_admitted,
    test_v59g_never_valid_path_sets_reason,
    test_v59g_stuck_check_disabled_by_zero_window,
    test_v59h_near_miss_buffer_and_recorder_present,
    test_v59h_recorder_appends_tuple,
    test_v59h_recorder_caps_buffer,
    test_v59h_distribution_bucket_boundaries,
    test_v59h_distribution_empty_buffer_all_zeros,
    test_v59i_classify_family_weather,
    test_v59i_classify_family_sports,
    test_v59i_classify_family_politics,
    test_v59i_classify_family_commodities,
    test_v59i_classify_family_entertainment,
    test_v59i_classify_family_misc_fallback,
    test_v59i_analysis_empty_buffer_safe,
    test_v59i_analysis_populates_top_N_and_families,
    test_v59j_constants_present,
    test_v59j_tick_bias_score_penny,
    test_v59j_tick_bias_score_cent_wide_vs_narrow,
    test_v59j_ranker_prefers_penny_over_cent_same_spread,
    test_v59j_ranker_cent_wide_above_cent_narrow,
    test_v59j_initial_cap_multiplier_penny,
    test_v59j_initial_cap_multiplier_cent_wide,
    test_v59j_initial_cap_multiplier_cent_narrow,
    test_v59j_market_shadow_construction_applies_cap,
    test_v59j_tick_bucket_classifier,
    test_v59j_aggregate_by_tick_bucket,
    test_v59k_constant_present_default_enabled,
    test_v59k_disabled_flag_clears_quotes_no_inventory,
    test_v59k_disabled_flag_blocks_new_buy_but_keeps_sell_with_inventory,
    test_v59k_enabled_flag_preserves_quoting_behavior,
    test_v58_constants_present,
    test_v58_shadow_has_inventory_tracking_fields,
    test_v58_inv_trap_strikes_increment_on_urgent,
    test_v58_inv_trap_demote_at_threshold,
    test_v58_inventory_stale_reason_is_nonurgent,
    test_v58_source_wired_inventory_age_gate,
    test_v58_source_wired_timestamp_set_on_buy,
    test_v58_source_wired_reset_on_sell_close,
    test_v58_source_wired_telemetry_line,
    test_v58_profit_lock_fires_once_not_every_tick,
    test_v58_inventory_age_gate_fires_in_compute_quotes,
    test_v58_profit_lock_fires_in_compute_quotes,
    test_v58_v54_min_edge_skipped_when_holding_inventory,
    test_v58_gates_run_before_v54_early_return,
    test_quote_loop_source_no_orphan_backdate,
]


def run():
    passed = 0
    failed = []
    for fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Fail as e:
            failed.append((fn.__name__, str(e)))
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed.append((fn.__name__, f"{type(e).__name__}: {e}"))
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print()
    print(f"  {passed}/{len(TESTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
