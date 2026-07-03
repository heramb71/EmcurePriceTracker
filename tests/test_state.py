"""Tests for strategy state transitions (open / partial / close / circuit breaker)."""
from __future__ import annotations

from src.emcure.state import (
    _default_state,
    book_partial,
    check_circuit_breaker,
    close_position,
    open_position,
    reset_session_if_new_day,
)

_SIZING = {
    "entry": 1700.0, "sl": 1685.0, "t1": 1710.0, "t2": 1720.0, "t3": 1725.0,
    "qty": 6, "risk_per_share": 15.0, "risk_amount": 90.0, "capital_used": 10200.0,
    "atr": 30.0, "atr_mult": 0.5, "rr": 1.0,
}


def _open(qty=6):
    state = _default_state()
    sizing = dict(_SIZING, qty=qty)
    return open_position(state, "EMCURE", sizing, atr=30.0)


def test_open_position_stores_all_targets():
    state = _open()
    pos = state["position"]
    assert pos["entry"] == 1700.0
    assert (pos["t1"], pos["t2"], pos["t3"]) == (1710.0, 1720.0, 1725.0)
    assert pos["qty"] == pos["qty_remaining"] == 6
    assert pos["partial_booked"] is False


def test_t1_books_third_and_moves_stop_to_breakeven():
    state = _open(qty=6)
    state, pnl = book_partial(state, exit_price=1710.0, reason="t1_hit")
    pos = state["position"]
    assert pos["qty_remaining"] == 4          # 6 - (6//3)
    assert pnl == 20.0                          # (1710-1700)*2
    assert pos["partial_booked"] is True
    assert pos["sl"] == pos["entry"]           # breakeven
    assert pos["breakeven_moved"] is True


def test_t2_books_without_moving_stop():
    state = _open(qty=6)
    state, _ = book_partial(state, 1710.0, reason="t1_hit")  # qty_remaining=4, sl=entry
    state, pnl = book_partial(state, 1720.0, reason="t2_hit")
    pos = state["position"]
    assert pos["t2_booked"] is True
    assert pnl == 20.0                          # (1720-1700)*(4//3=1)
    assert pos["sl"] == pos["entry"]           # unchanged from T1


def test_close_position_records_journal_with_net_pnl():
    state = _open(qty=6)
    state, _ = book_partial(state, 1710.0, reason="t1_hit")   # books 2 @ +10 = +20
    state, net = close_position(state, exit_price=1720.0, reason="supertrend_exit")
    assert state["position"] is None
    assert len(state["journal"]) == 1
    trade = state["journal"][-1]
    assert trade["reason"] == "supertrend_exit"
    # gross: partial +20 + remaining 4 @ +20 = +80 → 100
    assert trade["gross_pnl"] == 100.0
    # net is gross minus real charges (STT/exchange/GST/stamp)
    assert trade["charges"] > 0
    assert trade["total_pnl"] == round(100.0 - trade["charges"], 2)
    assert net == trade["total_pnl"]
    assert net < 100.0


def test_close_with_loss_increments_consecutive_losses():
    state = _open(qty=6)
    state, _ = close_position(state, exit_price=1685.0, reason="stop_hit")
    assert state["session"]["consecutive_losses"] == 1


def test_circuit_breaker_halts_after_two_losses():
    state = _default_state()
    state["session"]["consecutive_losses"] = 2
    halted, reason = check_circuit_breaker(state, capital=15000, max_loss_pct=8.0)
    assert halted is True
    assert reason == "two_consecutive_losses"


def test_circuit_breaker_halts_on_daily_drawdown():
    state = _default_state()
    state["session"]["session_pnl"] = -1300.0
    halted, reason = check_circuit_breaker(state, capital=15000, max_loss_pct=8.0)  # limit -1200
    assert halted is True
    assert reason == "daily_drawdown_limit"


def test_circuit_breaker_passes_when_healthy():
    state = _default_state()
    halted, reason = check_circuit_breaker(state, capital=15000, max_loss_pct=8.0)
    assert halted is False
    assert reason == ""


def test_reset_session_clears_on_new_day():
    state = _default_state()
    state["session"] = {
        "date": "2000-01-01", "consecutive_losses": 5,
        "session_pnl": -999.0, "halted": True,
    }
    state = reset_session_if_new_day(state)
    assert state["session"]["consecutive_losses"] == 0
    assert state["session"]["halted"] is False
