from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from emcure_tracker import config

logger = logging.getLogger(__name__)


# ── Data contracts ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IndicatorResult:
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    bb_upper: float
    bb_mid: float
    bb_lower: float
    ema_short: float
    ema_long: float
    avg_volume: int
    avg_range: float
    support_levels: tuple[float, ...]
    resistance_levels: tuple[float, ...]
    sector_relative_strength: float | None  # Emcure daily return / Nifty Pharma daily return


@dataclass(frozen=True)
class VolumeSignal:
    ratio: float
    label: str
    color: str


# ── Indicator computation ──────────────────────────────────────────────────

def compute_rsi(series: pd.Series, period: int = config.RSI_PERIOD) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(float(val), 2) if not np.isnan(val) else 50.0


def compute_macd(
    series: pd.Series,
    fast: int = config.MACD_FAST,
    slow: int = config.MACD_SLOW,
    signal: int = config.MACD_SIGNAL,
) -> tuple[float, float, float]:
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return (
        round(float(macd_line.iloc[-1]), 2),
        round(float(signal_line.iloc[-1]), 2),
        round(float(histogram.iloc[-1]), 2),
    )


def compute_bollinger(
    series: pd.Series, period: int = config.BB_PERIOD
) -> tuple[float, float, float]:
    ma = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = ma + 2 * std
    lower = ma - 2 * std
    return (
        round(float(upper.iloc[-1]), 2),
        round(float(ma.iloc[-1]), 2),
        round(float(lower.iloc[-1]), 2),
    )


def compute_ema(series: pd.Series, span: int) -> float:
    return round(float(series.ewm(span=span, adjust=False).mean().iloc[-1]), 2)


def compute_avg_volume(df: pd.DataFrame, days: int = 20) -> int:
    return int(df["volume"].tail(days).mean())


def compute_avg_range(df: pd.DataFrame, days: int = 10) -> float:
    daily_range = df["high"] - df["low"]
    return round(float(daily_range.tail(days).mean()), 2)


def compute_support_resistance(
    df: pd.DataFrame,
    lookback: int = config.SR_LOOKBACK,
    min_touches: int = config.SR_MIN_TOUCHES,
    tolerance_pct: float = 0.5,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """
    Detect support and resistance levels from swing highs/lows.
    A pivot high is a bar whose high exceeds both neighbours.
    A pivot low is a bar whose low is below both neighbours.
    Levels with `min_touches` occurrences within `tolerance_pct` band are kept.
    """
    window = df.tail(lookback).copy().reset_index(drop=True)
    if len(window) < 3:
        return (), ()

    pivot_highs: list[float] = []
    pivot_lows: list[float] = []

    for i in range(1, len(window) - 1):
        if window["high"].iloc[i] > window["high"].iloc[i - 1] and window["high"].iloc[i] > window["high"].iloc[i + 1]:
            pivot_highs.append(window["high"].iloc[i])
        if window["low"].iloc[i] < window["low"].iloc[i - 1] and window["low"].iloc[i] < window["low"].iloc[i + 1]:
            pivot_lows.append(window["low"].iloc[i])

    current_price = float(window["close"].iloc[-1])
    tol = current_price * tolerance_pct / 100

    def _cluster(levels: list[float]) -> list[float]:
        if not levels:
            return []
        levels_sorted = sorted(levels)
        clusters: list[list[float]] = [[levels_sorted[0]]]
        for lvl in levels_sorted[1:]:
            if lvl - clusters[-1][-1] <= tol:
                clusters[-1].append(lvl)
            else:
                clusters.append([lvl])
        result = []
        for cluster in clusters:
            if len(cluster) >= min_touches:
                result.append(round(sum(cluster) / len(cluster), 2))
        return result

    supports = tuple(sorted(_cluster(pivot_lows)))
    resistances = tuple(sorted(_cluster(pivot_highs)))
    return supports, resistances


def compute_volume_signal(current_vol: int, avg_vol: int) -> VolumeSignal:
    ratio = current_vol / avg_vol if avg_vol else 1.0
    if ratio >= 2.0:
        return VolumeSignal(ratio=round(ratio, 2), label=f"Very High ({ratio:.1f}x avg)", color="bold green")
    elif ratio >= 1.5:
        return VolumeSignal(ratio=round(ratio, 2), label=f"High ({ratio:.1f}x avg)", color="green")
    elif ratio >= 0.8:
        return VolumeSignal(ratio=round(ratio, 2), label=f"Normal ({ratio:.1f}x avg)", color="yellow")
    else:
        return VolumeSignal(ratio=round(ratio, 2), label=f"Low ({ratio:.1f}x avg)", color="red")


def rsi_signal(rsi: float) -> tuple[str, str]:
    if rsi >= 70:
        return "Overbought", "red"
    elif rsi <= 30:
        return "Oversold", "green"
    elif rsi >= 60:
        return "Bullish", "yellow"
    elif rsi <= 40:
        return "Bearish", "cyan"
    return "Neutral", "white"


# ── Main computation entry point ───────────────────────────────────────────

def compute_all(
    df: pd.DataFrame,
    sector_df: pd.DataFrame | None = None,
) -> IndicatorResult | None:
    try:
        close = df["close"]
        supports, resistances = compute_support_resistance(df)

        # Sector relative strength: compare last-day return
        rs = None
        if sector_df is not None and not sector_df.empty and len(sector_df) >= 2:
            emcure_ret = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]
            sector_close = sector_df["close"]
            sector_ret = (sector_close.iloc[-1] - sector_close.iloc[-2]) / sector_close.iloc[-2]
            rs = round(float(emcure_ret / sector_ret), 3) if sector_ret != 0 else None

        macd_line, signal_line, hist = compute_macd(close)
        bb_upper, bb_mid, bb_lower = compute_bollinger(close)

        return IndicatorResult(
            rsi=compute_rsi(close),
            macd=macd_line,
            macd_signal=signal_line,
            macd_hist=hist,
            bb_upper=bb_upper,
            bb_mid=bb_mid,
            bb_lower=bb_lower,
            ema_short=compute_ema(close, config.EMA_SHORT),
            ema_long=compute_ema(close, config.EMA_LONG),
            avg_volume=compute_avg_volume(df),
            avg_range=compute_avg_range(df),
            support_levels=supports,
            resistance_levels=resistances,
            sector_relative_strength=rs,
        )
    except Exception:
        logger.exception("compute_all failed")
        return None
