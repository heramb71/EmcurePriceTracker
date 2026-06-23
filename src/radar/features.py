"""Per-stock scalar feature snapshot for the radar.

Pulls daily + intraday bars via ``src.data`` and reduces them to the last-bar
scalars the signal detectors and scorer need. All indicator math reuses the live
``src.indicators`` stack (lowercase-column frames). Returns ``None`` on any data
failure — never raises to the scan loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.data import _download_with_retry, _normalise, fetch_daily, fetch_intraday
from src.indicators import (
    compute_atr,
    compute_avg_volume,
    compute_ema,
    compute_macd,
    compute_rsi,
    compute_vwap,
)
from src.intraday import compute_sma7
from src.radar.universe import adtv_cr

logger = logging.getLogger(__name__)

_ATR_AVG_WINDOW = 20  # trailing window for ATR-expansion baseline


@dataclass(frozen=True)
class StockFeatures:
    """Last-bar snapshot for one symbol. All prices in ₹."""

    stock: str
    price: float
    prev_close: float
    open: float
    sma7: float
    gap_to_sma7: float          # price - sma7 (negative = below SMA7)
    vwap: float
    rsi: float
    atr: float
    atr_expansion: float        # atr / 20d-avg-atr (>1 = expanding)
    rvol: float                 # today volume / 20d-avg-volume
    ema20: float
    ema50: float
    prev_high: float
    gap_pct: float              # (open - prev_close) / prev_close * 100
    rs20: float                 # 20d return minus NIFTY 20d return (fraction)
    adtv_cr: float
    above_50dma: bool
    # ── EOD-summary extras (defaulted so older construction sites stay valid) ──
    day_high: float = 0.0       # today's daily high
    day_low: float = 0.0        # today's daily low
    macd_hist: float = 0.0      # MACD histogram (>0 = short-term trend up)


def fetch_index_daily(symbol: str = "^NSEI", days: int = 120) -> Optional[pd.DataFrame]:
    """Daily bars for an index ticker (keeps the ``^`` prefix — no ``.NS``).

    Reuses ``src.data`` download/normalise helpers so retry/cleanup behaviour
    matches the rest of the system.
    """
    raw = _download_with_retry(symbol, period=f"{days}d", interval="1d")
    if raw is None:
        logger.error("fetch_index_daily exhausted retries for %s", symbol)
        return None
    df = _normalise(raw).dropna(subset=["open", "high", "low", "close"])
    return df.sort_values("date").reset_index(drop=True)


def _avg_atr(df_daily: pd.DataFrame, period: int = 14, window: int = _ATR_AVG_WINDOW) -> float:
    """Mean ATR over the trailing ``window`` bars (excludes the current bar)."""
    vals = []
    n = len(df_daily)
    if n < period + 2:
        return 0.0
    for i in range(max(period + 1, n - window), n):
        vals.append(compute_atr(df_daily.iloc[: i], period=period))
    vals = [v for v in vals if v > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _return(df_daily: pd.DataFrame, window: int = 20) -> float:
    """Trailing ``window``-bar simple return (fraction)."""
    if len(df_daily) <= window:
        return 0.0
    last = float(df_daily["close"].iloc[-1])
    base = float(df_daily["close"].iloc[-1 - window])
    return (last - base) / base if base else 0.0


def build_snapshot(
    ticker: str, nifty_daily: Optional[pd.DataFrame] = None
) -> Optional[StockFeatures]:
    """Build a :class:`StockFeatures` for ``ticker`` or ``None`` on failure."""
    df = fetch_daily(ticker, days=120)
    if df is None or len(df) < 30:
        logger.warning("build_snapshot: insufficient daily data for %s", ticker)
        return None

    intraday = fetch_intraday(ticker, interval="5m", days=5)

    try:
        close = df["close"]
        price = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        open_ = float(df["open"].iloc[-1])
        prev_high = float(df["high"].iloc[-2])
        day_high = float(df["high"].iloc[-1])
        day_low = float(df["low"].iloc[-1])
        _, _, macd_hist = compute_macd(close)

        sma7 = compute_sma7(df)
        rsi = compute_rsi(close)
        atr = compute_atr(df)
        avg_atr = _avg_atr(df)
        atr_expansion = round(atr / avg_atr, 2) if avg_atr > 0 else 1.0

        avg_vol = compute_avg_volume(df, days=20)
        today_vol = int(df["volume"].iloc[-1])
        rvol = round(today_vol / avg_vol, 2) if avg_vol > 0 else 0.0

        ema20 = compute_ema(close, 20)
        ema50 = compute_ema(close, 50)
        dma50 = float(close.tail(50).mean())

        vwap = compute_vwap(intraday) if intraday is not None else 0.0
        if vwap <= 0:
            vwap = price  # neutral fallback when intraday VWAP unavailable

        gap_pct = round((open_ - prev_close) / prev_close * 100, 2) if prev_close else 0.0

        rs20 = _return(df, 20)
        if nifty_daily is not None and len(nifty_daily) > 20:
            rs20 -= _return(nifty_daily, 20)

        return StockFeatures(
            stock=ticker,
            price=round(price, 2),
            prev_close=round(prev_close, 2),
            open=round(open_, 2),
            sma7=round(sma7, 2),
            gap_to_sma7=round(price - sma7, 2),
            vwap=round(vwap, 2),
            rsi=rsi,
            atr=atr,
            atr_expansion=atr_expansion,
            rvol=rvol,
            ema20=ema20,
            ema50=ema50,
            prev_high=round(prev_high, 2),
            gap_pct=gap_pct,
            rs20=round(rs20, 4),
            adtv_cr=round(adtv_cr(df), 1),
            above_50dma=price > dma50,
            day_high=round(day_high, 2),
            day_low=round(day_low, 2),
            macd_hist=macd_hist,
        )
    except Exception:
        logger.exception("build_snapshot failed for %s", ticker)
        return None
