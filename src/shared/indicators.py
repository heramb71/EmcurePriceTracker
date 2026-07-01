from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(float(val), 2) if not np.isnan(val) else 50.0


def compute_macd(close: pd.Series) -> tuple[float, float, float]:
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    return (
        round(float(macd_line.iloc[-1]), 2),
        round(float(signal_line.iloc[-1]), 2),
        round(float(histogram.iloc[-1]), 2),
    )


def compute_bollinger(close: pd.Series, period: int = 20) -> tuple[float, float, float]:
    ma = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = ma + 2 * std
    lower = ma - 2 * std
    return (
        round(float(upper.iloc[-1]), 2),
        round(float(ma.iloc[-1]), 2),
        round(float(lower.iloc[-1]), 2),
    )


def compute_ema(close: pd.Series, span: int) -> float:
    return round(float(close.ewm(span=span, adjust=False).mean().iloc[-1]), 2)


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    # Drop rows missing OHLC (e.g. yfinance's pre-market placeholder for today),
    # otherwise the True Range over a NaN row makes the latest ATR NaN → 0.
    df = df.dropna(subset=["high", "low", "close"])
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(period).mean()
    val = atr.iloc[-1]
    return round(float(val), 2) if not np.isnan(val) else 0.0


def compute_vwap(df_intraday: pd.DataFrame) -> float:
    today = date.today()
    mask = pd.to_datetime(df_intraday["date"]).dt.date == today
    today_df = df_intraday[mask]
    # Fall back to the last available session if today has no bars yet
    if today_df.empty:
        last_date = pd.to_datetime(df_intraday["date"]).dt.date.max()
        today_df = df_intraday[pd.to_datetime(df_intraday["date"]).dt.date == last_date]
    if today_df.empty:
        return 0.0
    typical = (today_df["high"] + today_df["low"] + today_df["close"]) / 3
    vwap = (typical * today_df["volume"]).cumsum() / today_df["volume"].cumsum()
    val = vwap.iloc[-1]
    return round(float(val), 2) if not np.isnan(val) else 0.0


def compute_avg_volume(df: pd.DataFrame, days: int = 20) -> int:
    return int(df["volume"].tail(days).mean())
