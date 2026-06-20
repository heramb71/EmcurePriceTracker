"""Tests for regime detection, ADX, breadth, and the liquidity gate."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.radar import regime, universe


def _trend_frame(n: int, drift: float, start: float = 20000.0) -> pd.DataFrame:
    """Synthetic daily OHLC with a steady drift (fraction per bar)."""
    closes = start * (1 + drift) ** np.arange(n)
    high = closes * 1.005
    low = closes * 0.995
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="D"),
            "open": closes,
            "high": high,
            "low": low,
            "close": closes,
            "volume": np.full(n, 1_000_000),
        }
    )


def test_breadth_fraction():
    assert regime.breadth([True, True, False, False]) == 0.5
    assert regime.breadth([]) == 0.0


def test_adx_rises_in_strong_trend():
    flat = _trend_frame(120, 0.0)
    trending = _trend_frame(120, 0.01)
    assert regime.compute_adx(trending) > regime.compute_adx(flat)


def test_regime_bull_when_rising_with_breadth():
    nifty = _trend_frame(120, 0.01)  # steady uptrend → high ADX, above 50DMA
    assert regime.current_regime(nifty, breadth_pct=0.8) == regime.TRENDING_BULL


def test_regime_sideways_when_breadth_disagrees():
    nifty = _trend_frame(120, 0.01)
    # Strong index trend but weak breadth blocks the bull label.
    assert regime.current_regime(nifty, breadth_pct=0.2) == regime.SIDEWAYS


def test_regime_sideways_on_flat_market():
    nifty = _trend_frame(120, 0.0)
    assert regime.current_regime(nifty, breadth_pct=0.5) == regime.SIDEWAYS


def test_regime_insufficient_data_is_sideways():
    assert regime.current_regime(_trend_frame(30, 0.01)) == regime.SIDEWAYS


def test_passes_liquidity_gate():
    # 1Cr shares × ₹1500 = ₹1500 Cr ADTV → passes ₹100 Cr floor.
    rich = pd.DataFrame({"close": [1500.0] * 20, "volume": [1_000_000] * 20})
    # 100 shares × ₹50 = trivially below floor.
    thin = pd.DataFrame({"close": [50.0] * 20, "volume": [100] * 20})
    assert universe.passes_liquidity(rich) is True
    assert universe.passes_liquidity(thin) is False
