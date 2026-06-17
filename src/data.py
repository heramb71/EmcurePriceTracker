from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Transient yfinance failures (rate limits, empty payloads) are common. Retry a
# few times with a short backoff before giving up so a single bad poll doesn't
# blank out a whole cycle.
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 2


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
                "yf.download empty for %s (%s/%s), attempt %d/%d",
                symbol, period, interval, attempt, _MAX_RETRIES,
            )
        except Exception:
            logger.exception(
                "yf.download error for %s, attempt %d/%d", symbol, attempt, _MAX_RETRIES
            )
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_BACKOFF_S * attempt)
    return None


def _normalise(raw: "pd.DataFrame") -> "pd.DataFrame":
    """Flatten MultiIndex columns and attach the DatetimeIndex as a 'date' column."""
    # Preserve the datetime index before any manipulation
    dates = pd.to_datetime(raw.index).tz_localize(None)

    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]

    # Drop the index entirely and insert dates as a plain column
    df = df.reset_index(drop=True)
    df.insert(0, "date", dates.values)
    return df


def fetch_daily(ticker: str, days: int = 100) -> Optional[pd.DataFrame]:
    raw = _download_with_retry(f"{ticker}.NS", period=f"{days}d", interval="1d")
    if raw is None:
        logger.error("fetch_daily exhausted retries for %s", ticker)
        return None
    df = _normalise(raw)
    # yfinance appends an all-NaN placeholder row for "today" before the session
    # has data (pre-market). It poisons rolling indicators — most visibly ATR,
    # whose True Range over the NaN row makes the latest value NaN → 0. Drop any
    # row missing OHLC so every indicator sees only real bars.
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_intraday(ticker: str, interval: str = "5m", days: int = 5) -> Optional[pd.DataFrame]:
    raw = _download_with_retry(f"{ticker}.NS", period=f"{days}d", interval=interval)
    if raw is None:
        logger.error("fetch_intraday exhausted retries for %s", ticker)
        return None
    df = _normalise(raw)
    return df.sort_values("date").reset_index(drop=True)


def fetch_live_quote(ticker: str) -> Optional[dict]:
    """
    Fetch the freshest available price via yf.Ticker.fast_info.
    Yahoo Finance delays this ~15 min for free users but it reflects
    the current trading session, not just the last daily close.
    Returns None on failure so callers can fall back to daily data.
    """
    try:
        t = yf.Ticker(f"{ticker}.NS")
        info = t.fast_info
        price = float(info.last_price or 0)
        prev_close = float(info.previous_close or 0)
        if price <= 0:
            return None
        change = round(price - prev_close, 2)
        change_pct = round(change / prev_close * 100, 2) if prev_close else 0.0
        import datetime
        return {
            "price": round(price, 2),
            "open": round(float(info.open or 0), 2),
            "high": round(float(info.day_high or 0), 2),
            "low": round(float(info.day_low or 0), 2),
            "close": round(price, 2),
            "volume": int(info.last_volume or 0),
            "prev_close": round(prev_close, 2),
            "change": change,
            "change_pct": change_pct,
            "date": str(datetime.date.today()),
            "source": "live",
        }
    except Exception:
        logger.exception("fetch_live_quote failed for %s", ticker)
        return None


def get_latest_quote(df_daily: pd.DataFrame) -> dict:
    last = df_daily.iloc[-1]
    prev = df_daily.iloc[-2] if len(df_daily) > 1 else last
    price = float(last["close"])
    prev_close = float(prev["close"])
    change = round(price - prev_close, 2)
    change_pct = round(change / prev_close * 100, 2) if prev_close else 0.0
    date_val = last.get("date", "")
    date_str = str(date_val).split(" ")[0] if date_val != "" else ""
    return {
        "price": round(price, 2),
        "open": round(float(last["open"]), 2),
        "high": round(float(last["high"]), 2),
        "low": round(float(last["low"]), 2),
        "close": round(price, 2),
        "volume": int(last["volume"]),
        "prev_close": round(prev_close, 2),
        "change": change,
        "change_pct": change_pct,
        "date": date_str,
        "source": "daily",
    }
