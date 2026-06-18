"""
Market-regime gate from the NIFTY index.

The bot only takes long entries when the broad market is healthy. On daily bars we
proxy the spec's "NIFTY above VWAP / TRENDING_BULL" with NIFTY closing above its
50-day moving average: bull above, bear below. The regime is both an entry gate
and an exit trigger (a flip to bear closes an open position).

Returned as a ``{date: label}`` map so the backtester and live engine can look up
any session in O(1).
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from src.swing.indicators import sma

TRENDING_BULL = "TRENDING_BULL"
TRENDING_BEAR = "TRENDING_BEAR"


def regime_by_date(df_nifty: pd.DataFrame, dma: int = 50) -> dict[date, str]:
    """Map each NIFTY session date to its regime label (bull/bear vs `dma`-DMA).

    Sessions before the moving average is defined are omitted (treated as not-bull
    by callers, which default missing dates to bear/no-trade).
    """
    if df_nifty is None or df_nifty.empty:
        return {}
    ma = sma(df_nifty["close"], dma)
    out: dict[date, str] = {}
    for d, close, m in zip(df_nifty["date"], df_nifty["close"], ma):
        if pd.isna(m):
            continue
        out[d] = TRENDING_BULL if close > m else TRENDING_BEAR
    return out


def is_bull(label: str | None) -> bool:
    """True only when the regime is an explicit bull; missing/unknown → False."""
    return label == TRENDING_BULL
