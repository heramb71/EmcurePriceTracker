"""
Crypto data fetching via yfinance.

yfinance supports BTC-USD, ETH-USD directly — no paid API needed.
USD/INR is fetched from USDINR=X with an 84.0 fallback.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger(__name__)

_USDINR_SYMBOL = "USDINR=X"
_USDINR_FALLBACK = 84.0

_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 2

# yfinance crypto symbol → CoinGecko coin id, for the fallback source.
_COINGECKO_IDS = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
}
_COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def _download_with_retry(
    symbol: str, *, period: str, interval: str
) -> Optional[pd.DataFrame]:
    """yf.download with retry/backoff. Returns a non-empty DataFrame or None."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            raw = yf.download(symbol, period=period, interval=interval, progress=False)
            if raw is not None and not raw.empty:
                return raw
            logger.warning(
                "yf.download empty for %s, attempt %d/%d", symbol, attempt, _MAX_RETRIES
            )
        except Exception:
            logger.exception(
                "yf.download error for %s, attempt %d/%d", symbol, attempt, _MAX_RETRIES
            )
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_BACKOFF_S * attempt)
    return None


def _coingecko_daily(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Fallback daily OHLC from CoinGecko. No volume column (zero-filled)."""
    coin_id = _COINGECKO_IDS.get(symbol)
    if not coin_id:
        return None
    try:
        resp = requests.get(
            f"{_COINGECKO_BASE}/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": str(min(days, 365))},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
        df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.tz_localize(None)
        df["volume"] = 0
        df = df[["date", "open", "high", "low", "close", "volume"]]
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        logger.warning("Using CoinGecko fallback daily data for %s", symbol)
        return df
    except Exception:
        logger.exception("CoinGecko daily fallback failed for %s", symbol)
        return None


def _coingecko_quote(symbol: str, usd_inr: float) -> Optional[dict]:
    """Fallback live quote from CoinGecko simple price API."""
    coin_id = _COINGECKO_IDS.get(symbol)
    if not coin_id:
        return None
    try:
        resp = requests.get(
            f"{_COINGECKO_BASE}/simple/price",
            params={
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get(coin_id, {})
        price_usd = float(data.get("usd") or 0)
        if price_usd <= 0:
            return None
        change_pct = round(float(data.get("usd_24h_change") or 0.0), 2)
        prev_close_usd = price_usd / (1 + change_pct / 100) if change_pct else price_usd
        logger.warning("Using CoinGecko fallback quote for %s", symbol)
        return {
            "price_usd":      round(price_usd, 2),
            "price_inr":      round(price_usd * usd_inr, 2),
            "prev_close_usd": round(prev_close_usd, 2),
            "prev_close_inr": round(prev_close_usd * usd_inr, 2),
            "high_usd":       round(price_usd, 2),
            "low_usd":        round(price_usd, 2),
            "high_inr":       round(price_usd * usd_inr, 2),
            "low_inr":        round(price_usd * usd_inr, 2),
            "change_pct":     change_pct,
            "usd_inr":        usd_inr,
        }
    except Exception:
        logger.exception("CoinGecko quote fallback failed for %s", symbol)
        return None


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

    Tries yfinance (with retry), then falls back to CoinGecko. Returns a
    normalised DataFrame (date, open, high, low, close, volume) or None.
    """
    raw = _download_with_retry(symbol, period=f"{days}d", interval="1d")
    if raw is not None:
        df = _normalise(raw)
        df = df.sort_values("date").reset_index(drop=True)
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        if not df.empty:
            return df

    logger.warning("fetch_crypto_daily: yfinance failed for %s — trying CoinGecko", symbol)
    return _coingecko_daily(symbol, days)


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
            logger.warning("fetch_crypto_quote: zero price for %s — trying CoinGecko", symbol)
            return _coingecko_quote(symbol, usd_inr)

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
        logger.exception("fetch_crypto_quote failed for %s — trying CoinGecko", symbol)
        return _coingecko_quote(symbol, usd_inr)
