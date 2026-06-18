"""
Vectorised indicators for backtesting and scanning.

These return full ``pandas.Series`` aligned to the input frame, unlike
``src.indicators`` (which returns only the latest scalar for the live dashboard).
The backtester needs the whole history, so this module is the source of truth for
the swing system. RSI here uses Wilder smoothing (EWM, alpha=1/n) — the standard
for swing setups and what the session baselines were computed with.

All functions are pure: they take a Series/DataFrame and return a new Series,
never mutating the input.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_OHLC = ("high", "low", "close")


def ema(close: pd.Series, span: int) -> pd.Series:
    """Exponential moving average (adjust=False, matching src.indicators)."""
    return close.ewm(span=span, adjust=False).mean()


def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return close.rolling(window).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI (0–100) as a Series; NaN until enough history.

    With zero average loss (an unbroken up-run) rs → ∞ and RSI → 100, the standard
    convention; division is done under errstate so the inf doesn't warn.
    """
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss
    return 100 - (100 / (1 + rs))


def true_range(df: pd.DataFrame) -> pd.Series:
    """True range per bar from OHLC."""
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average true range (simple rolling mean of true range)."""
    return true_range(df).rolling(period).mean()


def avg_volume(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Rolling average volume over `lookback` bars."""
    return volume.rolling(lookback).mean()


def rvol(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Relative volume: current volume / rolling average volume."""
    return volume / avg_volume(volume, lookback).replace(0, np.nan)


def rolling_vwap(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Daily-bar VWAP proxy: rolling Σ(typical·volume) / Σ(volume).

    True intraday VWAP is unavailable on daily bars; this rolling-anchored proxy
    stands in for the spec's "price > VWAP" trend filter on the swing timeframe.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = (typical * df["volume"]).rolling(window).sum()
    vol = df["volume"].rolling(window).sum().replace(0, np.nan)
    return pv / vol


def atr_expansion(df: pd.DataFrame, fast: int = 5, slow: int = 14) -> pd.Series:
    """ATR-expansion ratio: short-window ATR / long-window ATR.

    >1 means volatility is expanding (a breakout/momentum tell); <1 contracting.
    """
    return atr(df, fast) / atr(df, slow).replace(0, np.nan)
