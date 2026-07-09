"""Tests for src/execution/broker.py — held_qty's reading of Kite's
positions + holdings buckets.

Regression context (2026-07-09 live incident): selling delivery holdings
intraday decrements the holdings 'quantity' AND books a −qty CNC position for
the same shares. held_qty summed only quantity + t1_quantity, so a routine
holdings sale read as net short (−8) for the rest of the day — falsely
blocking every re-entry with a reconcile warning. used_quantity ("quantity
sold from the net holding", per the Kite Connect portfolio docs) restores the
identity: quantity + t1 + used + net_position = shares actually owned.
"""
from __future__ import annotations

from src.execution.broker import KiteBroker


class _StubKite:
    def __init__(self, positions=None, holdings=None, raise_on=None):
        self._positions = positions or []
        self._holdings = holdings or []
        self._raise_on = raise_on or set()

    def positions(self):
        if "positions" in self._raise_on:
            raise ConnectionError("kite down")
        return {"net": self._positions}

    def holdings(self):
        if "holdings" in self._raise_on:
            raise ConnectionError("kite down")
        return self._holdings


def _broker(**stub_kwargs) -> KiteBroker:
    b = KiteBroker.__new__(KiteBroker)          # skip __init__ (needs kiteconnect)
    b.kite = _StubKite(**stub_kwargs)
    return b


def _pos(qty, symbol="EMCURE", product="CNC"):
    return {"tradingsymbol": symbol, "product": product, "quantity": qty}


def _hold(quantity=0, t1=0, used=0, symbol="EMCURE"):
    return {"tradingsymbol": symbol, "quantity": quantity,
            "t1_quantity": t1, "used_quantity": used}


# ── the 2026-07-09 incident ──────────────────────────────────────────────────

def test_sold_holding_day_reads_flat_not_short():
    """Sold 8 demat shares intraday: positions −8, holdings quantity 0 with
    used_quantity 8. Economically flat — must read 0, not −8."""
    b = _broker(positions=[_pos(-8)], holdings=[_hold(quantity=0, used=8)])
    assert b.held_qty("EMCURE") == 0


def test_partial_holding_sale_reads_remainder():
    b = _broker(positions=[_pos(-4)], holdings=[_hold(quantity=4, used=4)])
    assert b.held_qty("EMCURE") == 4


def test_true_short_still_reads_negative():
    """Double sell (8 from holdings + 8 naked): positions −16, holdings used 8.
    A REAL short must stay visible so the buy guard can page loudly."""
    b = _broker(positions=[_pos(-16)], holdings=[_hold(quantity=0, used=8)])
    assert b.held_qty("EMCURE") == -8


# ── unchanged behaviours ─────────────────────────────────────────────────────

def test_plain_demat_holding():
    b = _broker(holdings=[_hold(quantity=8)])
    assert b.held_qty("EMCURE") == 8


def test_unsettled_t1_holding():
    b = _broker(holdings=[_hold(t1=8)])
    assert b.held_qty("EMCURE") == 8


def test_bought_today_intraday_position_only():
    b = _broker(positions=[_pos(8)])
    assert b.held_qty("EMCURE") == 8


def test_ignores_other_symbols_and_non_cnc():
    b = _broker(positions=[_pos(5, symbol="ICICIBANK"), _pos(3, product="MIS")],
                holdings=[_hold(quantity=9, symbol="SUZLON")])
    assert b.held_qty("EMCURE") == 0


def test_query_error_returns_none():
    b = _broker(raise_on={"holdings"})
    assert b.held_qty("EMCURE") is None
