"""Tests for src/probability.touch_probabilities — empirical touch odds for
arbitrary price levels, computed from daily OHLC."""
from __future__ import annotations

import pandas as pd

from src.emcure.probability import touch_probabilities


def _df(n: int, close: float, up_pct: float, dn_pct: float) -> pd.DataFrame:
    """n days where every day's high is +up_pct and low is −dn_pct vs a flat
    close — so excursions are deterministic and the math is predictable."""
    return pd.DataFrame({
        "close": [close] * n,
        "high":  [round(close * (1 + up_pct), 2)] * n,
        "low":   [round(close * (1 - dn_pct), 2)] * n,
    })


def test_reachable_level_within_daily_range_is_certain():
    df = _df(60, 1000.0, up_pct=0.05, dn_pct=0.01)   # +5% high, −1% low daily
    p = touch_probabilities(df, 1000.0, [1030.0], 980.0, horizon_days=1)
    assert p[1030.0] == 100        # needs +3%, daily high is +5% → always touched
    assert p["stop"] == 0          # needs −2%, daily low only −1% → never


def test_level_beyond_daily_range_is_unreachable():
    df = _df(60, 1000.0, up_pct=0.05, dn_pct=0.01)
    p = touch_probabilities(df, 1000.0, [1060.0], 970.0, horizon_days=1)
    assert p[1060.0] == 0          # needs +6%, only +5% available
    assert p["stop"] == 0          # needs −3%, only −1% available


def test_level_below_current_price_is_near_certain():
    df = _df(60, 1000.0, up_pct=0.05, dn_pct=0.01)
    p = touch_probabilities(df, 1000.0, [995.0], 980.0, horizon_days=1)
    assert p[995.0] == 99          # already above the level


def test_insufficient_data_returns_empty():
    df = _df(10, 1000.0, up_pct=0.05, dn_pct=0.01)   # < horizon + 20
    assert touch_probabilities(df, 1000.0, [1030.0], 980.0, horizon_days=5) == {}


def test_targets_are_monotonic_non_increasing():
    # Higher targets can never be more likely than lower ones.
    df = _df(120, 1500.0, up_pct=0.03, dn_pct=0.02)
    p = touch_probabilities(df, 1500.0, [1515.0, 1530.0, 1545.0], 1400.0, horizon_days=5)
    assert p[1515.0] >= p[1530.0] >= p[1545.0]


def test_daily_reach_probs_dynamic_from_current_price():
    from src.emcure.probability import daily_reach_probs
    df = _df(60, 1000.0, up_pct=0.03, dn_pct=0.02)   # +3% high, −2% low daily
    p = daily_reach_probs(df, 1000.0, [1020.0, 1040.0], down_level=980.0)
    assert p[1020.0] == 100      # +2% within the +3% daily up-move
    assert p[1040.0] == 0        # +4% beyond it
    assert p["stop"] == 100      # −2% within the −2% daily down-move


def test_daily_reach_probs_rises_as_price_approaches_target():
    from src.emcure.probability import daily_reach_probs
    df = _df(60, 1000.0, up_pct=0.03, dn_pct=0.02)
    far  = daily_reach_probs(df, 1000.0, [1040.0])    # needs +4% → unreachable
    near = daily_reach_probs(df, 1025.0, [1040.0])    # needs +1.5% → reachable
    assert near[1040.0] > far[1040.0]
