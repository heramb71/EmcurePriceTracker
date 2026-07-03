"""
Corporate-event guard — keep the auto-trader out of new positions around
earnings, where overnight gap risk is highest (relevant now that trades are CNC
and held overnight).

Uses yfinance earnings dates. Fails OPEN: if the calendar can't be fetched, it
returns False (not near an event) so a data hiccup never silently halts trading.
Results are cached per (ticker, day) to avoid hammering the API each poll.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)

# Skip new entries within this many days (either side) of an earnings date.
DEFAULT_EVENT_WINDOW_DAYS = 2

# Cache: (ticker, today_iso) -> bool, so we hit yfinance at most once per day.
_cache: dict[tuple[str, str], bool] = {}


def _nse_symbol(ticker: str) -> str:
    return ticker if ticker.endswith(".NS") else f"{ticker}.NS"


def _upcoming_earnings_dates(ticker: str) -> list[date]:
    """Earnings dates from yfinance, as plain dates. Empty list on failure."""
    try:
        t = yf.Ticker(_nse_symbol(ticker))
        df = t.get_earnings_dates(limit=8)
        if df is None or df.empty:
            return []
        return [idx.date() for idx in df.index]
    except Exception:
        logger.warning("Could not fetch earnings dates for %s — failing open", ticker)
        return []


def is_near_event(
    ticker: str,
    today: Optional[date] = None,
    window_days: int = DEFAULT_EVENT_WINDOW_DAYS,
) -> bool:
    """
    True if `today` is within `window_days` of a known earnings date.

    Fails open (returns False) when the calendar is unavailable.
    """
    today = today or datetime.now().date()
    cache_key = (ticker, today.isoformat())
    if cache_key in _cache:
        return _cache[cache_key]

    near = False
    for ed in _upcoming_earnings_dates(ticker):
        if abs((ed - today).days) <= window_days:
            near = True
            logger.warning(
                "%s is within %d days of earnings (%s) — blocking new entries",
                ticker, window_days, ed,
            )
            break

    _cache[cache_key] = near
    return near
