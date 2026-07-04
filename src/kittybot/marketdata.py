"""Market-data I/O for KittyBot — the one place that touches yfinance.

Isolated from the decision logic so the pure modules stay testable and this thin
layer can be swapped for a broker feed later. Every function returns ``None``/empty
on failure (codebase convention) so a bad poll degrades gracefully.

Volume convention: the opening range's ``avg_volume`` is a *per-minute* baseline
(avg daily volume ÷ session minutes), and breakout volume is the latest 1-minute
bar's volume — so "above-average volume" compares like with like.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Optional

import pandas as pd
import yfinance as yf

from src.kittybot.filters import OpenQuote
from src.kittybot.opening_range import OpeningRange, build_opening_range
from src.shared.data import fetch_daily, fetch_intraday

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_SESSION_MINUTES = 375  # 09:15–15:30
_MARKET_OPEN_T = dtime(9, 15)


def opening_quote(symbol: str) -> Optional[OpenQuote]:
    """Return today's open and previous close, or ``None`` if unavailable."""
    daily = fetch_daily(symbol, days=5)
    if daily is None or len(daily) < 2:
        return None
    prev_close = float(daily.iloc[-2]["close"])
    intraday = fetch_intraday(symbol, interval="1m", days=1)
    if intraday is None or intraday.empty:
        # Before the first 1-min bar prints, fall back to the daily open.
        open_px = float(daily.iloc[-1]["open"])
    else:
        open_px = float(intraday.iloc[0]["open"])
    return OpenQuote(open=round(open_px, 2), prev_close=round(prev_close, 2))


def _avg_per_min_volume(symbol: str) -> float:
    """Per-minute volume baseline from the trailing daily average volume."""
    daily = fetch_daily(symbol, days=30)
    if daily is None or daily.empty:
        return 0.0
    avg_daily = float(daily.tail(20)["volume"].mean())
    if pd.isna(avg_daily):
        return 0.0
    return avg_daily / _SESSION_MINUTES


def _today_1m_bars(symbol: str) -> Optional[pd.DataFrame]:
    df = fetch_intraday(symbol, interval="1m", days=1)
    if df is None or df.empty:
        return None
    today = datetime.now(_IST).date()
    df = df[pd.to_datetime(df["date"]).dt.date == today]
    return df if not df.empty else None


def opening_range(symbol: str, or_minutes: int) -> Optional[OpeningRange]:
    """Build the opening range from the first ``or_minutes`` 1-minute bars."""
    df = _today_1m_bars(symbol)
    if df is None:
        return None
    window_end = (datetime.combine(datetime.now(_IST).date(), _MARKET_OPEN_T)
                  + timedelta(minutes=or_minutes)).time()
    ts = pd.to_datetime(df["date"]).dt.time
    window = df[(ts >= _MARKET_OPEN_T) & (ts < window_end)]
    if window.empty:
        return None
    bars = [
        {"high": r.high, "low": r.low, "volume": r.volume}
        for r in window.itertuples()
    ]
    return build_opening_range(bars, avg_volume=_avg_per_min_volume(symbol))


def live_tick(symbol: str) -> Optional[tuple[float, float]]:
    """Return ``(last_price, last_1m_volume)`` from the freshest 1-minute bar."""
    df = _today_1m_bars(symbol)
    if df is None:
        return None
    last = df.iloc[-1]
    return round(float(last["close"]), 2), float(last["volume"] or 0.0)


def india_vix() -> tuple[Optional[float], Optional[float]]:
    """Return ``(vix_now, vix_prev_close)`` for India VIX, or ``(None, None)``."""
    try:
        t = yf.Ticker("^INDIAVIX")
        info = t.fast_info
        now = float(info.last_price or 0) or None
        prev = float(info.previous_close or 0) or None
        return now, prev
    except Exception:
        logger.exception("india_vix fetch failed")
        return None, None
