from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

from emcure_tracker import config

logger = logging.getLogger(__name__)

# ── Session-level cache (OHLCV refreshed once per trading day) ─────────────
_ohlcv_cache: Optional[pd.DataFrame] = None
_ohlcv_cache_date: Optional[datetime.date] = None


@dataclass(frozen=True)
class QuoteData:
    price: float
    open: float
    high: float
    low: float
    prev_close: float
    change: float
    change_pct: str
    volume: int
    delivery_pct: float          # FR2: % of total volume that was delivery
    week_52_high: float          # FR3
    week_52_low: float           # FR3
    latest_trading_day: str


@dataclass(frozen=True)
class OHLCVData:
    df: pd.DataFrame             # columns: date, open, high, low, close, volume


# ── OHLCV historical (cached daily) ───────────────────────────────────────

def fetch_ohlcv(force: bool = False) -> Optional[OHLCVData]:
    global _ohlcv_cache, _ohlcv_cache_date

    today = datetime.date.today()
    if not force and _ohlcv_cache is not None and _ohlcv_cache_date == today:
        return OHLCVData(df=_ohlcv_cache)

    try:
        ticker = yf.Ticker(config.STOCK_SYMBOL)
        raw = ticker.history(period="6mo", auto_adjust=True)
        if raw.empty:
            logger.warning("yfinance returned empty OHLCV for %s", config.STOCK_SYMBOL)
            return None

        df = raw.reset_index()
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"date": "date"})[["date", "open", "high", "low", "close", "volume"]]
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.sort_values("date").reset_index(drop=True)

        _ohlcv_cache = df
        _ohlcv_cache_date = today
        return OHLCVData(df=df)

    except Exception:
        logger.exception("fetch_ohlcv failed")
        if _ohlcv_cache is not None:
            logger.warning("Returning stale OHLCV cache")
            return OHLCVData(df=_ohlcv_cache)
        return None


# ── Live quote (fetched every refresh cycle) ───────────────────────────────

def fetch_quote() -> Optional[QuoteData]:
    try:
        ticker = yf.Ticker(config.STOCK_SYMBOL)
        info = ticker.fast_info

        price = float(info.last_price or 0)
        prev_close = float(info.previous_close or 0)
        change = round(price - prev_close, 2)
        change_pct = f"{(change / prev_close * 100):+.2f}%" if prev_close else "N/A"

        # Delivery % — available in some markets via yfinance actions; fallback to 0
        delivery_pct = _fetch_delivery_pct(ticker)

        return QuoteData(
            price=round(price, 2),
            open=round(float(info.open or 0), 2),
            high=round(float(info.day_high or 0), 2),
            low=round(float(info.day_low or 0), 2),
            prev_close=round(prev_close, 2),
            change=change,
            change_pct=change_pct,
            volume=int(info.last_volume or 0),
            delivery_pct=delivery_pct,
            week_52_high=round(float(info.year_high or 0), 2),
            week_52_low=round(float(info.year_low or 0), 2),
            latest_trading_day=str(datetime.date.today()),
        )

    except Exception:
        logger.exception("fetch_quote failed")
        return _quote_from_cache()


def _fetch_delivery_pct(ticker: yf.Ticker) -> float:
    """
    NSE delivery percentage via yfinance. Returns 0.0 if unavailable.
    yfinance exposes this through the full .info dict for Indian stocks.
    """
    try:
        info = ticker.info
        delivery_vol = info.get("deliveryQuantity") or info.get("delivery_quantity")
        total_vol = info.get("volume") or info.get("regularMarketVolume")
        if delivery_vol and total_vol and total_vol > 0:
            return round(delivery_vol / total_vol * 100, 1)
    except Exception:
        pass
    return 0.0


def _quote_from_cache() -> Optional[QuoteData]:
    if _ohlcv_cache is None or _ohlcv_cache.empty:
        return None
    last = _ohlcv_cache.iloc[-1]
    prev = _ohlcv_cache.iloc[-2] if len(_ohlcv_cache) > 1 else last
    change = round(float(last["close"]) - float(prev["close"]), 2)
    prev_close = float(prev["close"])
    change_pct = f"{(change / prev_close * 100):+.2f}%" if prev_close else "N/A"
    return QuoteData(
        price=round(float(last["close"]), 2),
        open=round(float(last["open"]), 2),
        high=round(float(last["high"]), 2),
        low=round(float(last["low"]), 2),
        prev_close=round(prev_close, 2),
        change=change,
        change_pct=change_pct,
        volume=int(last["volume"]),
        delivery_pct=0.0,
        week_52_high=round(float(_ohlcv_cache["high"].max()), 2),
        week_52_low=round(float(_ohlcv_cache["low"].min()), 2),
        latest_trading_day=str(last["date"].date()),
    )
