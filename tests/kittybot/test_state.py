"""Persistent state: open/close position, stop ratchet, loss-streak accounting."""
from __future__ import annotations

from datetime import date

from src.kittybot import state
from src.kittybot.opening_range import LONG
from src.kittybot.risk import plan_trade


def _plan():
    return plan_trade("TATAMOTORS", LONG, 100.0, 3.0, 1.0, 100_000, 1.0)


def test_load_empty_state(tmp_path):
    st = state.load(str(tmp_path / "s.json"))
    assert st["position"] is None
    assert st["loss_streak"] == 0
    assert st["halt_until"] is None


def test_open_and_read_position(tmp_path):
    path = str(tmp_path / "s.json")
    state.open_position(path, _plan(), session_date=date(2026, 7, 6),
                        entry_order_id="PAPER-1", fill_price=100.1)
    pos = state.get_position(path)
    assert pos["symbol"] == "TATAMOTORS"
    assert pos["entry_fill"] == 100.1
    assert pos["live_stop"] == 99.0
    assert pos["entry_order_id"] == "PAPER-1"


def test_update_stop_ratchets(tmp_path):
    path = str(tmp_path / "s.json")
    state.open_position(path, _plan(), session_date=date(2026, 7, 6),
                        entry_order_id="x", fill_price=100.0)
    state.update_stop(path, 100.0)
    assert state.get_position(path)["live_stop"] == 100.0


def test_close_position_loss_increments_streak(tmp_path):
    path = str(tmp_path / "s.json")
    state.open_position(path, _plan(), session_date=date(2026, 7, 6),
                        entry_order_id="x", fill_price=100.0)
    state.close_position(path, result_date=date(2026, 7, 6), is_loss=True)
    st = state.load(path)
    assert st["position"] is None
    assert st["loss_streak"] == 1


def test_close_position_win_resets_streak(tmp_path):
    path = str(tmp_path / "s.json")
    state.close_position(path, result_date=date(2026, 7, 6), is_loss=True)
    state.close_position(path, result_date=date(2026, 7, 7), is_loss=True)
    assert state.load(path)["loss_streak"] == 2
    state.close_position(path, result_date=date(2026, 7, 8), is_loss=False)
    assert state.load(path)["loss_streak"] == 0


def test_close_position_same_day_not_double_counted(tmp_path):
    path = str(tmp_path / "s.json")
    state.close_position(path, result_date=date(2026, 7, 6), is_loss=True)
    state.close_position(path, result_date=date(2026, 7, 6), is_loss=True)
    assert state.load(path)["loss_streak"] == 1


def test_set_and_parse_halt(tmp_path):
    path = str(tmp_path / "s.json")
    state.set_halt(path, date(2026, 7, 13))
    assert state.parse_halt_until(state.load(path)) == date(2026, 7, 13)
    state.set_halt(path, None)
    assert state.parse_halt_until(state.load(path)) is None


def test_record_exit_attempt_increments_and_persists(tmp_path):
    path = str(tmp_path / "s.json")
    state.open_position(path, _plan(), session_date=date(2026, 7, 6),
                        entry_order_id="x", fill_price=100.0)
    assert state.record_exit_attempt(path) == 1
    assert state.record_exit_attempt(path) == 2
    assert state.get_position(path)["exit_fail_count"] == 2


def test_record_exit_attempt_no_position_returns_zero(tmp_path):
    path = str(tmp_path / "s.json")
    assert state.record_exit_attempt(path) == 0


def test_mark_carryover_alerted_once_per_day(tmp_path):
    path = str(tmp_path / "s.json")
    state.open_position(path, _plan(), session_date=date(2026, 7, 6),
                        entry_order_id="x", fill_price=100.0)
    assert state.mark_carryover_alerted(path, date(2026, 7, 7)) is True
    assert state.mark_carryover_alerted(path, date(2026, 7, 7)) is False
    assert state.mark_carryover_alerted(path, date(2026, 7, 8)) is True


def test_mark_carryover_alerted_no_position_returns_false(tmp_path):
    path = str(tmp_path / "s.json")
    assert state.mark_carryover_alerted(path, date(2026, 7, 7)) is False


# ── realized P&L / drawdown ──────────────────────────────────────────────────
def test_record_realized_pnl_accumulates_same_day(tmp_path):
    path = str(tmp_path / "s.json")
    today_pnl, hwm = state.record_realized_pnl(path, pnl=-500.0, today=date(2026, 7, 6), capital=100_000.0)
    assert today_pnl == -500.0
    today_pnl, hwm = state.record_realized_pnl(path, pnl=-300.0, today=date(2026, 7, 6), capital=100_000.0)
    assert today_pnl == -800.0
    assert hwm == 100_000.0  # never above capital yet


def test_record_realized_pnl_resets_on_date_rollover(tmp_path):
    path = str(tmp_path / "s.json")
    state.record_realized_pnl(path, pnl=-800.0, today=date(2026, 7, 6), capital=100_000.0)
    today_pnl, _ = state.record_realized_pnl(path, pnl=200.0, today=date(2026, 7, 7), capital=100_000.0)
    assert today_pnl == 200.0  # yesterday's -800 not carried forward


def test_record_realized_pnl_high_water_mark_only_rises(tmp_path):
    path = str(tmp_path / "s.json")
    _, hwm1 = state.record_realized_pnl(path, pnl=2000.0, today=date(2026, 7, 6), capital=100_000.0)
    assert hwm1 == 102_000.0
    _, hwm2 = state.record_realized_pnl(path, pnl=-5000.0, today=date(2026, 7, 7), capital=100_000.0)
    assert hwm2 == 102_000.0  # a later loss never lowers the HWM
    st = state.load(path)
    assert st["cumulative_realized_pnl"] == -3000.0


def test_set_and_check_drawdown_halt(tmp_path):
    path = str(tmp_path / "s.json")
    assert state.is_drawdown_halted(state.load(path)) is False
    state.set_drawdown_halt(path, True)
    assert state.is_drawdown_halted(state.load(path)) is True
    state.set_drawdown_halt(path, False)
    assert state.is_drawdown_halted(state.load(path)) is False
