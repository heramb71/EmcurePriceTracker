"""0-100 stock scoring engine + per-date ranking.

Scoring weights follow the brief:
  RVOL 30 · VWAP strength 25 · ATR expansion 15 · RSI 10 · breakout 10 · RS 10.

Operates on a precomputed per-symbol feature frame (see backtest.build_features)
so it is cheap to call per bar across a 3-year window.
"""
from __future__ import annotations

import pandas as pd

W_RVOL = 30.0
W_VWAP = 25.0
W_ATR = 15.0
W_RSI = 10.0
W_BREAKOUT = 10.0
W_RS = 10.0

SCORE_GATE = 80.0


def _clip01(x: pd.Series) -> pd.Series:
    return x.clip(lower=0.0, upper=1.0)


def score_frame(feat: pd.DataFrame) -> pd.Series:
    """Per-bar 0-100 score for one symbol's feature frame."""
    rvol_s = _clip01((feat["rvol"] - 1.0) / (2.0 - 1.0)) * W_RVOL
    vwap_s = _clip01((feat["Close"] / feat["vwap"] - 1.0) / 0.03) * W_VWAP
    atr_s = _clip01((feat["atr_exp"] - 1.0) / 0.5) * W_ATR
    rsi_s = _clip01((feat["rsi"] - 50.0) / (70.0 - 50.0)) * W_RSI
    brk_s = (feat["Close"] > feat["prev_high"]).astype(float) * W_BREAKOUT
    rs_s = _clip01(feat["rs20"] / 0.05) * W_RS

    total = rvol_s + vwap_s + atr_s + rsi_s + brk_s + rs_s
    return total.fillna(0.0)


def rank_on_date(scores_by_symbol: dict[str, float]) -> list[tuple[str, float]]:
    """Return [(symbol, score), ...] sorted high→low for a single date."""
    return sorted(scores_by_symbol.items(), key=lambda kv: kv[1], reverse=True)
