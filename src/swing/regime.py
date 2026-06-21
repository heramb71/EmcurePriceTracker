"""Market regime detection from the NIFTY index.

Classifies each bar as TRENDING_BULL / TRENDING_BEAR / SIDEWAYS using the
50-DMA slope and price location. Long entries are only allowed in TRENDING_BULL.
"""
from __future__ import annotations

import pandas as pd

TRENDING_BULL = "TRENDING_BULL"
TRENDING_BEAR = "TRENDING_BEAR"
SIDEWAYS = "SIDEWAYS"

# Slope threshold over the lookback to qualify as trending (fraction of price).
_SLOPE_LOOKBACK = 10
_SLOPE_MIN = 0.01  # 50-DMA must move >1% over 10 bars to count as trending


def regime_series(nifty: pd.DataFrame) -> pd.Series:
    """Per-bar regime label for the NIFTY frame."""
    close = nifty["Close"]
    dma50 = close.rolling(50).mean()
    slope = dma50.pct_change(_SLOPE_LOOKBACK)

    above = close > dma50
    rising = slope > _SLOPE_MIN
    falling = slope < -_SLOPE_MIN

    out = pd.Series(SIDEWAYS, index=close.index)
    out[above & rising] = TRENDING_BULL
    out[(~above) & falling] = TRENDING_BEAR
    return out
