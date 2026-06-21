"""Stock universe + liquidity filter for the swing lab.

The universe is the user-final set. yfinance needs the NSE ``.NS`` suffix.
The liquidity gate (avg daily traded value > ₹100 Cr) is applied at backtest
time from the same daily bars, so it stays honest to the tested period.
"""
from __future__ import annotations

# User-final universe (high-beta PSU / thematic / pharma).
SYMBOLS: tuple[str, ...] = (
    "EMCURE",
    "ICICIBANK",
    "IREDA",
    "IRFC",
    "HUDCO",
    "SUZLON",
)

NIFTY = "^NSEI"

# Liquidity gate: average daily traded value over the trailing window.
MIN_AVG_TRADED_VALUE_CR = 100.0  # ₹ crore
LIQUIDITY_LOOKBACK = 20  # trading days


def to_yf(symbol: str) -> str:
    """Map a bare NSE symbol to its yfinance ticker."""
    return symbol if symbol.startswith("^") else f"{symbol}.NS"


def avg_traded_value_cr(close, volume, lookback: int = LIQUIDITY_LOOKBACK) -> float:
    """Average daily traded value (₹ crore) over the trailing ``lookback`` bars."""
    if close is None or volume is None or len(close) < 1:
        return 0.0
    window = min(lookback, len(close))
    tv = (close.tail(window) * volume.tail(window)).mean()
    return float(tv) / 1e7  # rupees → crore
