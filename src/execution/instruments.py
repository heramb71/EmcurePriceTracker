"""Kite instrument-token lookup + disk cache.

``kite.historical_data(...)`` needs a numeric ``instrument_token``, not the bare
NSE tradingsymbol strings used everywhere else in this repo (see
``_nse_symbol`` in :mod:`src.execution.broker`). Kite's instrument master is a
large, slow-changing dump (``kite.instruments("NSE")``) — this module fetches
it once per day and caches the tradingsymbol→token mapping to disk, mirroring
the ``KITE_TOKEN_FILE`` override convention already used for the auth token.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from src.shared.atomic_json import read_json, write_json

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_PATH = Path("/opt/emcure/kite_instruments_nse.json")
_CACHE_MAX_AGE_HOURS = 24.0


def _cache_path() -> Path:
    """Allow override via env var for local dev, mirroring KITE_TOKEN_FILE."""
    return Path(os.getenv("KITE_INSTRUMENTS_CACHE", str(_DEFAULT_CACHE_PATH)))


def refresh_instrument_map(kite) -> dict[str, int]:
    """Fetch the full NSE instrument master from Kite; cache tradingsymbol→token."""
    raw = kite.instruments("NSE")
    mapping = {str(row["tradingsymbol"]).upper(): int(row["instrument_token"]) for row in raw}
    write_json(str(_cache_path()), {"fetched_at": time.time(), "map": mapping})
    return mapping


def instrument_token(kite, symbol: str) -> Optional[int]:
    """Resolve a bare NSE symbol to its Kite ``instrument_token``.

    Uses the disk cache when fresh (< 24h old), else refreshes from Kite.
    Returns ``None`` on failure — never raises, matching this repo's
    "return None/empty on failure" convention.
    """
    symbol = symbol.replace(".NS", "").upper()
    cached = read_json(str(_cache_path()), None)
    fresh = bool(cached) and (time.time() - cached.get("fetched_at", 0)) < _CACHE_MAX_AGE_HOURS * 3600
    mapping = cached.get("map") if fresh and cached else None

    if mapping is None or symbol not in mapping:
        try:
            mapping = refresh_instrument_map(kite)
        except Exception:
            logger.exception("instrument master refresh failed")
            return None

    return mapping.get(symbol)
