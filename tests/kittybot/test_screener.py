"""Screener metrics + ranking on synthetic daily OHLCV."""
from __future__ import annotations

import pandas as pd

from src.kittybot import screener

from .conftest import make_config


def _daily(n=80, open_=100.0, up_pct=0.0, down_pct=0.0, close=100.0, volume=5_000_000):
    """n identical daily bars: each opens at open_, reaches +up_pct / -down_pct."""
    high = open_ * (1 + up_pct / 100.0)
    low = open_ * (1 - down_pct / 100.0)
    rows = [{"date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
             "open": open_, "high": high, "low": low, "close": close, "volume": volume}
            for i in range(n)]
    return pd.DataFrame(rows)


# ── hit-rate / room ──────────────────────────────────────────────────────────
def test_long_hit_rate_full_when_every_day_reaches_target():
    df = _daily(up_pct=3.0, down_pct=0.0)  # +3% high every day → 2% always available
    assert screener.directional_hit_rate(df, "long", lookback=60) == 100.0


def test_long_hit_rate_zero_when_never_reaches():
    df = _daily(up_pct=1.0)  # only +1% high → never hits 2%
    assert screener.directional_hit_rate(df, "long", lookback=60) == 0.0


def test_short_room_tracks_downside():
    df = _daily(up_pct=0.5, down_pct=3.0)
    assert screener.directional_hit_rate(df, "short", lookback=60) == 100.0
    assert screener.directional_hit_rate(df, "long", lookback=60) == 0.0


def test_either_hit_rate_combines_sides():
    df = _daily(up_pct=0.0, down_pct=2.5)  # only downside reaches 2%
    assert screener.either_hit_rate(df, lookback=60) == 100.0


# ── range / atr / liquidity ──────────────────────────────────────────────────
def test_avg_range_pct():
    df = _daily(up_pct=2.0, down_pct=2.0)  # 4% range vs open
    assert screener.avg_range_pct(df, 60) == 4.0


def test_adtv_cr_in_crore():
    # close 100 × volume 5,000,000 = ₹50 Cr traded value.
    df = _daily(close=100.0, volume=5_000_000)
    assert screener.adtv_cr(df) == 50.0


# ── target / score ───────────────────────────────────────────────────────────
def test_suggested_target_clamped_2_5():
    assert screener.suggested_target_pct(avg_range=10.0) == 5.0   # 60% of 10 = 6 → 5
    assert screener.suggested_target_pct(avg_range=1.0) == 2.0    # 60% of 1 = 0.6 → 2
    assert screener.suggested_target_pct(avg_range=5.0) == 3.0    # 60% of 5 = 3.0


def test_score_rewards_hit_rate_and_range():
    strong = screener.score(hit_rate_2pct=90.0, avg_range=5.0)
    weak = screener.score(hit_rate_2pct=20.0, avg_range=1.0)
    assert strong > weak
    assert 0 <= weak <= 100 and 0 <= strong <= 100


# ── screen_symbol gates ──────────────────────────────────────────────────────
def test_screen_symbol_rejects_thin_data():
    cfg = make_config()
    assert screener.screen_symbol("X", _daily(n=10), cfg) is None


def test_screen_symbol_rejects_below_liquidity_floor():
    cfg = make_config(screen_min_adtv_cr=100.0)
    thin = _daily(volume=1_000_000)  # ₹10 Cr < ₹100 Cr floor
    assert screener.screen_symbol("X", thin, cfg) is None


def test_screen_symbol_builds_metrics_when_qualified():
    cfg = make_config(screen_min_adtv_cr=10.0)
    df = _daily(up_pct=3.0, down_pct=1.0, volume=5_000_000)
    m = screener.screen_symbol("tatamotors", df, cfg)
    assert m is not None
    assert m.symbol == "TATAMOTORS"
    assert m.long_room_2pct == 100.0
    assert m.short_room_2pct == 0.0
    assert 2.0 <= m.suggested_target_pct <= 5.0
    assert m.suggested_stop_pct == round(m.suggested_target_pct / cfg.reward_risk_ratio, 2)


def test_screen_symbol_none_input():
    assert screener.screen_symbol("X", None, make_config()) is None


# ── ranking / payload ────────────────────────────────────────────────────────
def test_rank_takes_top_n_by_score_then_symbol():
    def m(sym, sc):
        return screener.ScreenMetrics(sym, sc, 2.0, 4.0, sc, 50.0, 50.0, 3.0, 1.5, 100.0, 200.0)
    metrics = [m("A", 50.0), m("B", 90.0), m("C", 90.0), m("D", 10.0)]
    ranked = screener.rank(metrics, max_picks=2)
    assert [x.symbol for x in ranked] == ["B", "C"]  # top score, tie broken by symbol


def test_build_payload_shape():
    from datetime import datetime
    m = screener.ScreenMetrics("A", 80.0, 2.0, 4.0, 80.0, 60.0, 40.0, 3.0, 1.5, 100.0, 200.0)
    payload = screener.build_payload([m], universe_size=32, generated_at=datetime(2026, 7, 6, 8, 45))
    assert payload["universe_size"] == 32
    assert payload["generated_at"].startswith("2026-07-06T08:45")
    pick = payload["picks"][0]
    assert pick["symbol"] == "A"
    assert pick["earnings_today"] is False
    assert "adtv_cr" not in pick  # internal field stripped
