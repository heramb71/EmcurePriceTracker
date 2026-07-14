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

def test_decide_sell_when_top_target_reached_and_price_still_there():
    # Current price is still at/above T3 — sell at current price.
    market = {"price": 1764, "day_high": 1764, "day_low": 1740}
    d = decide(_pos(), market, _cfg())
    assert d.action == "sell" and d.price == 1764 and d.qty == 8
    assert "target" in d.reason.lower()


def test_decide_sell_when_top_touched_intraday_price_pulled_back():
    # day_high cleared T3 (1763.10) but current price has fallen back below it.
    # Should still sell (at current price), not claim "top target reached".
    market = {"price": 1762, "day_high": 1764, "day_low": 1740}
    d = decide(_pos(), market, _cfg())
    assert d.action == "sell" and d.price == 1762 and d.qty == 8
    assert "pulled back" in d.reason.lower()
    assert "target" not in d.reason.lower()


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


def test_step_ignores_pre_entry_day_high():
    """Regression: stock opened ~₹1850 (above top target), dipped ₹28, entry
    at ₹1819.70. First cycle after fill must NOT immediately sell because
    day_high from yfinance includes the pre-entry morning spike."""
    set_position(1819.70, 8, _cfg(targets=(15.0, 20.0, 30.0), sl_rupees=100))
    # price is barely off entry; day_high carries the morning high well above top target
    market = {"price": 1819.80, "day_high": 1860.0, "day_low": 1810.0,
              "atr": 35.0, "gap": -28, "trend_7d": "Upward"}
    events = mc.step("EMCURE", market, None, _cfg(live=False))
    kinds = [e[0] for e in events]
    assert "managed_dryrun" not in kinds   # must NOT fire a sell


class _LtpBroker:
    """Authenticated broker stub returning a fixed real-time LTP (no orders)."""
    def __init__(self, ltp: float, held: int):
        self._ltp, self._held = ltp, held
    def is_authenticated(self) -> bool:
        return True
    def get_ltp(self, ticker: str) -> float:
        return self._ltp
    def held_qty(self, ticker: str):
        return self._held


def test_step_decides_on_broker_ltp_not_stale_quote():
    """The managed cycle must act on the broker's real-time LTP, not the
    (up-to-~15-min stale) yfinance price in `market`. Here the quote still shows
    ~entry, but the live LTP has reached the top target → must sell."""
    set_position(1800.0, 8, _cfg(targets=(15.0, 20.0, 30.0), sl_rupees=30))
    stale = {"price": 1801.0, "day_high": 1801.0, "day_low": 1795.0,
             "atr": 30.0, "gap": 0, "trend_7d": "Upward"}
    broker = _LtpBroker(ltp=1831.0, held=8)          # 1831 ≥ top target 1830
    events = mc.step("EMCURE", stale, broker, _cfg(live=False))
    dry = [e[1] for e in events if e[0] == "managed_dryrun"]
    assert dry, "should have fired a decision off the live LTP"
    assert dry[0]["decision"] == "sell"
    assert "target" in dry[0]["reason"].lower()


def test_step_keeps_quote_price_when_broker_unauthenticated():
    """If the broker isn't authenticated, fall back to the passed quote price
    (get_ltp is never trusted) — no spurious LTP-driven exit."""
    set_position(1800.0, 8, _cfg(targets=(15.0, 20.0, 30.0), sl_rupees=30))
    stale = {"price": 1801.0, "day_high": 1801.0, "day_low": 1795.0,
             "atr": 30.0, "gap": 0, "trend_7d": "Upward"}
    # No is_authenticated attr at all → treated as unauthenticated, LTP ignored.
    events = mc.step("EMCURE", stale, None, _cfg(live=False))
    assert "managed_dryrun" not in [e[0] for e in events]   # holds


# ── Full exit matrix ─────────────────────────────────────────────────────────
# Entry ₹1000, targets +10/+20/+30 → T1 1010, T2 1020, T3(top) 1030, stop 900.
# `high` here is high_since_entry (only highs seen AFTER entry — step() guarantees
# this by shadowing day_high). Each row: which post-entry high we've seen and
# where the price is now → the decision decide() must make.
def _mp(entry=1000.0, qty=8, sl=900.0):
    return {"entry": entry, "qty": qty, "sl": sl}


def _mcfg():
    return _cfg(targets=(10.0, 20.0, 30.0), sl_rupees=100.0)


@pytest.mark.parametrize("high, price, low, action, why", [
    # ── nothing reached yet → hold for the first target
    (1000.0, 1000.0, 1000.0, "hold", "flat at entry"),
    (1008.0, 1008.0,  998.0, "hold", "climbing, below T1"),
    # ── T1 reached, still climbing (price above the T1 floor) → hold, ride to T2
    (1015.0, 1015.0, 1000.0, "hold", "above T1 floor, riding to T2"),
    # ── T1 reached, price back at/below T1 → sell (book ~+10)
    (1010.0, 1010.0, 1000.0, "sell", "sitting on the T1 floor"),
    (1015.0, 1006.0, 1000.0, "sell", "pulled back under T1"),
    # ── T2 reached, still climbing between T2 and T3 → hold, ride to T3
    (1025.0, 1025.0, 1000.0, "hold", "above T2 floor, riding to T3"),
    # ── T2 reached, price back at/below T2 → sell (book ~+20)
    (1025.0, 1018.0, 1000.0, "sell", "pulled back under T2"),
    (1020.0, 1020.0, 1000.0, "sell", "sitting on the T2 floor"),
    # ── top reached, price still at/above top → sell "reached the target"
    (1030.0, 1030.0, 1000.0, "sell", "at the top target"),
    (1035.0, 1032.0, 1000.0, "sell", "above the top target"),
    # ── top reached via the high, price has slipped below it → sell (pulled back)
    (1030.0, 1020.0, 1000.0, "sell", "top touched then pulled back"),
    # ── stop takes priority over any target
    (1030.0,  895.0,  890.0, "exit_sl", "price under the stop"),
    (1015.0, 1005.0,  899.0, "exit_sl", "day-low pierced the stop"),
])
def test_decide_exit_matrix(high, price, low, action, why):
    d = decide(_mp(), {"price": price, "day_high": high, "day_low": low}, _mcfg())
    assert d.action == action, f"{why}: expected {action}, got {d.action} — {d.reason}"


# Sell sub-type wording, so the message says the right thing (not just "sell").
@pytest.mark.parametrize("high, price, needle", [
    (1030.0, 1030.0, "target"),        # reached the top
    (1030.0, 1020.0, "pulled back"),   # top touched, slipped below
    (1025.0, 1018.0, "booking"),       # pulled back to the T2 floor
])
def test_decide_sell_reason_wording(high, price, needle):
    d = decide(_mp(), {"price": price, "day_high": high, "day_low": 1000.0}, _mcfg())
    assert d.action == "sell" and needle in d.reason.lower()


# ── Timing: WHEN the high happened, verified through step() (which shadows
# day_high with high_since_entry). This is the crux of the phantom-sell bug. ──
def _dry(market):
    return mc.step("EMCURE", market, None, _mcfg())


def test_high_before_entry_is_ignored():
    """Bought AFTER the day's spike: a session high above the top target that
    printed BEFORE entry must be ignored — the first cycle holds, not sells."""
    set_position(1000.0, 8, _mcfg())
    ev = _dry({"price": 1001.0, "day_high": 1035.0, "day_low": 998.0})   # spike was pre-entry
    assert "managed_dryrun" not in [e[0] for e in ev]                     # holds


def test_high_after_entry_reaches_only_t2_then_pulls_back_sells_at_t2():
    """Bought BEFORE the day's high; the post-entry high reaches only T2 (not the
    top), then price pulls back → sell locking the T2 floor, never the top."""
    set_position(1000.0, 8, _mcfg())
    hold = _dry({"price": 1025.0, "day_high": 1025.0, "day_low": 1000.0})  # climbs past T2
    assert "managed_dryrun" not in [e[0] for e in hold]                    # rides toward T3
    ev = _dry({"price": 1015.0, "day_high": 1015.0, "day_low": 1000.0})    # slips below T2
    d = [e[1] for e in ev if e[0] == "managed_dryrun"]
    assert d and d[0]["decision"] == "sell" and "1,020" in d[0]["reason"]  # booked the T2 floor


def test_high_after_entry_reaches_only_t1_then_pulls_back_sells_at_t1():
    set_position(1000.0, 8, _mcfg())
    _dry({"price": 1015.0, "day_high": 1015.0, "day_low": 1000.0})         # touches T1, rides
    ev = _dry({"price": 1006.0, "day_high": 1006.0, "day_low": 1000.0})    # slips below T1
    d = [e[1] for e in ev if e[0] == "managed_dryrun"]
    assert d and d[0]["decision"] == "sell" and "1,010" in d[0]["reason"]


def test_high_after_entry_reaches_top_sells_reached():
    set_position(1000.0, 8, _mcfg())
    ev = _dry({"price": 1030.0, "day_high": 1030.0, "day_low": 1000.0})
    d = [e[1] for e in ev if e[0] == "managed_dryrun"]
    assert d and d[0]["decision"] == "sell" and "target" in d[0]["reason"].lower()


def test_touched_floor_never_given_back_while_price_stays_above():
    """Once T2 is touched, staying just above it must keep holding (ride up),
    not churn a sell — the floor only sells on a real pullback through it."""
    set_position(1000.0, 8, _mcfg())
    _dry({"price": 1025.0, "day_high": 1025.0, "day_low": 1000.0})         # touch T2
    ev = _dry({"price": 1022.0, "day_high": 1022.0, "day_low": 1000.0})    # still above T2
    assert "managed_dryrun" not in [e[0] for e in ev]                      # holds


def test_stop_fires_even_after_a_high_was_seen():
    set_position(1000.0, 8, _mcfg())
    _dry({"price": 1025.0, "day_high": 1025.0, "day_low": 1000.0})         # saw T2
    ev = _dry({"price": 895.0, "day_high": 1025.0, "day_low": 890.0})      # then crashed
    d = [e[1] for e in ev if e[0] == "managed_dryrun"]
    assert d and d[0]["decision"] == "exit_sl"


def test_step_dryrun_adopts_holding_and_announces_no_orders():
    broker = _FakeBroker(held=8, avg=1733.10)
    # price=1764 puts current price above T3 (1763.10) so the post-entry high
    # alone triggers the sell — independent of any pre-entry session high.
    market = {"price": 1764, "day_high": 1800, "day_low": 1740, "atr": 35.0,
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
    assert "Holding 8 shares" in block and "₹1,733.10" in block
    assert "1,763.10" in block          # aiming for the top target (+₹30)
    assert "1,633.10" in block          # safety exit


def test_levels_block_flat_shows_reentry_trigger():
    block = format_levels_block(_cfg(), None, sma7=1740.0)
    assert "watching to buy" in block
    assert "1,720.00" in block                                      # 1740 − reentry_gap 20


def test_pre_open_briefing_swaps_in_managed_block():
    sentinel = "🎯 *Managed plan — holding 8 sh @ ₹1,733.10*"
    msg = format_pre_open_briefing(
        ticker="EMCURE", price=1733.10, sma7=1740.0, trend_7d="Upward", atr=35.0,
        capital=100000, risk_rupees=4500, managed_block=sentinel,
    )
    assert sentinel in msg
    assert "Odds today" not in msg                 # legacy probability ladder replaced
    assert "Buy if it dips" not in msg


def test_pre_open_briefing_legacy_unchanged_without_block():
    msg = format_pre_open_briefing(
        ticker="EMCURE", price=1700.0, sma7=1722.0, trend_7d="Upward", atr=30.0,
        capital=100000, risk_rupees=4500,
    )
    assert "Odds today" in msg                     # legacy path intact (backward compatible)


def test_levels_block_includes_reach_odds_and_chosen_target():
    pos = {"entry": 1733.10, "qty": 8, "sl": 1633.10}
    probs = {1748.10: 85, 1753.10: 78, 1763.10: 72, "stop": 18}
    block = format_levels_block(_cfg(reach_min_prob=50), pos, sma7=1740.0, probs=probs)
    assert "1,763.10" in block          # all clear 50 → aim for the top target (+₹30)
    assert "72%" in block               # its chance shown in plain words


# ── Phase 2 safety guards: kill-switch, cooldown, stop-out, external close ────

from datetime import datetime, timedelta, timezone

from src.emcure.managed_cycle import get_position, reentry_blocked, set_position

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


# ── Regression: session-wide day_low must not stop out a fresh entry ──────────
# Mirror of the day_high phantom-sell fix: yfinance day_low includes the
# PRE-ENTRY morning crash. Re-entries happen on dip days by definition, so a raw
# session low ≤ sl would fire an instant false exit_sl on the first cycle.

def test_low_before_entry_is_ignored():
    mc.set_position(1000.0, 8, _mcfg())                # sl = 900
    ev = _dry({"price": 1001.0, "day_high": 1002.0, "day_low": 880.0})  # crash was pre-entry
    assert "managed_dryrun" not in [e[0] for e in ev]                   # holds, no stop-out


def test_low_after_entry_still_stops_out():
    mc.set_position(1000.0, 8, _mcfg())
    _dry({"price": 1001.0, "day_high": 1002.0, "day_low": 880.0})       # pre-entry low ignored
    ev = _dry({"price": 899.0, "day_high": 1002.0, "day_low": 880.0})   # real crash while holding
    d = [e[1] for e in ev if e[0] == "managed_dryrun"]
    assert d and d[0]["decision"] == "exit_sl"


def test_low_since_entry_tracks_observed_prices():
    mc.set_position(1000.0, 8, _mcfg())
    _dry({"price": 905.0, "day_high": 1002.0, "day_low": 880.0})        # dips near (not through) sl
    pos = mc.get_position()
    assert pos["low_since_entry"] == 905.0                              # tracked from prices seen


# ── EXIT command: queued flag sells at the current price, then expires ────────

def test_exit_request_overrides_hold_and_is_consumed():
    mc.set_position(1000.0, 8, _mcfg())
    assert mc.request_exit() is not None
    ev = _dry({"price": 1004.0, "day_high": 1004.0, "day_low": 1000.0})  # would otherwise hold
    d = [e[1] for e in ev if e[0] == "managed_dryrun"]
    assert d and d[0]["decision"] == "sell" and "exit" in d[0]["reason"].lower()
    # Flag consumed — the next cycle is a normal hold, no repeat sell.
    ev2 = _dry({"price": 1004.0, "day_high": 1004.0, "day_low": 1000.0})
    assert "managed_dryrun" not in [e[0] for e in ev2]


def test_exit_request_when_flat_sets_nothing():
    assert mc.request_exit() is None
    ev = _dry({"price": 1000.0, "day_high": 1000.0, "day_low": 1000.0,
               "gap": -5, "trend_7d": "Upward"})
    assert ev == []                                                      # plain wait


# ── HALT / RESUME: block re-entries only, exits unaffected ────────────────────

def test_halt_blocks_reentry_until_resume():
    dip = {"price": 970.0, "day_high": 1000.0, "day_low": 965.0,
           "gap": -25, "trend_7d": "Choppy"}
    mc.set_halted(True)
    ev = _dry(dip)
    blocked = [e[1] for e in ev if e[0] == "managed_blocked"]
    assert blocked and "halt" in blocked[0]["reason"].lower()
    mc.set_halted(False)
    ev2 = _dry(dip)
    d = [e[1] for e in ev2 if e[0] == "managed_dryrun"]
    assert d and d[0]["decision"] == "reenter"


def test_halt_never_blocks_an_exit():
    mc.set_position(1000.0, 8, _mcfg())
    mc.set_halted(True)
    ev = _dry({"price": 895.0, "day_high": 1000.0, "day_low": 1000.0})   # live price under stop
    d = [e[1] for e in ev if e[0] == "managed_dryrun"]
    assert d and d[0]["decision"] == "exit_sl"


# ── Stop ratchet: the resting exchange stop follows the touched floor ─────────

def test_touched_floor_from_high_since_entry():
    pos = {"entry": 1000.0, "high_since_entry": 1005.0}
    assert mc._touched_floor(pos, _mcfg()) is None            # nothing printed yet
    pos = {"entry": 1000.0, "high_since_entry": 1022.0}
    assert mc._touched_floor(pos, _mcfg()) == 1020.0          # T2 is the floor
    pos = {"entry": 1000.0, "high_since_entry": 1031.0}
    assert mc._touched_floor(pos, _mcfg()) == 1030.0          # top rung


class _RatchetBroker:
    """Live-mode broker stub: tracks the resting stop, never fills orders."""
    def __init__(self, held=8):
        self._held = held
        self.cancelled: list[str] = []
        self.stops: list[tuple[int, float]] = []
        self._n = 0

    def held_qty(self, ticker):
        return self._held

    def is_authenticated(self):
        return False                       # keep the quote price (no LTP override)

    def order_state(self, order_id):
        return "TRIGGER PENDING"

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        return True

    def place_stop_loss(self, ticker, qty, trigger, **kw):
        self.stops.append((qty, round(trigger, 2)))
        self._n += 1
        return f"STOP{self._n}"

    def place_order_and_confirm(self, *a, **k):   # hold path must never sell
        raise AssertionError("order placed during a hold")


def test_hold_ratchets_resting_stop_to_touched_floor():
    cfg = ManagedConfig(
        enabled=True, live=True, targets=(10.0, 20.0, 30.0), sl_rupees=100.0,
        qty=8, reentry_gap=20.0, reach_min_prob=50.0, max_daily_loss=800.0,
        reentry_cooldown_min=60.0, block_reentry_after_stop=True,
    )
    mc.set_position(1000.0, 8, cfg)                   # sl = 900
    mc._update_position(stop_order_id="OLD", stop_trigger=900.0)
    broker = _RatchetBroker()

    # Price prints T2 (1020) and holds above it → the stop must lift 900 → 1020.
    mc.step("EMCURE", {"price": 1022.0, "day_high": 1022.0, "day_low": 1000.0},
            broker, cfg)
    assert broker.cancelled == ["OLD"]
    assert broker.stops == [(8, 1020.0)]
    assert mc.get_position()["stop_trigger"] == 1020.0

    # Same conditions again → already at the floor, no churn.
    mc.step("EMCURE", {"price": 1022.0, "day_high": 1022.0, "day_low": 1000.0},
            broker, cfg)
    assert len(broker.stops) == 1 and len(broker.cancelled) == 1


# ── Net-of-charges exits: tally, ledger, and return value ─────────────────────

def test_record_exit_books_net_into_day_tally_and_ledger(monkeypatch, tmp_path):
    from src.shared.costs import round_trip_charges
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    mc.set_position(1000.0, 8, _mcfg())
    now = mc._now_ist()

    net, charges = mc._record_exit(160.0, is_stop=False, now=now, ticker="EMCURE",
                                   exit_price=1020.0, live=True)
    expected_charges = round_trip_charges(1000.0, 1020.0, 8)
    assert charges == expected_charges
    assert net == round(160.0 - expected_charges, 2)
    assert mc._load()["realized_pnl_today"] == net       # kill-switch runs on net

    from src.emcure import ledger
    row = ledger.recent_trades(ledger.connect(), limit=1)[0]
    assert row["charges"] == expected_charges and row["net_pnl"] == net


# ── Percentage re-entry gap (opt-in; default unchanged) ───────────────────────

def test_pct_gap_scales_the_reentry_trigger():
    cfg = _cfg(reentry_gap_pct=1.4)
    flat_market = {"price": 1385.0, "day_high": 1400.0, "day_low": 1380.0,
                   "sma7": 1400.0, "trend_7d": "Choppy"}
    # threshold = 1400 × 1.4% = ₹19.60 → a ₹18 gap waits, a ₹20 gap re-enters.
    d = decide(None, {**flat_market, "gap": -18.0}, cfg)
    assert d.action == "wait"
    d = decide(None, {**flat_market, "gap": -20.0}, cfg)
    assert d.action == "reenter"


def test_rupee_gap_unchanged_when_pct_unset():
    d = decide(None, {"price": 1385.0, "gap": -20.0, "sma7": 1400.0,
                      "trend_7d": "Choppy"}, _cfg())
    assert d.action == "reenter"


# ── 2026-07-09 incident regressions ───────────────────────────────────────────
# Live incident: a sold-holding day read as net short (see tests/test_broker.py
# for the held_qty side). These cover the managed-cycle side: the sell path must
# never market-sell past a stop it could not cancel, and the buy path must page
# loudly (once) on a short reading instead of warning every cycle.

class _NoCancelBroker(_StopBroker):
    """Stop cancel always FAILS (e.g. order already COMPLETE at the exchange)."""
    def cancel(self, oid):
        self.cancels.append(oid)
        return False


def test_sell_books_stop_fill_instead_of_double_selling(monkeypatch, tmp_path):
    """Cancel fails because the ratcheted floor stop already filled → book THAT
    fill as the exit; placing the market sell too would go short (−8)."""
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    cfg = _cfg(live=True, sl_rupees=30.0)
    set_position(1800.0, 8, cfg)                       # sl = 1770
    mc._update_position(stop_order_id="stop1", stop_trigger=1815.0)   # ratcheted to T1
    broker = _NoCancelBroker(held=8)
    broker._results["stop1"] = {"status": "COMPLETE", "fill_price": 1815.1, "filled_qty": 8}

    events = mc._execute_sell("EMCURE", mc.Decision("sell", price=1815.0, qty=8),
                              broker, _NOW, cfg)

    assert broker.sells == []                          # NO second sell went out
    assert get_position() is None                      # exit booked, position closed
    ev = next(p for t, p in events if t == "managed_sell")
    assert ev["exit_price"] == 1815.1
    assert ev["kind"] == "sell"                        # ratcheted floor = profit exit…
    assert mc._load().get("stopped_out_today") is not True   # …not a stop-out block


def test_sell_books_protective_stop_fill_as_stop_out(monkeypatch, tmp_path):
    """Same race on the ORIGINAL entry−SL stop → still a real stop-out: kind
    exit_sl and the same-day re-entry block must engage."""
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    cfg = _cfg(live=True, sl_rupees=30.0)
    set_position(1800.0, 8, cfg)                       # sl = 1770
    mc._update_position(stop_order_id="stop1", stop_trigger=1770.0)
    broker = _NoCancelBroker(held=8)
    broker._results["stop1"] = {"status": "COMPLETE", "fill_price": 1769.5, "filled_qty": 8}

    events = mc._execute_sell("EMCURE", mc.Decision("exit_sl", price=1770.0, qty=8),
                              broker, _NOW, cfg)

    assert broker.sells == []
    ev = next(p for t, p in events if t == "managed_sell")
    assert ev["kind"] == "exit_sl"
    assert mc._load()["stopped_out_today"] is True


def test_sell_deferred_when_stop_uncancellable_and_not_dead():
    """Cancel fails and the stop still shows live at the exchange — hold off
    (position untouched) rather than risk both orders filling."""
    cfg = _cfg(live=True, sl_rupees=30.0)
    set_position(1800.0, 8, cfg)
    mc._update_position(stop_order_id="stop1", stop_trigger=1770.0)
    broker = _NoCancelBroker(held=8)
    broker._states["stop1"] = "TRIGGER PENDING"

    events = mc._execute_sell("EMCURE", mc.Decision("sell", price=1815.0, qty=8),
                              broker, _NOW, cfg)

    assert broker.sells == []
    assert get_position() is not None                  # retried next cycle
    assert [t for t, _ in events] == ["managed_exit_failed"]


def test_sell_proceeds_when_uncancellable_stop_is_already_dead(monkeypatch, tmp_path):
    """Cancel fails but the stop is confirmed CANCELLED/REJECTED → safe to sell."""
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    cfg = _cfg(live=True, sl_rupees=30.0)
    set_position(1800.0, 8, cfg)
    mc._update_position(stop_order_id="stop1", stop_trigger=1770.0)
    broker = _NoCancelBroker(held=8, sell_fill=1815.0)
    broker._states["stop1"] = "CANCELLED"

    events = mc._execute_sell("EMCURE", mc.Decision("sell", price=1815.0, qty=8),
                              broker, _NOW, cfg)

    assert broker.sells == [8]
    assert any(t == "managed_sell" for t, _ in events)


def test_buy_pages_short_warning_once_per_day():
    """held_qty < 0 → loud managed_short_warn, no order — and deduped so it
    doesn't re-alert every 5-min cycle (live incident sent the generic warn)."""
    broker = _StopBroker(held=-8)
    dec = mc.Decision("reenter", price=1810.0, qty=8)

    events = mc._execute_buy("EMCURE", dec, broker, _cfg(live=True), _NOW)
    assert broker.buys == []
    assert [t for t, _ in events] == ["managed_short_warn"]
    assert events[0][1]["held"] == -8

    assert mc._execute_buy("EMCURE", dec, broker, _cfg(live=True), _NOW) == []  # deduped
    msg = mc.format_managed_event("EMCURE", "managed_short_warn", events[0][1])
    assert "SHORT" in msg and "-8" in msg


def test_buy_reconcile_warning_dedupes_per_day():
    broker = _StopBroker(held=8)
    dec = mc.Decision("reenter", price=1810.0, qty=8)

    events = mc._execute_buy("EMCURE", dec, broker, _cfg(live=True), _NOW)
    assert broker.buys == []
    assert [t for t, _ in events] == ["managed_reconcile_warn"]
    assert mc._execute_buy("EMCURE", dec, broker, _cfg(live=True), _NOW) == []  # deduped


def test_step_short_account_blocks_reentry_and_pages():
    """End-to-end: flat + dip signal + broker reading short → no buy, one page."""
    broker = _StopBroker(held=-8)
    market = {"price": 1810.0, "day_high": 1850.0, "day_low": 1805.0,
              "gap": -25.0, "sma7": 1835.0, "trend_7d": "Choppy"}
    events = mc.step("EMCURE", market, broker, _cfg(live=True), now=_NOW)
    assert broker.buys == []
    assert any(t == "managed_short_warn" for t, _ in events)


# ── 2026-07-14 incident regressions ───────────────────────────────────────────
# Live incident: an OVERNIGHT hold's resting stop (a day-validity order) lapsed
# at the previous close. Its id no longer existed in the next session's order
# book — cancel raised and order_history returned state=None — so the
# uncancellable-stop guard deferred the exit every cycle, the "resting" stop
# protected nothing, and the alert claimed a sell order failed when none was
# ever placed. _NOW is 2026-06-18, so id prefix 260617… reads as yesterday.

_LAPSED_ID = "260617151274017"      # Kite ids embed placement date as YYMMDD
_TODAY_ID  = "260618151274017"


def test_stop_stale_from_placed_on_field():
    assert mc._stop_is_stale({"stop_order_id": "x1", "stop_placed_on": "2026-06-17"}, _NOW)
    assert not mc._stop_is_stale({"stop_order_id": "x1", "stop_placed_on": "2026-06-18"}, _NOW)


def test_stop_stale_falls_back_to_id_date_prefix():
    assert mc._stop_is_stale({"stop_order_id": _LAPSED_ID}, _NOW)
    assert not mc._stop_is_stale({"stop_order_id": _TODAY_ID}, _NOW)
    assert not mc._stop_is_stale({"stop_order_id": "stop1"}, _NOW)   # unparseable → not stale
    assert not mc._stop_is_stale({}, _NOW)                           # no stop at all


def test_sell_skips_cancel_when_stop_lapsed_overnight(monkeypatch, tmp_path):
    """The morning deadlock: cancel of a lapsed overnight stop can only fail
    with state=None. The sell must proceed without the cancel handshake."""
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    cfg = _cfg(live=True, sl_rupees=30.0)
    set_position(1795.35, 8, cfg)
    mc._update_position(stop_order_id=_LAPSED_ID, stop_trigger=1765.35)
    broker = _NoCancelBroker(held=8, sell_fill=1830.0)

    events = mc._execute_sell("EMCURE", mc.Decision("sell", price=1830.0, qty=8),
                              broker, _NOW, cfg)

    assert broker.cancels == []          # no cancel attempt against the dead id
    assert broker.sells == [8]
    assert get_position() is None
    assert any(t == "managed_sell" for t, _ in events)


def test_sell_still_deferred_for_same_day_unconfirmable_stop():
    """A SAME-day stop that can't be cancelled or confirmed dead may still be
    live at the exchange — the double-sell guard must keep deferring."""
    cfg = _cfg(live=True, sl_rupees=30.0)
    set_position(1800.0, 8, cfg)
    mc._update_position(stop_order_id=_TODAY_ID, stop_trigger=1770.0)
    broker = _NoCancelBroker(held=8)     # cancel fails, order_state → None

    events = mc._execute_sell("EMCURE", mc.Decision("sell", price=1815.0, qty=8),
                              broker, _NOW, cfg)

    assert broker.sells == []
    assert get_position() is not None
    assert [t for t, _ in events] == ["managed_exit_failed"]
    assert events[0][1].get("deferred") is True


def test_ensure_stop_replaces_overnight_lapsed_stop():
    """09:15 hold cycle: the recorded stop lapsed at yesterday's close but
    order_state=None used to read as 'still resting' — the position sat with
    no exchange protection. A stale stop must be re-placed fresh."""
    cfg = _cfg(live=True, sl_rupees=30.0)
    set_position(1795.35, 8, cfg)                      # sl = 1765.35
    mc._update_position(stop_order_id=_LAPSED_ID, stop_trigger=1765.35)
    broker = _StopBroker(held=8)

    new_id = mc._ensure_protective_stop("EMCURE", broker, cfg, _NOW)

    assert broker.cancels == []                        # dead id — never cancelled
    assert broker.stops and broker.stops[-1][1] == 1765.35
    pos = get_position()
    assert pos["stop_order_id"] == new_id != _LAPSED_ID
    assert pos["stop_placed_on"] == _NOW.date().isoformat()


class _SellFailBroker(_StopBroker):
    """Stop cancel succeeds but the market sell never confirms (timeout/reject)."""
    def place_order_and_confirm(self, ticker, qty, side):
        if side == "SELL":
            self.sells.append(qty)
            return None
        return super().place_order_and_confirm(ticker, qty, side)


def test_failed_sell_rearms_protective_stop():
    """Cancel-then-sell where the sell doesn't fill used to leave the position
    with NO resting stop until some later hold cycle. It must re-arm."""
    cfg = _cfg(live=True, sl_rupees=30.0)
    set_position(1795.35, 8, cfg)
    mc._update_position(stop_order_id=_TODAY_ID, stop_trigger=1765.35)
    broker = _SellFailBroker(held=8)

    events = mc._execute_sell("EMCURE", mc.Decision("sell", price=1830.0, qty=8),
                              broker, _NOW, cfg)

    assert [t for t, _ in events] == ["managed_exit_failed"]
    assert events[0][1].get("deferred") is None        # a real order was attempted
    assert get_position() is not None                  # position kept for retry
    assert broker.stops                                # …and protected again
    assert get_position()["stop_order_id"] == broker.stops[-1][2]


def test_buy_records_stop_placement_day():
    broker = _StopBroker(held=0)
    mc._execute_buy("EMCURE", mc.Decision("reenter", price=1796.8, qty=8),
                    broker, _cfg(live=True), _NOW)
    assert get_position()["stop_placed_on"] == _NOW.date().isoformat()


def test_exit_failed_message_distinguishes_deferred_from_failed_order():
    deferred = mc.format_managed_event("EMCURE", "managed_exit_failed",
                                       {"ticker": "EMCURE", "qty": 8, "reason": "", "deferred": True})
    failed   = mc.format_managed_event("EMCURE", "managed_exit_failed",
                                       {"ticker": "EMCURE", "qty": 8, "reason": ""})
    assert "held off" in deferred and "no sell" in deferred.lower()
    assert "didn't go through" in failed
