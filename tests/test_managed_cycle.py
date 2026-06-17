"""Tests for src/managed_cycle.py — the pure decision core (choose_target,
decide) and the dry-run step() that must place no orders."""
from __future__ import annotations

import pytest

import src.managed_cycle as mc
from src.managed_cycle import ManagedConfig, choose_target, decide, format_levels_block
from src.predictor import format_pre_open_briefing


def _cfg(**over) -> ManagedConfig:
    base = dict(
        enabled=True, live=False, targets=(15.0, 20.0, 30.0),
        sl_rupees=100.0, qty=8, reentry_gap=20.0, reach_atr_factor=1.0,
    )
    base.update(over)
    return ManagedConfig(**base)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_STATE_FILE", str(tmp_path / "managed_state.json"))


# ── choose_target: highest reachable by ATR ──────────────────────────────────

def test_choose_target_wide_day_picks_highest():
    t = choose_target(1733.10, atr=35.0, cfg=_cfg())   # 35 covers all deltas
    assert t["delta"] == 30.0 and t["price"] == 1763.10


def test_choose_target_mid_day_picks_middle():
    t = choose_target(1733.10, atr=22.0, cfg=_cfg())   # covers 15 & 20, not 30
    assert t["delta"] == 20.0 and t["price"] == 1753.10


def test_choose_target_quiet_day_none_reachable():
    assert choose_target(1733.10, atr=10.0, cfg=_cfg()) is None   # even +15 too far


# ── decide: holding ──────────────────────────────────────────────────────────

def _pos(entry=1733.10, qty=8, sl=1633.10):
    return {"entry": entry, "qty": qty, "sl": sl}


def test_decide_sell_when_high_reaches_chosen_target():
    market = {"price": 1762, "day_high": 1764, "day_low": 1740, "atr": 35.0}
    d = decide(_pos(), market, _cfg())
    assert d.action == "sell" and d.price == 1763.10 and d.qty == 8


def test_decide_exit_sl_takes_priority_over_target():
    # Even on a wide day, a stop breach exits first (capital protection).
    market = {"price": 1632, "day_high": 1800, "day_low": 1632, "atr": 35.0}
    d = decide(_pos(), market, _cfg())
    assert d.action == "exit_sl" and d.price == 1633.10


def test_decide_hold_when_target_not_reached():
    market = {"price": 1740, "day_high": 1745, "day_low": 1735, "atr": 35.0}
    d = decide(_pos(), market, _cfg())
    assert d.action == "hold" and d.label == "+₹30"


# ── decide: flat / re-entry ──────────────────────────────────────────────────

def test_decide_reenter_on_sma7_dip():
    market = {"price": 1700, "atr": 30.0, "gap": -22, "trend_7d": "Upward"}
    d = decide(None, market, _cfg())
    assert d.action == "reenter" and d.qty == 8 and d.price == 1700


def test_decide_no_reenter_in_downtrend():
    market = {"price": 1700, "atr": 30.0, "gap": -25, "trend_7d": "Downward"}
    assert decide(None, market, _cfg()).action == "wait"


def test_decide_wait_when_gap_too_small():
    market = {"price": 1730, "atr": 30.0, "gap": -5, "trend_7d": "Upward"}
    assert decide(None, market, _cfg()).action == "wait"


# ── step: dry-run never places orders, de-dups announcements ─────────────────

class _FakeBroker:
    """Minimal broker stub. Records any order attempts (there must be none in
    dry-run)."""
    def __init__(self, held=0, avg=0.0):
        self._held, self._avg = held, avg
        self.orders = []

    def held_qty(self, ticker):
        return self._held

    @property
    def kite(self):
        broker = self
        class _K:
            def holdings(self):
                return [{"tradingsymbol": "EMCURE", "average_price": broker._avg,
                         "quantity": broker._held, "t1_quantity": 0}] if broker._held else []
            def positions(self):
                return {"net": []}
        return _K()

    def place_order_and_confirm(self, *a, **k):
        self.orders.append((a, k))
        return {"status": "COMPLETE", "fill_price": 0.0, "filled_qty": 0}


def test_step_dryrun_adopts_holding_and_announces_no_orders():
    broker = _FakeBroker(held=8, avg=1733.10)
    market = {"price": 1762, "day_high": 1764, "day_low": 1740, "atr": 35.0,
              "gap": 0, "trend_7d": "Upward"}
    events = mc.step("EMCURE", market, broker, _cfg(live=False))

    kinds = [e[0] for e in events]
    assert "managed_adopt" in kinds          # imported the 8-share holding
    assert "managed_dryrun" in kinds          # announced the would-SELL
    assert broker.orders == []                # DRY-RUN placed no real order

    # Second identical cycle must NOT re-announce (de-dup).
    events2 = mc.step("EMCURE", market, broker, _cfg(live=False))
    assert [e[0] for e in events2] == []


# ── Briefing levels block (managed ladder, not legacy +10/20/25) ─────────────

def test_levels_block_holding_shows_managed_ladder():
    pos = {"entry": 1733.10, "qty": 8, "sl": 1633.10}
    block = format_levels_block(_cfg(), pos, sma7=1740.0, atr=35.0)
    assert "holding 8 sh @ ₹1,733.10" in block
    assert "T1  ₹1,748.10" in block and "T3  ₹1,763.10" in block   # +15 / +30
    assert "Stop  ₹1,633.10" in block
    assert "+₹30" in block                                          # highest reachable at ATR 35


def test_levels_block_flat_shows_reentry_trigger():
    block = format_levels_block(_cfg(), None, sma7=1740.0, atr=30.0)
    assert "flat, watching to re-enter" in block
    assert "≤ ₹1,720.00" in block                                   # 1740 − reentry_gap 20


def test_pre_open_briefing_swaps_in_managed_block():
    sentinel = "🎯 *Managed plan — holding 8 sh @ ₹1,733.10*"
    msg = format_pre_open_briefing(
        ticker="EMCURE", price=1733.10, sma7=1740.0, trend_7d="Upward", atr=35.0,
        capital=100000, risk_rupees=4500, managed_block=sentinel,
    )
    assert sentinel in msg
    assert "Chance of +₹10 profit" not in msg     # legacy probability ladder replaced
    assert "Entry zones today" not in msg


def test_pre_open_briefing_legacy_unchanged_without_block():
    msg = format_pre_open_briefing(
        ticker="EMCURE", price=1700.0, sma7=1722.0, trend_7d="Upward", atr=30.0,
        capital=100000, risk_rupees=4500,
    )
    assert "Chance of +₹10 profit" in msg          # legacy path intact (backward compatible)
