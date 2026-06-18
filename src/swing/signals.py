"""
Entry/exit signal definitions for the swing system.

A ``Strategy`` bundles an entry rule (prepared-frame → boolean Series) with the
exit parameters (ATR-based stop/target, max hold, alt MA exit). The backtester
and live scanner share these pure definitions so what is validated is exactly what
trades.

`prepare` adds every indicator column the entry rules need, once, so entry
functions stay cheap and readable. Two entries are built in:
  - BREAKOUT — the brief's spec (close > prev-day high + trend + RSI>55 + RVOL).
    Backtests worst (chases gap-ups); kept as the comparison baseline.
  - PULLBACK — fade a dip to the 20-EMA inside an uptrend. Backtests best of the
    simple variants; the primary candidate for the validation search.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from src.swing import indicators as ind

# Minimum bars before any signal is valid (50-EMA + buffer).
WARMUP = 51


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of `df` with all indicator columns the entry rules need."""
    out = df.copy()
    out["ema20"] = ind.ema(out["close"], 20)
    out["ema50"] = ind.ema(out["close"], 50)
    out["rsi"] = ind.rsi(out["close"], 14)
    out["atr"] = ind.atr(out, 14)
    out["avg_vol"] = ind.avg_volume(out["volume"], 20)
    out["rvol"] = ind.rvol(out["volume"], 20)
    out["prev_high"] = out["high"].shift(1)
    out["vwap"] = ind.rolling_vwap(out, 20)
    return out


def _uptrend(df: pd.DataFrame) -> pd.Series:
    """Shared trend filter: 20-EMA above 50-EMA, price above 50-EMA, ATR defined."""
    return (df["ema20"] > df["ema50"]) & (df["close"] > df["ema50"]) & (df["atr"] > 0)


def entry_breakout(df: pd.DataFrame) -> pd.Series:
    """Spec breakout: prev-day-high breakout in an uptrend, RSI>55, RVOL>1.5×."""
    sig = (
        _uptrend(df)
        & (df["close"] > df["prev_high"])
        & (df["close"] > df["ema20"])
        & (df["close"] > df["vwap"])
        & (df["rsi"] > 55)
        & (df["rvol"] > 1.5)
    )
    return _gate_warmup(sig)


def entry_pullback(df: pd.DataFrame) -> pd.Series:
    """Pullback: dip to/under the 20-EMA inside an uptrend, RSI cooled to 40–58,
    with an up-day confirming the bounce (avoids catching a falling knife)."""
    sig = (
        _uptrend(df)
        & (df["close"] <= df["ema20"] * 1.01)
        & (df["rsi"].between(40, 58))
        & (df["close"] > df["close"].shift(1))
    )
    return _gate_warmup(sig)


def _gate_warmup(sig: pd.Series) -> pd.Series:
    """Force False for the warm-up window and any NaN comparisons → clean bool."""
    sig = sig.fillna(False).astype(bool)
    sig.iloc[:WARMUP] = False
    return sig


@dataclass(frozen=True)
class Strategy:
    """An entry rule plus its exit parameters. `alt_exit_ma` (e.g. 'ema20'/'ema50')
    closes the trade when a daily close prints below that moving average; None
    disables it. `entry` maps a prepared frame to a boolean entry Series."""
    name: str
    entry: Callable[[pd.DataFrame], pd.Series]
    atr_stop: float = 1.5
    atr_target: float = 3.0
    max_hold: int = 10
    alt_exit_ma: Optional[str] = "ema20"


# Built-in strategies. More variants (A–E) get added for the P2 search.
BREAKOUT = Strategy("breakout", entry_breakout, alt_exit_ma="ema20")
PULLBACK = Strategy("pullback", entry_pullback, alt_exit_ma="ema50")

REGISTRY: dict[str, Strategy] = {s.name: s for s in (BREAKOUT, PULLBACK)}
