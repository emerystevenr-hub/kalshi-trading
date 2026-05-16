"""Self-test for kalshi_catalyst.py dynamic-exit logic.

Exercises the partial-take-profit + breakeven-ratchet pipeline on
scripted price trajectories without hitting Kalshi. Validates:

  1. Ratchet fires at breakeven_lock_fraction of target distance.
  2. Ratchet only moves stop UP, never back.
  3. Partial take-profit fires at partial_take_fraction and closes
     exactly partial_take_size of the original position.
  4. Lifetime P&L = partial_pnl + final_pnl (cumulative booking).
  5. Entry fee is allocated pro-rata across partial + final chunks.
  6. Stop hit after ratchet books a WIN (stop was ratcheted above entry).
  7. Backward-compat: a thesis constructed without the new fields still
     behaves as before (default params don't break existing theses).

Run:  python3 kalshi_catalyst_test.py
Exit code 0 on all pass, 1 on any failure.
"""

import math
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

# Point fills log to a tmp file so we don't pollute the real CSV
_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
os.environ["CATALYST_LIVE_MODE"] = "0"

# Stub out KALSHI_KEY_ID / PRIVATE_KEY_PATH so the import doesn't blow up
os.environ.setdefault("KALSHI_KEY_ID", "test")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "/dev/null")

import kalshi_catalyst as kc  # noqa: E402

# Redirect fills log to tmp
kc.CATALYST_FILLS_LOG = _tmp.name


def _mk_thesis(**overrides):
    defaults = dict(
        id="test_thesis",
        description="test",
        calendar_event="test",
        target_ticker_prefixes=["TESTTKR"],
        side="NO",
        our_probability=0.20,
        max_entry_price=0.60,
        target_price=0.80,
        stop_price=0.40,
        max_position_usd=50.0,
        correlation_group="test_group",
        valid_until=datetime.now(timezone.utc) + timedelta(days=30),
    )
    defaults.update(overrides)
    return kc.CatalystThesis(**defaults)


def _mk_contract(no_bid=0.60, yes_bid=0.40):
    c = kc.ContractState(ticker="TESTTKR")
    c.no_bid = no_bid
    c.yes_bid = yes_bid
    return c


def _mk_position(thesis, size=100.0, entry_price=0.60):
    entry_fee = kc.compute_fee(size, entry_price, is_taker=True)
    pos = kc.CatalystPosition(
        thesis_id=thesis.id,
        ticker="TESTTKR",
        side=thesis.side,
        size=size,
        entry_price=entry_price,
        entry_ts=0.0,
        current_price=entry_price,
        fees_paid=entry_fee,
        original_size=size,
        entry_fee=entry_fee,
        effective_stop=thesis.stop_price,
    )
    return pos


def reset_state(thesis):
    kc.POSITIONS.clear()
    kc.THESES.clear()
    kc.THESES.append(thesis)


class Fail(Exception):
    pass


def expect(cond, msg):
    if not cond:
        raise Fail(msg)


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────

def test_ratchet_fires_at_threshold():
    """Breakeven lock at 40% of distance should move stop above entry."""
    t = _mk_thesis(partial_take_fraction=0.65, breakeven_lock_fraction=0.40,
                   breakeven_stop_buffer=0.05)
    reset_state(t)
    # Entry at 0.60 (hypothetical), target 0.80, distance = 0.20
    # Ratchet triggers at entry + 0.40*0.20 = 0.68
    pos = _mk_position(t, size=100.0, entry_price=0.60)
    kc.POSITIONS.append(pos)
    # Price not yet at threshold
    c = _mk_contract(no_bid=0.64)
    kc._check_exit_conditions(pos, c)
    expect(pos.effective_stop == 0.40, f"stop moved prematurely: {pos.effective_stop}")
    # Price crosses ratchet threshold
    c = _mk_contract(no_bid=0.68)
    kc._check_exit_conditions(pos, c)
    expect(abs(pos.effective_stop - 0.65) < 1e-9,
           f"stop didn't ratchet to entry+buffer: {pos.effective_stop}")
    # Price drops back (still above ratcheted stop) — ratchet must not reverse
    c = _mk_contract(no_bid=0.66)
    kc._check_exit_conditions(pos, c)
    expect(abs(pos.effective_stop - 0.65) < 1e-9,
           f"ratchet reversed: {pos.effective_stop}")


def test_partial_fires_at_threshold_and_closes_size():
    """Partial at 65% of distance should exit partial_take_size of position."""
    t = _mk_thesis(partial_take_fraction=0.65, partial_take_size=0.5,
                   breakeven_lock_fraction=0.40)
    reset_state(t)
    pos = _mk_position(t, size=100.0, entry_price=0.60)
    kc.POSITIONS.append(pos)
    # Partial threshold: entry + 0.65*0.20 = 0.73
    c = _mk_contract(no_bid=0.73)
    kc._check_exit_conditions(pos, c)
    expect(pos.partial_taken, "partial_taken flag not set")
    expect(abs(pos.size - 50.0) < 1e-9, f"size didn't halve: {pos.size}")
    expect(pos.original_size == 100.0, f"original_size mutated: {pos.original_size}")
    expect(pos.realized_pnl > 0, f"partial pnl not booked positive: {pos.realized_pnl}")
    # Partial should not fire again
    prior_pnl = pos.realized_pnl
    c = _mk_contract(no_bid=0.75)
    kc._check_exit_conditions(pos, c)
    expect(abs(pos.realized_pnl - prior_pnl) < 1e-9,
           "partial re-fired on second tick")


def test_full_target_after_partial_sums_correctly():
    """Lifetime P&L after partial-then-target should be sum of the two chunks."""
    t = _mk_thesis(partial_take_fraction=0.50, partial_take_size=0.5)
    reset_state(t)
    pos = _mk_position(t, size=100.0, entry_price=0.60)
    kc.POSITIONS.append(pos)
    # Trigger partial at 0.70
    kc._check_exit_conditions(pos, _mk_contract(no_bid=0.70))
    expect(pos.partial_taken, "partial didn't fire")
    partial_pnl = pos.realized_pnl
    # Trigger full target at 0.80
    kc._check_exit_conditions(pos, _mk_contract(no_bid=0.80))
    expect(pos.closed, "position didn't close on target")
    lifetime = pos.realized_pnl
    expect(lifetime > partial_pnl, f"lifetime ({lifetime}) <= partial ({partial_pnl})")
    # Sanity:
    #   Gross gain  = 50*(0.70-0.60) + 50*(0.80-0.60) = 5 + 10 = $15
    #   Entry fee   = 0.07 * 100 * 0.60 * 0.40         = $1.68
    #   Partial fee = 0.07 *  50 * 0.70 * 0.30         = $0.735
    #   Final fee   = 0.07 *  50 * 0.80 * 0.20         = $0.56
    #   Net         = 15 - 1.68 - 0.735 - 0.56         ≈ $12.025
    expect(11.8 < lifetime < 12.3,
           f"lifetime pnl out of expected band: {lifetime:.3f}")


def test_stop_after_ratchet_books_win():
    """If price ratchets stop to breakeven and then reverses, exit books a small win."""
    t = _mk_thesis(partial_take_fraction=0.99, breakeven_lock_fraction=0.40,
                   breakeven_stop_buffer=0.05)
    # partial_take_fraction set very high so it never fires in this test
    reset_state(t)
    pos = _mk_position(t, size=100.0, entry_price=0.60)
    kc.POSITIONS.append(pos)
    # Move to 0.70 — triggers ratchet (stop -> 0.65 with $0.05 buffer)
    kc._check_exit_conditions(pos, _mk_contract(no_bid=0.70))
    expect(pos.effective_stop > t.stop_price, "ratchet didn't fire")
    # Price reverses to 0.65 — hits ratcheted stop
    kc._check_exit_conditions(pos, _mk_contract(no_bid=0.65))
    expect(pos.closed, "didn't exit on ratcheted stop")
    expect(pos.close_reason == "stop_ratcheted",
           f"close reason wrong: {pos.close_reason}")
    expect(pos.realized_pnl > 0,
           f"ratcheted stop exit didn't book a win: {pos.realized_pnl}")


def test_stop_below_ratchet_books_loss():
    """If price moves against the position past thesis.stop_price before any ratchet, exit at loss."""
    t = _mk_thesis()
    reset_state(t)
    pos = _mk_position(t, size=100.0, entry_price=0.60)
    kc.POSITIONS.append(pos)
    kc._check_exit_conditions(pos, _mk_contract(no_bid=0.40))
    expect(pos.closed, "didn't exit on stop")
    expect(pos.close_reason == "stop",
           f"close reason wrong: {pos.close_reason}")
    expect(pos.realized_pnl < 0, f"stop exit should book loss: {pos.realized_pnl}")


def test_entry_fee_allocated_prorata():
    """Sum of fees_paid across the full lifetime should equal entry_fee + all exit_fees."""
    t = _mk_thesis(partial_take_fraction=0.50, partial_take_size=0.5)
    reset_state(t)
    pos = _mk_position(t, size=100.0, entry_price=0.60)
    kc.POSITIONS.append(pos)
    entry_fee = pos.entry_fee
    kc._check_exit_conditions(pos, _mk_contract(no_bid=0.70))  # partial
    partial_exit_fee = kc.compute_fee(50.0, 0.70, is_taker=True)
    kc._check_exit_conditions(pos, _mk_contract(no_bid=0.80))  # final
    final_exit_fee = kc.compute_fee(50.0, 0.80, is_taker=True)
    expected_total_fees = entry_fee + partial_exit_fee + final_exit_fee
    expect(abs(pos.fees_paid - expected_total_fees) < 1e-6,
           f"fees_paid mismatch: {pos.fees_paid} vs expected {expected_total_fees}")


def test_defaults_backcompat():
    """Thesis constructed without new fields uses defaults."""
    t = _mk_thesis()  # no dynamic-exit overrides
    expect(t.partial_take_fraction == 0.65, "default partial_take_fraction wrong")
    expect(t.partial_take_size == 0.5, "default partial_take_size wrong")
    expect(t.breakeven_lock_fraction == 0.40, "default breakeven_lock_fraction wrong")
    expect(t.breakeven_stop_buffer == 0.05, "default breakeven_stop_buffer wrong")


def test_validator_rejects_bad_ratchet_order():
    """If ratchet threshold >= partial threshold, validator must reject."""
    t = _mk_thesis(partial_take_fraction=0.40, breakeven_lock_fraction=0.60)
    reset_state(t)
    try:
        kc._validate_theses_or_die()
        raise Fail("validator should have rejected bad ordering")
    except SystemExit:
        pass  # expected


def test_session_realized_sees_partial_gains():
    """_session_realized_pnl must include partial gains on still-open positions."""
    t = _mk_thesis(partial_take_fraction=0.50, partial_take_size=0.5)
    reset_state(t)
    pos = _mk_position(t, size=100.0, entry_price=0.60)
    kc.POSITIONS.append(pos)
    kc._check_exit_conditions(pos, _mk_contract(no_bid=0.70))
    expect(pos.partial_taken, "partial didn't fire")
    expect(not pos.closed, "position should still be open")
    sess = kc._session_realized_pnl()
    expect(sess > 0, f"session pnl should reflect partial gain: {sess}")


TESTS = [
    test_ratchet_fires_at_threshold,
    test_partial_fires_at_threshold_and_closes_size,
    test_full_target_after_partial_sums_correctly,
    test_stop_after_ratchet_books_win,
    test_stop_below_ratchet_books_loss,
    test_entry_fee_allocated_prorata,
    test_defaults_backcompat,
    test_validator_rejects_bad_ratchet_order,
    test_session_realized_sees_partial_gains,
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
