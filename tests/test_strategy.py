"""Tests for the Supertrend strategy pure functions (sizing + management)."""
from __future__ import annotations

from src.emcure.strategy import (
    check_buy_gate,
    compute_position_size,
    manage_position,
    unrealised_pnl,
)

# ── Position sizing ──────────────────────────────────────────────────────────

def test_position_size_caps_qty_by_capital():
    # Arrange — generous risk budget, but capital only affords a few shares
    sizing = compute_position_size(capital=15000, risk_pct=4.0, entry=1700.0, atr=30.0)
    # Assert — qty * entry must never exceed capital
    assert sizing is not None
    assert sizing["qty"] * sizing["entry"] <= 15000


def test_position_size_atr_scaled_targets():
    # Act
    sizing = compute_position_size(capital=100000, risk_pct=1.0, entry=1700.0, atr=30.0)
    # Assert — targets scale with ATR: entry + {1,2,3} × ATR
    assert sizing["t1"] == 1730.0   # +1 × 30
    assert sizing["t2"] == 1760.0   # +2 × 30
    assert sizing["t3"] == 1790.0   # +3 × 30
    assert sizing["sl"] == 1670.0   # −1 × 30


def test_position_size_returns_none_on_invalid_input():
    assert compute_position_size(0, 1.0, 1700.0, 30.0) is None
    assert compute_position_size(100000, 1.0, 0.0, 30.0) is None
    assert compute_position_size(100000, 1.0, 1700.0, 0.0) is None


# ── Position management (exit priority) ──────────────────────────────────────

_POS = {
    "entry": 1700.0, "sl": 1685.0, "t1": 1710.0, "t2": 1720.0, "t3": 1725.0,
    "qty": 6, "qty_remaining": 6,
    "partial_booked": False, "t2_booked": False, "t3_booked": False,
}


def test_manage_stop_takes_priority():
    action = manage_position(dict(_POS), price=1680.0, supertrend_value=1650.0, supertrend_direction=1)
    assert action["action"] == "exit_full"
    assert action["reason"] == "stop_hit"


def test_manage_supertrend_flip_exits_full():
    action = manage_position(dict(_POS), price=1705.0, supertrend_value=1710.0, supertrend_direction=-1)
    assert action["action"] == "exit_full"
    assert action["reason"] == "supertrend_exit"


def test_manage_t1_then_t2_then_t3_in_order():
    # T1 only
    a1 = manage_position(dict(_POS), 1712.0, supertrend_value=1650.0, supertrend_direction=1)
    assert a1["reason"] == "t1_hit"
    # T3 reached but none booked → highest unbooked target fires first (t3)
    a3 = manage_position(dict(_POS), 1730.0, supertrend_value=1650.0, supertrend_direction=1)
    assert a3["reason"] == "t3_hit"


def test_manage_skips_already_booked_targets():
    pos = dict(_POS)
    pos["partial_booked"] = True   # t1 done
    pos["t3_booked"] = True         # t3 done
    action = manage_position(pos, 1722.0, supertrend_value=1650.0, supertrend_direction=1)
    assert action["reason"] == "t2_hit"


def test_manage_holds_when_nothing_triggered():
    assert manage_position(dict(_POS), 1705.0, supertrend_value=1650.0, supertrend_direction=1) is None


def test_unrealised_pnl_uses_remaining_qty():
    pos = dict(_POS)
    pos["qty_remaining"] = 4
    assert unrealised_pnl(pos, 1710.0) == 40.0  # (1710-1700)*4


# ── Buy gate ─────────────────────────────────────────────────────────────────

def test_buy_gate_triggers_on_trend_momentum_volume():
    quote = {"price": 1700.0, "volume": 200000}
    indicators = {"ema20": 1690.0, "rsi": 55.0, "avg_volume": 100000}
    st = {"supertrend": 1650.0, "direction": 1}
    candle = {"open": 1695, "high": 1705, "low": 1690, "close": 1702}
    result = check_buy_gate(quote, indicators, st, candle, "Trending Up")
    assert result["triggered"] is True


def test_buy_gate_blocks_on_downtrend():
    quote = {"price": 1700.0, "volume": 200000}
    indicators = {"ema20": 1690.0, "rsi": 55.0, "avg_volume": 100000}
    st = {"supertrend": 1750.0, "direction": -1}
    candle = {"open": 1695, "high": 1705, "low": 1690, "close": 1702}
    result = check_buy_gate(quote, indicators, st, candle, "Trending Down")
    assert result["triggered"] is False


def test_buy_gate_blocks_in_sideways_regime():
    # All technicals pass, but regime is Sideways → hard-blocked
    quote = {"price": 1700.0, "volume": 200000}
    indicators = {"ema20": 1690.0, "rsi": 55.0, "avg_volume": 100000}
    st = {"supertrend": 1650.0, "direction": 1}
    candle = {"open": 1695, "high": 1705, "low": 1690, "close": 1702}
    result = check_buy_gate(quote, indicators, st, candle, "Sideways")
    assert result["triggered"] is False
    assert result["conditions"]["regime_ok"] is False
