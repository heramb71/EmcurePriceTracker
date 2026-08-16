"""Kite historical OHLCV data — daily and intraday, via the same authenticated
Kite session KittyBot's live broker integration already holds.

Deliberately NOT wired into ``src.kittybot.marketdata`` or the ``Broker``
Protocol — those are live, per-tick concerns the engine calls during trading
hours; multi-year historical OHLCV is a backtesting-only concern with no
live-loop purpose. Consumed directly by offline backtest scripts. Returns the
same DataFrame shape as :func:`src.shared.data.fetch_daily`/``fetch_intraday``
(``date,open,high,low,close,volume``, ascending, tz-naive) so it's a drop-in
data source for anything already built against that convention.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from src.execution.instruments import instrument_token

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 2
_INTRADAY_CHUNK_DAYS = 60  # Kite caps 'minute'-interval requests to ~60 days/call


def _normalise(rows: list[dict]) -> pd.DataFrame:
    """Kite's historical_data() rows -> the repo-wide OHLCV column shape."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df[["date", "open", "high", "low", "close", "volume"]]


def _fetch_with_retry(kite, *, token: int, from_date: datetime, to_date: datetime,
                      interval: str) -> Optional[list[dict]]:
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            rows = kite.historical_data(token, from_date, to_date, interval)
            if rows:
                return rows
            logger.warning("kite.historical_data empty for token %s (%s/%s), attempt %d/%d",
                           token, from_date, to_date, attempt, _MAX_RETRIES)
        except Exception:
            logger.exception("kite.historical_data error for token %s, attempt %d/%d",
                             token, attempt, _MAX_RETRIES)
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_BACKOFF_S * attempt)
    return None


def fetch_daily(kite_broker, symbol: str, days: int = 100) -> Optional[pd.DataFrame]:
    """Daily OHLCV via Kite. Same column/shape contract as
    ``src.shared.data.fetch_daily``."""
    token = instrument_token(kite_broker.kite, symbol)
    if token is None:
        logger.error("fetch_daily: no instrument_token for %s", symbol)
        return None
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)
    rows = _fetch_with_retry(kite_broker.kite, token=token,
                             from_date=from_date, to_date=to_date, interval="day")
    if rows is None:
        logger.error("fetch_daily exhausted retries for %s", symbol)
        return None
    return _normalise(rows).sort_values("date").reset_index(drop=True)


def fetch_intraday(kite_broker, symbol: str, interval: str = "minute",
                   days: int = 60) -> Optional[pd.DataFrame]:
    """Intraday OHLCV via Kite (default 1-minute — the whole point: yfinance
    caps this at ~8 days, Kite goes back to 2015). Kite caps 'minute' requests
    to ~60 days/call, so multi-year pulls are chunked + concatenated here.
    """
    token = instrument_token(kite_broker.kite, symbol)
    if token is None:
        logger.error("fetch_intraday: no instrument_token for %s", symbol)
        return None

    to_date = datetime.now()
    cursor = to_date - timedelta(days=days)
    chunks: list[pd.DataFrame] = []
    while cursor < to_date:
        chunk_end = min(cursor + timedelta(days=_INTRADAY_CHUNK_DAYS), to_date)
        rows = _fetch_with_retry(kite_broker.kite, token=token,
                                 from_date=cursor, to_date=chunk_end, interval=interval)
        if rows:
            chunks.append(_normalise(rows))
        cursor = chunk_end

    if not chunks:
        logger.error("fetch_intraday exhausted retries for %s", symbol)
        return None
    combined = pd.concat(chunks, ignore_index=True)
    return combined.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
