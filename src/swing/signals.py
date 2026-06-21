"""Entry and exit rules for the swing lab — pure and testable.

Two entry variants are compared:
  - breakout : close > prev-day high (chase strength) — the brief's literal rule
  - pullback : fade a dip back to the 20EMA inside an uptrend (best prior result)

Exit rules are shared: 1.5xATR stop, 3xATR target, alt-exits (close<VWAP,
close<20EMA, regime turns bear), and a 10-day time stop.
"""
from __future__ import annotations

import pandas as pd

from .regime import TRENDING_BULL

STOP_ATR_MULT = 1.5
TARGET_ATR_MULT = 3.0
MAX_HOLD_DAYS = 10

# Pullback proximity: close within this fraction above the 20EMA counts as a dip.
_PULLBACK_BAND = 0.01


def breakout_entry(feat: pd.DataFrame) -> pd.Series:
    """Boolean entry series for the breakout variant (brief's literal rule)."""
    return (
        (feat["Close"] > feat["prev_high"])
        & (feat["Close"] > feat["vwap"])
        & (feat["ema20"] > feat["ema50"])
        & (feat["rsi"] > 55.0)
        & (feat["rvol"] > 1.5)
    ).fillna(False)


def pullback_entry(feat: pd.DataFrame) -> pd.Series:
    """Boolean entry series for the pullback (buy-the-dip-in-uptrend) variant."""
    uptrend = feat["ema20"] > feat["ema50"]
    near_ema = feat["Close"] <= feat["ema20"] * (1.0 + _PULLBACK_BAND)
    not_falling_knife = feat["rsi"] > 40.0
    return (uptrend & near_ema & not_falling_knife).fillna(False)


def should_exit(
    bar: pd.Series,
    entry_price: float,
    stop: float,
    target: float,
    days_held: int,
    regime: str,
    use_ma_exits: bool = True,
) -> tuple[bool, str, float]:
    """Decide exit for an open position on this bar.

    Returns (exit?, reason, fill_price). Intrabar stop/target use bar extremes;
    everything else fills at the close.

    ``use_ma_exits`` toggles the trend-following close<20EMA / close<VWAP
    alt-exits. They are coherent for the breakout (trend) variant but
    self-defeating for the pullback (mean-reversion) variant, which deliberately
    enters below the 20EMA — so the backtester disables them for pullback.
    """
    if bar["Low"] <= stop:
        return True, "stop", stop
    if bar["High"] >= target:
        return True, "target", target
    if regime == "TRENDING_BEAR":
        return True, "regime_bear", bar["Close"]
    if use_ma_exits:
        if bar["Close"] < bar["ema20"]:
            return True, "below_ema20", bar["Close"]
        if bar["Close"] < bar["vwap"]:
            return True, "below_vwap", bar["Close"]
    if days_held >= MAX_HOLD_DAYS:
        return True, "time_stop", bar["Close"]
    return False, "", 0.0


def entry_allowed(regime: str) -> bool:
    return regime == TRENDING_BULL
