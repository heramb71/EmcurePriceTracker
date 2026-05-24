from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

from emcure_tracker import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FIIDIIData:
    fii_net: float              # FII net buy/sell in crores (positive = net buy)
    dii_net: float              # DII net buy/sell in crores
    date: str
    available: bool             # False when NSE data could not be fetched


@dataclass(frozen=True)
class SectorData:
    df: pd.DataFrame            # columns: date, open, high, low, close, volume
    latest_return_pct: float    # Nifty Pharma 1-day return %


# ── FII / DII flows ────────────────────────────────────────────────────────

def fetch_fii_dii() -> Optional[FIIDIIData]:
    try:
        from nsepython import nse_fiidii  # type: ignore[import]
        df = nse_fiidii()  # returns DataFrame with columns: buyValue, category, date, netValue, sellValue
        if df is None or df.empty:
            return _unavailable_fii()

        # Most recent row per category
        latest_date = df["date"].iloc[0]
        fii_row = df[df["category"].str.upper().str.startswith("FII")]
        dii_row = df[df["category"].str.upper() == "DII"]
        fii_net = float(fii_row["netValue"].iloc[0]) if not fii_row.empty else 0.0
        dii_net = float(dii_row["netValue"].iloc[0]) if not dii_row.empty else 0.0
        return FIIDIIData(fii_net=fii_net, dii_net=dii_net, date=str(latest_date), available=True)

    except Exception:
        logger.exception("fetch_fii_dii failed")
        return _unavailable_fii()


def _unavailable_fii() -> FIIDIIData:
    return FIIDIIData(fii_net=0.0, dii_net=0.0, date="", available=False)


# ── Nifty Pharma sector index ──────────────────────────────────────────────

def fetch_sector() -> Optional[SectorData]:
    try:
        ticker = yf.Ticker(config.SECTOR_INDEX_SYMBOL)
        raw = ticker.history(period="5d", auto_adjust=True)
        if raw.empty or len(raw) < 2:
            return None

        df = raw.reset_index()
        df.columns = [c.lower() for c in df.columns]
        df = df[["date", "open", "high", "low", "close", "volume"]].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.sort_values("date").reset_index(drop=True)

        prev_close = float(df["close"].iloc[-2])
        last_close = float(df["close"].iloc[-1])
        ret_pct = round((last_close - prev_close) / prev_close * 100, 3) if prev_close else 0.0

        return SectorData(df=df, latest_return_pct=ret_pct)

    except Exception:
        logger.exception("fetch_sector failed")
        return None
