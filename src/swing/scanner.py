"""
0–100 setup-quality score for ranking and gating.

Combines the brief's §1 factors into one cross-sectional score. Two uses:
  - RANK: among symbols signalling on the same day, take the highest score.
  - GATE: only enter when score > threshold (e.g. 80), so the bot trades far fewer,
    higher-quality setups — the one lever that can plausibly beat the cost drag at
    ₹15k. The P2 search tests whether that lever actually flips expectancy positive.

Operates on a *prepared* frame (see `signals.prepare`) so all indicator columns
already exist. Sector strength is approximated by the stock's own 20-day momentum
(a clean per-stock relative-strength feed is an Open Item in the plan).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Factor weights (sum = 100).
_W = {
    "trend": 20,      # 20EMA>50EMA and price>50EMA
    "vwap": 10,       # close > rolling-VWAP proxy
    "rsi": 15,        # momentum band 50–70
    "rvol": 15,       # relative volume, capped at 2×
    "atr_exp": 10,    # volatility expanding
    "breakout": 10,   # close > previous-day high
    "mom20": 20,      # 20-day price momentum (rel-strength proxy)
}


def _clip01(s: pd.Series) -> pd.Series:
    return s.clip(0.0, 1.0)


def compute_score(df: pd.DataFrame) -> pd.Series:
    """Return a 0–100 quality score per bar for a prepared frame."""
    trend = ((df["ema20"] > df["ema50"]) & (df["close"] > df["ema50"])).astype(float)
    vwap = (df["close"] > df["vwap"]).astype(float)

    # RSI: full marks in 50–70, tapering outside; 0 below 40 / above 80.
    rsi = df["rsi"]
    rsi_score = _clip01(1 - (rsi - 60).abs() / 20).fillna(0.0)

    rvol_score = _clip01((df["rvol"] - 1.0) / 1.0).fillna(0.0)  # 1×→0, 2×→1

    from src.swing.indicators import atr_expansion
    atr_exp = _clip01(atr_expansion(df) - 1.0).fillna(0.0)       # >1 expanding

    breakout = (df["close"] > df["prev_high"]).astype(float)

    mom = df["close"] / df["close"].shift(20) - 1.0
    mom_score = _clip01(mom / 0.10).fillna(0.0)                  # +10% over 20d → full

    score = (
        _W["trend"] * trend
        + _W["vwap"] * vwap
        + _W["rsi"] * rsi_score
        + _W["rvol"] * rvol_score
        + _W["atr_exp"] * atr_exp
        + _W["breakout"] * breakout
        + _W["mom20"] * mom_score
    )
    return score.fillna(0.0)
