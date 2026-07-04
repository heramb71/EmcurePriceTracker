"""End-to-end engine flow on synthetic data with a fake broker.

Drives one full day: prepare → filter discards → opening-range breakout →
selection → sized paper entry → breakeven ratchet → target exit, asserting the
journal and persisted state at each stage. Market data is monkeypatched so the
test is deterministic and offline.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.kittybot import journal, marketdata, state
from src.kittybot.broker import BUY, Fill
from src.kittybot.engine import KittyBotEngine
from src.kittybot.filters import OpenQuote
from src.kittybot.opening_range import OpeningRange

from .conftest import make_config

TODAY = datetime(2026, 7, 6)  # a Monday


class FakeBroker:
    """Records orders and fills at a controllable price."""

    name = "fake"

    def __init__(self, price: float):
        self.price = price
        self.orders: list[tuple[str, int, str]] = []
        self._seq = 0

    def get_ltp(self, symbol):
        return self.price

    def place_market(self, symbol, qty, side, product):
        self._seq += 1
        self.orders.append((symbol, qty, side))
        return Fill(order_id=f"FAKE-{self._seq}", side=side, qty=qty,
                    price=self.price, status="COMPLETE")


class Tick:
    """Mutable holder so live_tick can change across the day."""

    def __init__(self, value):
        self.value = value


@pytest.fixture
def wired(tmp_path, monkeypatch):
    picks_path = tmp_path / "daily_picks.json"
    picks_path.write_text(json.dumps({
        "generated_at": TODAY.replace(hour=8, minute=45).isoformat(),
        "picks": [
            {"symbol": "TATAMOTORS", "atr14_pct": 2.0, "long_room_2pct": 3.0,
             "short_room_2pct": 1.0, "suggested_target_pct": 3.0, "suggested_stop_pct": 1.5,
             "prev_close": 100.0},
            {"symbol": "EARNCO", "earnings_today": True, "prev_close": 50.0},
            {"symbol": "GAPPY", "prev_close": 200.0},  # will gap at open
        ],
    }))
    cfg = make_config(
        picks_path=str(picks_path),
        state_path=str(tmp_path / "state.json"),
        journal_dir=str(tmp_path / "journal"),
        max_picks=5,
    )

    quotes = {
        "TATAMOTORS": OpenQuote(open=100.0, prev_close=100.0),   # flat → survives
        "EARNCO": OpenQuote(open=50.0, prev_close=50.0),
        "GAPPY": OpenQuote(open=210.0, prev_close=200.0),        # +5% gap → discarded
    }
    monkeypatch.setattr(marketdata, "opening_quote", lambda s: quotes.get(s))
    monkeypatch.setattr(marketdata, "india_vix", lambda: (11.0, 11.0))
    monkeypatch.setattr(
        marketdata, "opening_range",
        lambda s, m: OpeningRange(high=105.0, low=95.0, volume=15000.0, avg_volume=1000.0),
    )
    tick = Tick((106.0, 1500.0))  # breakout above the 105 range high on volume
    monkeypatch.setattr(marketdata, "live_tick", lambda s: tick.value)

    broker = FakeBroker(price=106.0)
    engine = KittyBotEngine(cfg, broker=broker)
    return cfg, engine, broker, tick


def _events(cfg):
    return [e["event"] for e in journal.read_day(cfg.journal_dir, TODAY.date())]


def test_full_day_long_target_exit(wired):
    cfg, engine, broker, tick = wired

    # 09:15 — prepare: filters run, TATAMOTORS survives, other two discarded.
    engine.step(TODAY.replace(hour=9, minute=15))
    events = _events(cfg)
    assert "start" in events
    assert events.count("discard") == 2
    assert "observe" in events

    # 09:31 — breakout fires, single trade selected and entered (paper/fake).
    engine.step(TODAY.replace(hour=9, minute=31))
    pos = state.get_position(cfg.state_path)
    assert pos is not None
    assert pos["symbol"] == "TATAMOTORS"
    assert pos["direction"] == "LONG"
    assert broker.orders[0][2] == BUY
    # Sized to ≤1% of ₹100k: stop ≈104.41 → per-share risk ≈1.59 → 628 shares.
    assert pos["qty"] == 628
    assert "entry" in _events(cfg)

    # 12:00 — price runs to target; breakeven ratchet then TARGET exit.
    tick.value = (110.0, 1500.0)
    engine.step(TODAY.replace(hour=12, minute=0))
    assert state.get_position(cfg.state_path) is None
    events = _events(cfg)
    assert "stop_moved" in events
    assert "exit" in events
    # A winning trade must not increment the loss streak.
    assert state.load(cfg.state_path)["loss_streak"] == 0
    # Two broker orders total: the entry BUY and the exit SELL.
    assert len(broker.orders) == 2


def test_no_reentry_after_position_closed(wired):
    cfg, engine, broker, tick = wired
    engine.step(TODAY.replace(hour=9, minute=15))
    engine.step(TODAY.replace(hour=9, minute=31))     # enter
    tick.value = (110.0, 1500.0)
    engine.step(TODAY.replace(hour=12, minute=0))     # target exit
    orders_after_exit = len(broker.orders)
    # Later in the same day another breakout must NOT open a new position.
    engine.step(TODAY.replace(hour=13, minute=0))
    assert state.get_position(cfg.state_path) is None
    assert len(broker.orders) == orders_after_exit


def test_hard_time_exit_closes_open_position(wired):
    cfg, engine, broker, tick = wired
    engine.step(TODAY.replace(hour=9, minute=15))
    engine.step(TODAY.replace(hour=9, minute=31))     # enter
    assert state.get_position(cfg.state_path) is not None
    # Sitting between stop and target, but 15:10 forces the exit.
    tick.value = (107.0, 1200.0)
    engine.step(TODAY.replace(hour=15, minute=10))
    assert state.get_position(cfg.state_path) is None
    exit_events = [e for e in journal.read_day(cfg.journal_dir, TODAY.date())
                   if e["event"] == "exit"]
    assert exit_events and exit_events[-1]["reason"] == "TIME"


def test_vix_spike_skips_the_day(wired, monkeypatch):
    cfg, engine, broker, tick = wired
    monkeypatch.setattr(marketdata, "india_vix", lambda: (14.0, 11.0))  # +27%
    engine.step(TODAY.replace(hour=9, minute=15))
    engine.step(TODAY.replace(hour=9, minute=31))
    assert state.get_position(cfg.state_path) is None
    assert "skip_day" in _events(cfg)
    assert broker.orders == []
