"""Vectorized indicator series for backtesting.

The live ``src/indicators.py`` returns scalar latest-values for the realtime
engine. The backtester needs the full per-bar series, so these are computed
here as pandas Series aligned to the input frame's index. Pure functions.
"""
from __future__ import annotations

import pandas as pd


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rolling_vwap(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Rolling-anchored daily VWAP proxy (true intraday VWAP needs tick data)."""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    pv = (typical * df["Volume"]).rolling(window).sum()
    vol = df["Volume"].rolling(window).sum()
    return pv / vol.replace(0.0, pd.NA)


def rvol(volume: pd.Series, window: int = 20) -> pd.Series:
    """Relative volume vs trailing average (excludes current bar)."""
    avg = volume.shift(1).rolling(window).mean()
    return volume / avg.replace(0.0, pd.NA)


def atr_expansion(atr_series: pd.Series, window: int = 20) -> pd.Series:
    """Current ATR relative to its trailing average — >1 means expanding."""
    avg = atr_series.shift(1).rolling(window).mean()
    return atr_series / avg.replace(0.0, pd.NA)


def rolling_return(close: pd.Series, window: int) -> pd.Series:
    return close.pct_change(window)
