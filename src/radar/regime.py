"""Market regime detection for the radar.

Ports the swing lab's NIFTY 50-DMA slope/location logic to a last-bar scalar and
enriches it with ADX(14) (trend strength) and a universe-internal breadth proxy
(% of the 6 names above their 50-DMA), since NSE advance/decline isn't free on
yfinance. The combined label is one of TRENDING_BULL / TRENDING_BEAR / SIDEWAYS
and is attached to every alert.
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
import pandas as pd

TRENDING_BULL = "TRENDING_BULL"
TRENDING_BEAR = "TRENDING_BEAR"
SIDEWAYS = "SIDEWAYS"

_SLOPE_LOOKBACK = 10
_SLOPE_MIN = 0.01      # 50-DMA must move >1% over 10 bars to count as trending
_ADX_TREND_MIN = 20.0  # ADX below this ⇒ no real trend (forces SIDEWAYS)
_BREADTH_BULL = 0.5    # >half the universe above 50-DMA confirms bull
_BREADTH_BEAR = 0.5    # <half confirms bear


def compute_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Wilder's ADX on a lowercase-column OHLC frame. 0.0 if undefined."""
    if df is None or len(df) < period * 2:
        return 0.0
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean() / atr.replace(0.0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean() / atr.replace(0.0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    return round(float(adx), 2) if not np.isnan(adx) else 0.0


def breadth(above_50dma_flags: Sequence[bool]) -> float:
    """Fraction of the universe trading above its 50-DMA (0.0–1.0)."""
    flags = list(above_50dma_flags)
    return sum(1 for f in flags if f) / len(flags) if flags else 0.0


def current_regime(
    nifty_daily: pd.DataFrame,
    breadth_pct: Optional[float] = None,
) -> str:
    """Combine NIFTY slope/location, ADX trend strength, and breadth.

    A direction set by slope+location is only honoured when ADX confirms a real
    trend *and* breadth agrees; otherwise the market is SIDEWAYS.
    """
    if nifty_daily is None or len(nifty_daily) < 60:
        return SIDEWAYS

    close = nifty_daily["close"]
    dma50 = close.rolling(50).mean()
    last_close = float(close.iloc[-1])
    last_dma = float(dma50.iloc[-1])
    slope = (last_dma - float(dma50.iloc[-1 - _SLOPE_LOOKBACK])) / float(
        dma50.iloc[-1 - _SLOPE_LOOKBACK]
    )

    adx = compute_adx(nifty_daily)
    above = last_close > last_dma
    rising = slope > _SLOPE_MIN
    falling = slope < -_SLOPE_MIN

    if adx < _ADX_TREND_MIN:
        return SIDEWAYS

    if above and rising:
        if breadth_pct is None or breadth_pct >= _BREADTH_BULL:
            return TRENDING_BULL
    if (not above) and falling:
        if breadth_pct is None or breadth_pct <= _BREADTH_BEAR:
            return TRENDING_BEAR
    return SIDEWAYS
