"""Shared fixtures + synthetic OHLCV builders for KittyBot tests."""
from __future__ import annotations

from src.kittybot.config import KittyBotConfig
from src.kittybot.opening_range import OpeningRange
from src.kittybot.picks import Pick


def make_pick(symbol: str = "TATAMOTORS", **overrides) -> Pick:
    """A sensible default pick; override any field by keyword."""
    base = dict(
        symbol=symbol,
        score=90.0,
        atr14_pct=2.5,
        avg_range_60d_pct=3.0,
        hit_rate_2pct=0.6,
        long_room_2pct=3.0,
        short_room_2pct=1.0,
        suggested_target_pct=3.0,
        suggested_stop_pct=1.5,
        prev_close=1000.0,
        earnings_today=False,
    )
    base.update(overrides)
    return Pick(**base)


def make_bars(highs, lows, volumes=None) -> list[dict]:
    """Build a list of OHLCV-ish bar dicts from parallel high/low/volume lists."""
    n = len(highs)
    volumes = volumes if volumes is not None else [1000] * n
    return [
        {"high": h, "low": lo, "volume": v, "open": lo, "close": h}
        for h, lo, v in zip(highs, lows, volumes)
    ]


def make_or(high=105.0, low=100.0, volume=15000.0, avg_volume=1000.0) -> OpeningRange:
    return OpeningRange(high=high, low=low, volume=volume, avg_volume=avg_volume)


def make_config(**overrides) -> KittyBotConfig:
    from dataclasses import replace
    return replace(KittyBotConfig(), **overrides)
