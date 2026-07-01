"""Tests for confirm-fill-then-record execution path (critical-path safety)."""
from __future__ import annotations

from apps import main
from src.emcure.state import _default_state


_QUOTE  = {"price": 1700.0, "volume": 100000}
_ST     = {"supertrend": 1650.0, "direction": 1, "atr": 30.0}
_BUY    = {"triggered": True, "conditions": {}, "details": {}}
_SIZING = {
    "entry": 1700.0, "sl": 1685.0, "t1": 1710.0, "t2": 1720.0, "t3": 1725.0,
    "qty": 5, "risk_per_share": 15.0, "risk_amount": 75.0, "capital_used": 8500.0,
    "atr": 30.0, "atr_mult": 0.5, "rr": 1.0,
}


class _FillBroker:
    """Broker stub that always reports a complete fill at a fixed price."""
    def __init__(self, fill_price=1702.5, held=0, funds=1_000_000.0):
        self.fill_price = fill_price
        self._held = held
        self._funds = funds
        self.orders = []

    def held_qty(self, ticker):
        return self._held

    def available_funds(self):
        return self._funds

    def place_order_and_confirm(self, ticker, qty, side):
        self.orders.append((side, qty))
        return {"order_id": "X", "status": "COMPLETE",
                "fill_price": self.fill_price, "filled_qty": qty}


class _FailBroker:
    """Broker stub whose orders never fill."""
    def __init__(self, held=0, funds=1_000_000.0):
        self._held = held
        self._funds = funds
        self.orders = []

    def held_qty(self, ticker):
        return self._held

    def available_funds(self):
        return self._funds

    def place_order_and_confirm(self, ticker, qty, side):
        self.orders.append((side, qty))
        return None


def test_simulation_open_uses_theoretical_price():
    # Arrange
    state = _default_state()
    # Act
    state, events = main._execute_strategy(
        state, "EMCURE", _QUOTE, _ST, _BUY, _SIZING, 30.0, halted=False, broker=None
    )
    # Assert
    assert state["position"] is not None
    assert events[0][0] == "open"
    assert state["position"]["entry"] == 1700.0


def test_confirmed_fill_records_actual_fill_price_and_targets():
    # Arrange
    state = _default_state()
    broker = _FillBroker(fill_price=1702.5)
    # Act — atr=30 → targets re-anchored off the real fill at +1/+2/+3 × ATR
    state, events = main._execute_strategy(
        state, "EMCURE", _QUOTE, _ST, _BUY, _SIZING, 30.0, halted=False, broker=broker
    )
    # Assert
    assert events[0][0] == "open"
    assert state["position"]["entry"] == 1702.5
    assert state["position"]["t1"] == 1732.5  # fill + 1 × ATR
    assert state["position"]["t2"] == 1762.5  # fill + 2 × ATR
    assert broker.orders == [("BUY", 5)]


def test_failed_buy_opens_no_position():
    # Arrange
    state = _default_state()
    # Act
    state, events = main._execute_strategy(
        state, "EMCURE", _QUOTE, _ST, _BUY, _SIZING, 30.0, halted=False, broker=_FailBroker()
    )
    # Assert
    assert state["position"] is None
    assert events[0][0] == "open_failed"


def test_failed_sell_retains_position():
    # Arrange — open a position first via a successful fill
    state = _default_state()
    state, _ = main._execute_strategy(
        state, "EMCURE", _QUOTE, _ST, _BUY, _SIZING, 30.0, halted=False, broker=_FillBroker()
    )
    # Act — price clears T1 (fill 1702.5 + 1×ATR = 1732.5) but the SELL never fills
    state, events = main._execute_strategy(
        state, "EMCURE", {"price": 1740.0, "volume": 1}, _ST,
        {"triggered": False}, None, 30.0, halted=False, broker=_FailBroker()
    )
    # Assert
    assert state["position"] is not None
    assert events[0][0] == "exit_failed"


def test_halted_does_not_open_position():
    # Arrange
    state = _default_state()
    # Act
    state, events = main._execute_strategy(
        state, "EMCURE", _QUOTE, _ST, _BUY, _SIZING, 30.0, halted=True, broker=_FillBroker()
    )
    # Assert
    assert state["position"] is None
    assert events == []


def test_insufficient_funds_skips_buy():
    # Arrange — broker reports less cash than the trade needs
    state = _default_state()
    broker = _FillBroker(funds=1000.0)  # need 8500
    # Act
    state, events = main._execute_strategy(
        state, "EMCURE", _QUOTE, _ST, _BUY, _SIZING, 30.0, halted=False, broker=broker
    )
    # Assert
    assert state["position"] is None
    assert events[0][0] == "insufficient_funds"
    assert broker.orders == []  # no order attempted


def test_untracked_holdings_skip_buy():
    # Arrange — bot is flat but broker already holds shares
    state = _default_state()
    broker = _FillBroker(held=10)
    # Act
    state, events = main._execute_strategy(
        state, "EMCURE", _QUOTE, _ST, _BUY, _SIZING, 30.0, halted=False, broker=broker
    )
    # Assert
    assert state["position"] is None
    assert events[0][0] == "reconcile_warn"
    assert broker.orders == []  # no order attempted


def test_near_event_blocks_buy():
    # Arrange
    state = _default_state()
    broker = _FillBroker()
    # Act
    state, events = main._execute_strategy(
        state, "EMCURE", _QUOTE, _ST, _BUY, _SIZING, 30.0,
        halted=False, broker=broker, near_event=True,
    )
    # Assert
    assert state["position"] is None
    assert events[0][0] == "event_blocked"
    assert broker.orders == []


def test_sizing_at_fill_keeps_filled_qty():
    # Act
    corrected = main._sizing_at_fill(_SIZING, fill_price=1702.5, filled_qty=4, atr=30.0)
    # Assert
    assert corrected["entry"] == 1702.5
    assert corrected["qty"] == 4
    assert corrected["capital_used"] == round(4 * 1702.5, 2)
