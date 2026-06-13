"""
Crypto data fetching via yfinance.

yfinance supports BTC-USD, ETH-USD directly — no paid API needed.
USD/INR is fetched from USDINR=X with an 84.0 fallback.
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_USDINR_SYMBOL = "USDINR=X"
_USDINR_FALLBACK = 84.0


def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns and store the DatetimeIndex as a plain 'date' column."""
    dates = pd.to_datetime(raw.index).tz_localize(None)
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df = df.reset_index(drop=True)
    df.insert(0, "date", dates.values)
    return df


def fetch_usd_inr() -> float:
    """Return the current USD/INR exchange rate, or 84.0 on failure."""
    try:
        t = yf.Ticker(_USDINR_SYMBOL)
        price = float(t.fast_info.last_price or 0)
        if price > 0:
            return round(price, 4)
    except Exception:
        logger.exception("fetch_usd_inr failed")
    logger.warning("Using fallback USD/INR: %.1f", _USDINR_FALLBACK)
    return _USDINR_FALLBACK


def fetch_crypto_daily(symbol: str, days: int = 250) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV for a crypto ticker (e.g. 'BTC-USD', 'ETH-USD').

    Returns a normalised DataFrame with columns: date, open, high, low, close, volume.
    Returns None on failure.
    """
    try:
        raw = yf.download(symbol, period=f"{days}d", interval="1d", progress=False)
        if raw is None or raw.empty:
            logger.warning("fetch_crypto_daily: empty response for %s", symbol)
            return None
        df = _normalise(raw)
        df = df.sort_values("date").reset_index(drop=True)
        # Drop rows with missing close (can happen at weekends in some sources)
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        return df
    except Exception:
        logger.exception("fetch_crypto_daily failed for %s", symbol)
        return None


def fetch_crypto_quote(symbol: str, usd_inr: float) -> Optional[dict]:
    """
    Fetch the live quote for a crypto ticker and return prices in both USD and INR.

    Falls back to the last daily close from fast_info if live price is unavailable.
    Returns None on failure.
    """
    try:
        t = yf.Ticker(symbol)
        info = t.fast_info

        price_usd = float(info.last_price or 0)
        prev_close_usd = float(info.previous_close or 0)

        if price_usd <= 0:
            logger.warning("fetch_crypto_quote: zero price for %s", symbol)
            return None

        change_usd = price_usd - prev_close_usd
        change_pct = round(change_usd / prev_close_usd * 100, 2) if prev_close_usd > 0 else 0.0

        high_usd = float(info.day_high or price_usd)
        low_usd = float(info.day_low or price_usd)

        return {
            "price_usd":      round(price_usd, 2),
            "price_inr":      round(price_usd * usd_inr, 2),
            "prev_close_usd": round(prev_close_usd, 2),
            "prev_close_inr": round(prev_close_usd * usd_inr, 2),
            "high_usd":       round(high_usd, 2),
            "low_usd":        round(low_usd, 2),
            "high_inr":       round(high_usd * usd_inr, 2),
            "low_inr":        round(low_usd * usd_inr, 2),
            "change_pct":     change_pct,
            "usd_inr":        usd_inr,
        }
    except Exception:
        logger.exception("fetch_crypto_quote failed for %s", symbol)
        return None
