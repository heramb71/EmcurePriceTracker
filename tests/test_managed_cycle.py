"""Tests for src/managed_cycle.py — the pure decision core (choose_target,
decide) and the dry-run step() that must place no orders."""
from __future__ import annotations

import pytest

import src.emcure.managed_cycle as mc
from src.emcure.managed_cycle import ManagedConfig, choose_target, decide, format_levels_block
from src.emcure.predictor import format_pre_open_briefing


def _cfg(**over) -> ManagedConfig:
    base = dict(
        enabled=True, live=False, targets=(15.0, 20.0, 30.0),
        sl_rupees=100.0, qty=8, reentry_gap=20.0, reach_min_prob=50.0,
        max_daily_loss=800.0, reentry_cooldown_min=60.0, block_reentry_after_stop=True,
    )
    base.update(over)
    return ManagedConfig(**base)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_STATE_FILE", str(tmp_path / "managed_state.json"))


# ── choose_target: dynamic, probability-based ────────────────────────────────

def test_choose_target_picks_highest_above_threshold():
    probs = {1748.10: 80, 1753.10: 60, 1763.10: 30}    # T1,T2 clear 50; T3 doesn't
    t = choose_target(1733.10, probs, _cfg(reach_min_prob=50))
    assert t["delta"] == 20.0 and t["price"] == 1753.10   # highest among the likely


def test_choose_target_falls_back_to_most_likely():
    probs = {1748.10: 40, 1753.10: 25, 1763.10: 10}    # none clear 50
    t = choose_target(1733.10, probs, _cfg(reach_min_prob=50))
    assert t["delta"] == 15.0 and t["price"] == 1748.10   # the most likely (T1)


def test_choose_target_promotes_higher_as_probs_rise():
    probs = {1748.10: 90, 1753.10: 70, 1763.10: 55}    # all clear 50
    t = choose_target(1733.10, probs, _cfg(reach_min_prob=50))
    assert t["delta"] == 30.0                              # highest profit among likely


# ── decide: holding ──────────────────────────────────────────────────────────

def _pos(entry=1733.10, qty=8, sl=1633.10):
    return {"entry": entry, "qty": qty, "sl": sl}


# The held-position exit is now a mechanical touched-target FLOOR (no probability
# gating). Ladder off entry 1733.10 → T1 1748.10, T2 1753.10, T3 1763.10.

def test_decide_sell_when_top_target_reached():
    market = {"price": 1762, "day_high": 1764, "day_low": 1740}      # high clears T3
    d = decide(_pos(), market, _cfg())
    assert d.action == "sell" and d.price == 1763.10 and d.qty == 8


def test_decide_exit_sl_takes_priority_over_target():
    market = {"price": 1632, "day_high": 1800, "day_low": 1632}
    d = decide(_pos(), market, _cfg())
    assert d.action == "exit_sl" and d.price == 1633.10


def test_decide_hold_for_first_target_when_nothing_touched():
    market = {"price": 1740, "day_high": 1745, "day_low": 1735}      # below T1
    d = decide(_pos(), market, _cfg())
    assert d.action == "hold" and d.label == "+₹15"                  # waiting on first rung


def test_decide_books_touched_t2_floor_on_pullback():
    # The live bug: high prints T2 (1753.10), T3 (1763.10) never hits, price slips
    # back below T2 → SELL at market, booking the touched +₹20 floor.
    market = {"price": 1750, "day_high": 1758, "day_low": 1748}
    d = decide(_pos(), market, _cfg())
    assert d.action == "sell" and d.label == "+₹20" and d.price == 1750


def test_decide_rides_above_touched_floor_toward_next_rung():
    # T2 touched but price still ABOVE it → ride toward T3, +₹20 locked as the floor.
    market = {"price": 1756, "day_high": 1758, "day_low": 1750}
    d = decide(_pos(), market, _cfg())
    assert d.action == "hold" and d.label == "+₹30"                  # aiming next rung


def test_decide_today_scenario_pullback_to_t2_books_it():
    # Exact 2026-06-18 case: entry 1733.10, day high 1758 (T2 touched, T3 missed).
    # A pullback to/under the T2 rung books +₹20 — not the old bug holding for T3.
    d = decide(_pos(), {"price": 1753.10, "day_high": 1758, "day_low": 1748}, _cfg())
    assert d.action == "sell" and d.label == "+₹20" and d.price == 1753.10


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
    block = format_levels_block(_cfg(), pos, sma7=1740.0)
    assert "holding 8 sh @ ₹1,733.10" in block
    assert "T1  ₹1,748.10" in block and "T3  ₹1,763.10" in block   # +15 / +30
    assert "Stop  ₹1,633.10" in block
    assert "+₹30" in block                                          # T3 ladder line


def test_levels_block_flat_shows_reentry_trigger():
    block = format_levels_block(_cfg(), None, sma7=1740.0)
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


def test_levels_block_includes_reach_odds_and_chosen_target():
    pos = {"entry": 1733.10, "qty": 8, "sl": 1633.10}
    probs = {1748.10: 85, 1753.10: 78, 1763.10: 72, "stop": 18}
    block = format_levels_block(_cfg(reach_min_prob=50), pos, sma7=1740.0, probs=probs)
    assert "85%" in block and "72%" in block and "18%" in block
    assert "reaching from the live price" in block
    assert "Aiming to sell all at +₹30" in block      # all clear 50 → aim T3


# ── Phase 2 safety guards: kill-switch, cooldown, stop-out, external close ────

from datetime import datetime, timedelta, timezone
from src.emcure.managed_cycle import reentry_blocked, set_position, get_position

_IST = timezone(timedelta(hours=5, minutes=30))
_NOW = datetime(2026, 6, 18, 11, 0, tzinfo=_IST)


class _FillBroker:
    def __init__(self, held, fill_price):
        self._held, self._fill, self.orders = held, fill_price, []
    def held_qty(self, ticker):
        return self._held
    def place_order_and_confirm(self, ticker, qty, side):
        self.orders.append((side, qty))
        return {"status": "COMPLETE", "fill_price": self._fill, "filled_qty": qty}


def test_reentry_blocked_after_stop_out():
    mc._save({"day": _NOW.date().isoformat(), "stopped_out_today": True})
    assert "stopped out" in reentry_blocked(_cfg(), _NOW)


def test_reentry_blocked_by_daily_loss_cap():
    mc._save({"day": _NOW.date().isoformat(), "realized_pnl_today": -850.0})
    assert "daily loss cap" in reentry_blocked(_cfg(max_daily_loss=800.0), _NOW)


def test_reentry_blocked_by_cooldown():
    mc._save({"day": _NOW.date().isoformat(),
              "last_exit_at": (_NOW - timedelta(minutes=20)).isoformat()})
    assert "cooldown" in reentry_blocked(_cfg(reentry_cooldown_min=60), _NOW)


def test_reentry_allowed_next_day_resets_guards():
    mc._save({"day": "2026-06-17", "stopped_out_today": True, "realized_pnl_today": -5000})
    assert reentry_blocked(_cfg(), _NOW) is None      # new day → clean slate


def test_live_stop_records_exit_and_blocks_reentry():
    set_position(1733.10, 8, _cfg())                  # sl = 1633.10
    broker = _FillBroker(held=8, fill_price=1633.0)
    market = {"price": 1632.0, "day_high": 1700.0, "day_low": 1632.0, "atr": 35.0}
    events = mc.step("EMCURE", market, broker, _cfg(live=True), now=_NOW)

    assert ("SELL", 8) in broker.orders                # real exit placed
    assert get_position() is None                      # position closed
    state = mc._load()
    assert state["stopped_out_today"] is True
    assert state["realized_pnl_today"] < 0
    assert reentry_blocked(_cfg(), _NOW + timedelta(minutes=5)) is not None  # halted


def test_step_clears_position_when_broker_flat():
    set_position(1733.10, 8, _cfg())
    broker = _FillBroker(held=0, fill_price=0.0)        # Zerodha shows nothing
    market = {"price": 1740.0, "day_high": 1745.0, "day_low": 1735.0, "atr": 35.0}
    events = mc.step("EMCURE", market, broker, _cfg(live=True), now=_NOW)

    assert [e[0] for e in events] == ["managed_closed_externally"]
    assert get_position() is None
    assert broker.orders == []                          # nothing sold — we held 0


def test_step_live_with_no_broker_places_nothing_and_keeps_position():
    """Regression for the 09:05 pre-open phantom sell: a live SELL decision with
    broker=None (the data-only pre-open _refresh) must NOT fabricate a fill,
    emit an event, or clear the position."""
    set_position(1733.10, 8, _cfg())
    # day_high already past the top target → decide() would return 'sell'.
    market = {"price": 1763.10, "day_high": 1776.0, "day_low": 1733.0, "atr": 35.0}
    events = mc.step("EMCURE", market, broker=None, cfg=_cfg(live=True), now=_NOW)

    assert events == []                 # no managed_sell announced
    assert get_position() is not None   # position untouched — not phantom-cleared


# ── Resting exchange stop-loss lifecycle (live only) ─────────────────────────

class _StopBroker:
    """Broker stub that records stop placement / cancel and can simulate order
    states + results, for the resting-stop lifecycle."""
    def __init__(self, held, avg=0.0, sell_fill=0.0):
        self._held, self._avg, self._sell_fill = held, avg, sell_fill
        self.stops, self.cancels, self.sells, self.buys = [], [], [], []
        self._n = 0
        self._states: dict = {}
        self._results: dict = {}

    def held_qty(self, ticker):
        return self._held

    @property
    def kite(self):
        b = self
        class _K:
            def holdings(self):
                return ([{"tradingsymbol": "EMCURE", "average_price": b._avg,
                          "quantity": b._held, "t1_quantity": 0}] if b._held else [])
            def positions(self):
                return {"net": []}
        return _K()

    def available_funds(self):
        return 1e9

    def place_stop_loss(self, ticker, qty, trigger, slippage_pct=0.1):
        self._n += 1
        oid = f"stop{self._n}"
        self.stops.append((qty, round(trigger, 2), oid))
        self._states[oid] = "TRIGGER PENDING"
        return oid

    def cancel(self, oid):
        self.cancels.append(oid)
        self._states[oid] = "CANCELLED"
        return True

    def order_state(self, oid):
        return self._states.get(oid)

    def order_result(self, oid):
        return self._results.get(oid, {"status": self._states.get(oid), "fill_price": 0.0, "filled_qty": 0})

    def place_order_and_confirm(self, ticker, qty, side):
        if side == "SELL":
            self.sells.append(qty)
            return {"status": "COMPLETE", "fill_price": self._sell_fill, "filled_qty": qty}
        self.buys.append(qty)
        return {"status": "COMPLETE", "fill_price": self._avg or 1700.0, "filled_qty": qty}


_HOLD_MKT = {"price": 1740, "day_high": 1745, "day_low": 1735, "atr": 35.0, "gap": 0, "trend_7d": "Upward"}


def test_live_adopt_places_resting_stop_at_sl():
    broker = _StopBroker(held=8, avg=1733.10)
    mc.step("EMCURE", _HOLD_MKT, broker, _cfg(live=True), now=_NOW)
    assert broker.stops and broker.stops[0][0] == 8 and broker.stops[0][1] == 1633.10
    assert get_position()["stop_order_id"] == "stop1"


def test_live_target_sell_cancels_resting_stop():
    broker = _StopBroker(held=8, avg=1733.10, sell_fill=1763.0)
    mc.step("EMCURE", _HOLD_MKT, broker, _cfg(live=True), now=_NOW)     # adopt + stop
    sid = get_position()["stop_order_id"]
    hit = {"price": 1764, "day_high": 1765, "day_low": 1740, "atr": 35.0}
    events = mc.step("EMCURE", hit, broker, _cfg(live=True), now=_NOW)  # target reached
    assert sid in broker.cancels         # resting stop cancelled before selling
    assert broker.sells == [8]
    assert get_position() is None
    assert any(e[0] == "managed_sell" for e in events)


def test_settle_books_stop_fill_when_broker_flat():
    set_position(1733.10, 8, _cfg())
    mc._update_position(stop_order_id="stop1")
    broker = _StopBroker(held=0)
    broker._results["stop1"] = {"status": "COMPLETE", "fill_price": 1633.0, "filled_qty": 8}
    market = {"price": 1632, "day_high": 1700, "day_low": 1632, "atr": 35.0}
    events = mc.step("EMCURE", market, broker, _cfg(live=True), now=_NOW)
    assert any(e[0] == "managed_sell" and e[1]["kind"] == "exit_sl" for e in events)
    assert get_position() is None
    st = mc._load()
    assert st["stopped_out_today"] is True and st["realized_pnl_today"] < 0


def test_ensure_stop_replaces_cancelled_stop():
    set_position(1733.10, 8, _cfg())
    mc._update_position(stop_order_id="stopX")
    broker = _StopBroker(held=8, avg=1733.10)
    broker._states["stopX"] = "CANCELLED"
    mc.step("EMCURE", _HOLD_MKT, broker, _cfg(live=True), now=_NOW)
    assert broker.stops                              # a fresh stop was placed
    assert get_position()["stop_order_id"] != "stopX"
