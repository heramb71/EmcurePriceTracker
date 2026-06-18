"""
Daily-bar data loaders for the swing system.

Wraps yfinance for the multi-symbol universe and the index inputs (NIFTY, VIX).
Returns frames with a plain ``date`` column (python ``date``) and lower-cased
OHLCV columns, sorted ascending — the shape the indicators and backtester expect.
Network failures return ``None`` so callers degrade gracefully (never raise).

Index symbols (``^NSEI``, ``^INDIAVIX``) must NOT get the ``.NS`` suffix, so they
use a separate path from equities.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 2


def _download(symbol: str, period: str = "3y", interval: str = "1d") -> Optional[pd.DataFrame]:
    """yf.download with retry/backoff; normalised OHLCV + `date` column or None."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            raw = yf.download(symbol, period=period, interval=interval,
                              progress=False, auto_adjust=False)
            if raw is not None and not raw.empty:
                return _normalise(raw)
            logger.warning("empty download for %s (%s/%s) %d/%d",
                           symbol, period, interval, attempt, _MAX_RETRIES)
        except Exception:
            logger.exception("download error for %s %d/%d", symbol, attempt, _MAX_RETRIES)
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_BACKOFF_S * attempt)
    return None


def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns, lower-case, attach a `date` column, drop NaN OHLC."""
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    dates = [d.date() for d in pd.to_datetime(df.index)]
    df = df.reset_index(drop=True)
    df.insert(0, "date", dates)
    keep = [c for c in ("open", "high", "low", "close") if c in df.columns]
    return df.dropna(subset=keep).sort_values("date").reset_index(drop=True)


def fetch_equity(symbol: str, period: str = "3y") -> Optional[pd.DataFrame]:
    """Daily OHLCV for a bare NSE equity symbol (``.NS`` appended)."""
    return _download(f"{symbol}.NS", period=period, interval="1d")


def fetch_index(symbol: str, period: str = "3y") -> Optional[pd.DataFrame]:
    """Daily OHLCV for an index symbol (e.g. ``^NSEI``) — no ``.NS`` suffix."""
    return _download(symbol, period=period, interval="1d")


def fetch_universe(symbols: list[str], period: str = "3y") -> dict[str, pd.DataFrame]:
    """Fetch daily bars for many equities; skips symbols that fail to load."""
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = fetch_equity(sym, period=period)
        if df is None or df.empty:
            logger.warning("skipping %s — no data", sym)
            continue
        out[sym] = df
    return out
