"""
Crypto signal scoring.

Reuses the pure indicator functions from src/indicators.py (RSI, MACD, Bollinger,
EMA, ATR) unchanged.  Replaces pivot-point sub-score with Bollinger Band position
since crypto has no fixed session reference points.

Score weights:
  RSI(14)      0.25  — oversold bias
  MACD hist    0.20  — momentum direction
  BB position  0.20  — mean-reversion zone
  EMA trend    0.20  — structural bias (EMA20/50/200)
  7d momentum  0.10  — price trend over the week
  Volume ratio 0.05  — conviction check

Signal thresholds:
  ≥ 0.72  Strong Buy
  ≥ 0.58  Buy
  ≤ 0.42  Sell
  ≤ 0.28  Strong Sell
  else    Hold
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from src.shared.indicators import (
    compute_atr,
    compute_avg_volume,
    compute_bollinger,
    compute_ema,
    compute_macd,
    compute_rsi,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Sub-score helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rsi_score(rsi: float) -> float:
    if rsi <= 30:
        return 1.0
    if rsi >= 70:
        return 0.0
    if rsi <= 50:
        return round(0.5 + (50 - rsi) / 40, 3)
    return round(0.5 - (rsi - 50) / 40, 3)


def _bb_score(price: float, upper: float, lower: float) -> float:
    """Position within Bollinger Bands: lower band = 1.0 (buy zone), upper = 0.0."""
    band_range = upper - lower
    if band_range <= 0:
        return 0.5
    pos = (price - lower) / band_range  # 0 = at lower, 1 = at upper
    return round(max(0.0, min(1.0, 1.0 - pos)), 3)


def _ema_trend_score(price: float, ema20: float, ema50: float, ema200: float) -> float:
    """
    Score based on price/EMA structure.

    Full bull stack (price > EMA20 > EMA50 > EMA200) = 1.0
    Full bear stack (price < EMA20 < EMA50 < EMA200) = 0.0
    """
    score = 0.5
    if ema20 > 0:
        score += 0.15 if price > ema20 else -0.15
    if ema20 > 0 and ema50 > 0:
        score += 0.20 if ema20 > ema50 else -0.20
    if ema200 > 0:
        score += 0.15 if price > ema200 else -0.15
    return round(max(0.0, min(1.0, score)), 3)


def _momentum_score(change_7d_pct: float) -> float:
    """
    Counter-trend scoring: deep pullbacks score high (mean-reversion opportunity).
    Extended rallies score low (caution zone).
    """
    if change_7d_pct <= -20:
        return 0.90
    if change_7d_pct <= -10:
        return 0.75
    if change_7d_pct <= -5:
        return 0.60
    if change_7d_pct <= 5:
        return 0.50
    if change_7d_pct <= 15:
        return 0.35
    return 0.20  # extended / overbought


# ─────────────────────────────────────────────────────────────────────────────
# Main signal computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_crypto_signal(df_daily: pd.DataFrame, quote: dict) -> dict:
    """
    Compute a directional signal for one crypto asset from daily OHLCV.

    Parameters
    ----------
    df_daily : DataFrame
        Normalised daily data from src.crypto.data.fetch_crypto_daily.
        Must have columns: date, open, high, low, close, volume.
    quote : dict
        Live quote dict from src.crypto.data.fetch_crypto_quote.

    Returns
    -------
    dict with keys: score, signal, trend, rsi, macd_hist, macd_line, macd_signal,
        bb_upper, bb_mid, bb_lower, ema20, ema50, ema200, atr, atr_pct,
        change_7d_pct, vol_ratio, sub_scores
    """
    close = df_daily["close"]
    price = quote.get("price_usd", float(close.iloc[-1]))

    # ── Indicators ────────────────────────────────────────────────────────────
    rsi = compute_rsi(close)
    macd_line, macd_signal_val, macd_hist = compute_macd(close)
    bb_upper, bb_mid, bb_lower = compute_bollinger(close)
    ema20 = compute_ema(close, 20)
    ema50 = compute_ema(close, 50)
    ema200 = compute_ema(close, 200)
    atr = compute_atr(df_daily)
    avg_vol = compute_avg_volume(df_daily)

    # 7-day price change (use actual daily rows, not calendar days)
    week_ago = float(df_daily["close"].iloc[-8]) if len(df_daily) >= 8 else float(close.iloc[0])
    change_7d_pct = round((price - week_ago) / week_ago * 100, 2) if week_ago > 0 else 0.0

    today_vol = int(df_daily["volume"].iloc[-1])
    vol_ratio = round(today_vol / avg_vol, 2) if avg_vol > 0 else 1.0

    # ── Sub-scores ────────────────────────────────────────────────────────────
    rsi_sc = _rsi_score(rsi)
    macd_sc = 1.0 if macd_hist > 0 else 0.0
    bb_sc = _bb_score(price, bb_upper, bb_lower)
    ema_sc = _ema_trend_score(price, ema20, ema50, ema200)
    mom_sc = _momentum_score(change_7d_pct)
    vol_sc = round(min(1.0, vol_ratio / 2), 3)

    weights = {
        "rsi":       0.25,
        "macd":      0.20,
        "bb":        0.20,
        "ema_trend": 0.20,
        "momentum":  0.10,
        "volume":    0.05,
    }

    score = round(
        rsi_sc  * weights["rsi"]
        + macd_sc * weights["macd"]
        + bb_sc   * weights["bb"]
        + ema_sc  * weights["ema_trend"]
        + mom_sc  * weights["momentum"]
        + vol_sc  * weights["volume"],
        4,
    )

    if score >= 0.72:
        signal = "Strong Buy"
    elif score >= 0.58:
        signal = "Buy"
    elif score <= 0.28:
        signal = "Strong Sell"
    elif score <= 0.42:
        signal = "Sell"
    else:
        signal = "Hold"

    # Structural trend label from EMA stack
    if price > ema20 > ema50 > ema200 and ema200 > 0:
        trend = "Strong Uptrend"
    elif price > ema50 > 0:
        trend = "Uptrend"
    elif ema20 > 0 and ema50 > 0 and price < ema20 < ema50:
        trend = "Downtrend"
    else:
        trend = "Ranging"

    return {
        "score":        score,
        "signal":       signal,
        "trend":        trend,
        "rsi":          rsi,
        "macd_hist":    macd_hist,
        "macd_line":    macd_line,
        "macd_signal":  macd_signal_val,
        "bb_upper":     bb_upper,
        "bb_mid":       bb_mid,
        "bb_lower":     bb_lower,
        "ema20":        ema20,
        "ema50":        ema50,
        "ema200":       ema200,
        "atr":          atr,
        "atr_pct":      round(atr / price * 100, 2) if price > 0 else 0.0,
        "change_7d_pct": change_7d_pct,
        "vol_ratio":    vol_ratio,
        "sub_scores": {
            "rsi":       rsi_sc,
            "macd":      macd_sc,
            "bb":        bb_sc,
            "ema_trend": ema_sc,
            "momentum":  mom_sc,
            "volume":    vol_sc,
        },
    }


def is_alert_worthy(sig: dict) -> bool:
    """
    Return True if this reading warrants an intraday alert.

    Triggers:
      - RSI < 35  (oversold — potential accumulation zone)
      - RSI > 68  (overbought — caution)
      - Signal is Strong Buy or Strong Sell
    """
    rsi = sig["rsi"]
    signal = sig["signal"]
    return rsi < 35 or rsi > 68 or signal in ("Strong Buy", "Strong Sell")
