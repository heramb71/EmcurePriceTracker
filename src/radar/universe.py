"""Radar stock universe + liquidity gate.

The universe is the user-final 6-stock set. Liquidity is gated on average daily
traded value (ADTV) computed from the same daily bars the scan uses, so it stays
honest to the live data. yfinance needs the NSE ``.NS`` suffix; the index keeps
its ``^`` prefix.
"""
from __future__ import annotations

import pandas as pd

# User-final universe (high-beta PSU / thematic / pharma).
SYMBOLS: tuple[str, ...] = (
    "EMCURE",
    "ICICIBANK",
    "IREDA",
    "IRFC",
    "HUDCO",
    "SUZLON",
)

NIFTY = "^NSEI"

# Liquidity gate: average daily traded value over the trailing window.
MIN_AVG_TRADED_VALUE_CR = 100.0  # ₹ crore
LIQUIDITY_LOOKBACK = 20  # trading days


def to_yf(symbol: str) -> str:
    """Map a bare NSE symbol to its yfinance ticker."""
    return symbol if symbol.startswith("^") else f"{symbol}.NS"


def adtv_cr(df_daily: pd.DataFrame, lookback: int = LIQUIDITY_LOOKBACK) -> float:
    """Average daily traded value (₹ crore) over the trailing ``lookback`` bars.

    Operates on the live lowercase-column daily frame from ``src.data``.
    """
    if df_daily is None or df_daily.empty:
        return 0.0
    window = min(lookback, len(df_daily))
    tail = df_daily.tail(window)
    traded = (tail["close"] * tail["volume"]).mean()
    if pd.isna(traded):
        return 0.0
    return float(traded) / 1e7  # rupees → crore


def passes_liquidity(
    df_daily: pd.DataFrame, min_cr: float = MIN_AVG_TRADED_VALUE_CR
) -> bool:
    """True when ADTV clears the minimum traded-value floor."""
    return adtv_cr(df_daily) >= min_cr
