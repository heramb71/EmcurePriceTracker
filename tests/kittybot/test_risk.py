"""Risk logic: sizing, levels, breakeven ratchet, exit rules, P&L."""
from __future__ import annotations

from datetime import time as dtime

import pytest

from src.kittybot.opening_range import LONG, SHORT
from src.kittybot.risk import (
    STOP,
    TARGET,
    TIME,
    breakeven_stop,
    compute_levels,
    exit_reason,
    plan_trade,
    position_size,
    realized_pnl,
)


# ── compute_levels ───────────────────────────────────────────────────────────
def test_long_levels_bracket_entry():
    target, stop = compute_levels(100.0, LONG, target_pct=3.0, stop_pct=1.5)
    assert target == 103.0
    assert stop == 98.5


def test_short_levels_mirror_long():
    target, stop = compute_levels(100.0, SHORT, target_pct=3.0, stop_pct=1.5)
    assert target == 97.0
    assert stop == 101.5


def test_unknown_direction_raises():
    with pytest.raises(ValueError):
        compute_levels(100.0, "SIDEWAYS", 3.0, 1.5)


# ── position_size ────────────────────────────────────────────────────────────
def test_position_size_caps_at_one_percent_risk():
    # 1% of ₹100000 = ₹1000 budget; ₹2 per-share risk → 500 shares.
    qty = position_size(capital=100_000, entry=100.0, stop=98.0, risk_pct=1.0)
    assert qty == 500
    assert (100.0 - 98.0) * qty <= 100_000 * 0.01


def test_position_size_never_exceeds_budget_when_not_divisible():
    # ₹1000 budget / ₹3 risk = 333.3 → floor to 333, still ≤ budget.
    qty = position_size(100_000, 100.0, 97.0, 1.0)
    assert qty == 333
    assert (100.0 - 97.0) * qty <= 1000.0


def test_position_size_zero_when_no_stop_distance():
    assert position_size(100_000, 100.0, 100.0, 1.0) == 0


def test_position_size_zero_on_degenerate_capital():
    assert position_size(0, 100.0, 98.0, 1.0) == 0
    assert position_size(100_000, 100.0, 98.0, 0.0) == 0


# ── plan_trade ───────────────────────────────────────────────────────────────
def test_plan_trade_bundles_sized_trade():
    plan = plan_trade("TATAMOTORS", LONG, entry=100.0, target_pct=3.0, stop_pct=1.0,
                      capital=100_000, risk_pct=1.0)
    assert plan is not None
    assert plan.qty == 1000  # ₹1000 / ₹1 per-share risk
    assert plan.target == 103.0
    assert plan.stop == 99.0
    assert plan.risk_rupees == pytest.approx(1000.0)


def test_plan_trade_none_when_unsized():
    # Wildly small capital → risk budget below one share → no plan.
    plan = plan_trade("X", LONG, entry=100.0, target_pct=3.0, stop_pct=1.0,
                      capital=1.0, risk_pct=1.0)
    assert plan is None


# ── breakeven ratchet ────────────────────────────────────────────────────────
def test_breakeven_moves_long_stop_up_at_trigger():
    # +1% on a 100 entry = 101 → stop jumps from 99 to breakeven 100.
    assert breakeven_stop(100.0, LONG, current_price=101.0, current_stop=99.0,
                          trigger_pct=1.0) == 100.0


def test_breakeven_holds_long_stop_before_trigger():
    assert breakeven_stop(100.0, LONG, current_price=100.5, current_stop=99.0,
                          trigger_pct=1.0) == 99.0


def test_breakeven_never_loosens_long_stop():
    # Already ratcheted above breakeven — must not drop back to entry.
    assert breakeven_stop(100.0, LONG, current_price=101.0, current_stop=100.5,
                          trigger_pct=1.0) == 100.5


def test_breakeven_moves_short_stop_down_at_trigger():
    assert breakeven_stop(100.0, SHORT, current_price=99.0, current_stop=101.0,
                          trigger_pct=1.0) == 100.0


def test_breakeven_never_loosens_short_stop():
    assert breakeven_stop(100.0, SHORT, current_price=99.0, current_stop=99.5,
                          trigger_pct=1.0) == 99.5


# ── exit rules ───────────────────────────────────────────────────────────────
def _long_plan():
    return plan_trade("X", LONG, 100.0, 3.0, 1.0, 100_000, 1.0)


def test_exit_target_hit_long():
    plan = _long_plan()
    assert exit_reason(plan, 103.5, dtime(11, 0), dtime(15, 10)) == TARGET


def test_exit_stop_hit_long():
    plan = _long_plan()
    assert exit_reason(plan, 98.9, dtime(11, 0), dtime(15, 10)) == STOP


def test_exit_none_inside_range():
    plan = _long_plan()
    assert exit_reason(plan, 101.0, dtime(11, 0), dtime(15, 10)) is None


def test_hard_time_exit_takes_precedence():
    plan = _long_plan()
    # Even sitting profitably below target, 15:10 forces a flat book.
    assert exit_reason(plan, 102.0, dtime(15, 10), dtime(15, 10)) == TIME


def test_exit_honours_ratcheted_stop_override():
    plan = _long_plan()  # base stop 99.0
    # Price 99.5 wouldn't hit the base stop, but a breakeven stop of 100 does.
    assert exit_reason(plan, 99.5, dtime(12, 0), dtime(15, 10), stop=100.0) == STOP


def test_exit_short_target_and_stop():
    plan = plan_trade("X", SHORT, 100.0, 3.0, 1.0, 100_000, 1.0)
    assert exit_reason(plan, 96.5, dtime(11, 0), dtime(15, 10)) == TARGET
    assert exit_reason(plan, 101.5, dtime(11, 0), dtime(15, 10)) == STOP


# ── realized P&L ─────────────────────────────────────────────────────────────
def test_realized_pnl_long_and_short():
    long_plan = plan_trade("X", LONG, 100.0, 3.0, 1.0, 100_000, 1.0)  # qty 1000
    assert realized_pnl(long_plan, 103.0) == pytest.approx(3000.0)
    assert realized_pnl(long_plan, 99.0) == pytest.approx(-1000.0)

    short_plan = plan_trade("X", SHORT, 100.0, 3.0, 1.0, 100_000, 1.0)
    assert realized_pnl(short_plan, 97.0) == pytest.approx(3000.0)
    assert realized_pnl(short_plan, 101.0) == pytest.approx(-1000.0)
