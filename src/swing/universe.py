"""
Tradeable universe for the swing bot.

The universe is the user-chosen set of NSE symbols. Each carries light metadata so
the scanner/backtester can reason about liquidity and price level (which drive the
cost-drag and position-sizing limits at small capital). Index symbols (NIFTY, VIX)
are kept separate — they are inputs to the regime gate, never trade candidates.

Liquidity note (measured 2026-06, avg ₹-turnover/day): ICICIBANK ~₹2,100 Cr is by
far the most liquid; EMCURE ~₹26 Cr the least and the most volatile, with the
shortest history. The set skews high-beta PSU/thematic — capital preservation must
come from the risk rules (1.5×ATR stop, ₹300 max risk, no-trade discipline), not
from the universe itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Index symbols feed the regime gate; they are NOT trade candidates.
NIFTY = "^NSEI"
INDIA_VIX = "^INDIAVIX"


@dataclass(frozen=True)
class Instrument:
    symbol: str          # bare NSE symbol, e.g. "ICICIBANK"
    name: str
    note: str = ""       # liquidity / history caveats surfaced in reports

    @property
    def yf_symbol(self) -> str:
        """yfinance ticker for an NSE equity."""
        return f"{self.symbol}.NS"


# User-final universe (refined brief, 2026-06-18).
UNIVERSE: tuple[Instrument, ...] = (
    Instrument("EMCURE", "Emcure Pharmaceuticals", "least liquid, <2y history, high event risk"),
    Instrument("ICICIBANK", "ICICI Bank", "only low-beta name; weakest under momentum rules"),
    Instrument("IREDA", "Indian Renewable Energy Dev Agency", "<2y history, very high beta"),
    Instrument("IRFC", "Indian Railway Finance Corp", "PSU"),
    Instrument("HUDCO", "Housing & Urban Development Corp", "PSU"),
    Instrument("SUZLON", "Suzlon Energy", "low-priced, very high beta"),
)

_BY_SYMBOL = {i.symbol: i for i in UNIVERSE}


def symbols() -> list[str]:
    """Bare NSE symbols in the tradeable universe."""
    return [i.symbol for i in UNIVERSE]


def yf_symbols() -> list[str]:
    """yfinance tickers for the tradeable universe."""
    return [i.yf_symbol for i in UNIVERSE]


def get(symbol: str) -> Optional[Instrument]:
    """Look up an instrument by bare symbol, or None if not in the universe."""
    return _BY_SYMBOL.get(symbol)
