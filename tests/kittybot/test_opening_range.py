"""Opening-range construction from synthetic bars."""
from __future__ import annotations

from src.kittybot.opening_range import build_opening_range

from .conftest import make_bars


def test_build_from_bars_takes_extremes_and_sums_volume():
    bars = make_bars(highs=[101, 104, 103], lows=[99, 100, 98], volumes=[500, 700, 300])
    or_range = build_opening_range(bars, avg_volume=1000.0)
    assert or_range.high == 104
    assert or_range.low == 98
    assert or_range.volume == 1500
    assert or_range.avg_volume == 1000.0
    assert or_range.width == 6


def test_build_returns_none_for_empty_bars():
    assert build_opening_range([], avg_volume=1000.0) is None


def test_build_ignores_missing_fields():
    bars = [{"high": 105, "low": 100}, {"high": None, "low": None}]
    or_range = build_opening_range(bars, avg_volume=0.0)
    assert or_range is not None
    assert or_range.high == 105
    assert or_range.low == 100
    assert or_range.volume == 0.0
