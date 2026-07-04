"""Opening-range construction and breakout detection (spec steps 2–3).

The first ``opening_range_minutes`` (default 15) of the session define each
survivor's high/low. After that window a symbol *triggers* when price breaks the
range high (LONG) or low (SHORT) on above-average volume. Shorts are only allowed
when the pick has at least as much downside room as upside (``short_room_2pct >=
long_room_2pct``) — otherwise the short is skipped, per spec.

Everything here is pure: build the range from bars, then evaluate a live price +
volume against it. The engine supplies the bars/price/volume from the data layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from src.kittybot.picks import Pick

LONG = "LONG"
SHORT = "SHORT"

_EPS = 1e-9


@dataclass(frozen=True)
class OpeningRange:
    """The first-N-minute range for one symbol.

    ``volume`` is the total traded volume inside the opening window; ``avg_volume``
    is the baseline it is compared against (a same-length window's typical volume,
    e.g. avg 15-min volume over recent sessions) to judge "above-average".
    """

    high: float
    low: float
    volume: float
    avg_volume: float

    @property
    def width(self) -> float:
        return max(self.high - self.low, 0.0)


@dataclass(frozen=True)
class Trigger:
    """A fired opening-range breakout for one symbol."""

    symbol: str
    direction: str        # LONG | SHORT
    trigger_price: float  # price at which the breakout was detected
    strength: float       # ranking score — larger = stronger signal


def build_opening_range(bars: Sequence[Mapping[str, float]], avg_volume: float) -> OpeningRange | None:
    """Build the opening range from the opening-window bars.

    ``bars`` are OHLCV mappings (e.g. the 1-minute bars of the first 15 minutes).
    Returns ``None`` if there are no bars, so the caller can defer selection until
    the window has actually printed.
    """
    highs = [float(b["high"]) for b in bars if b.get("high") is not None]
    lows = [float(b["low"]) for b in bars if b.get("low") is not None]
    if not highs or not lows:
        return None
    volume = sum(float(b.get("volume", 0.0) or 0.0) for b in bars)
    return OpeningRange(high=max(highs), low=min(lows), volume=volume, avg_volume=avg_volume)


def _volume_ok(or_range: OpeningRange, breakout_volume: float, multiple: float) -> bool:
    """Above-average-volume check for the breakout bar/window."""
    baseline = or_range.avg_volume
    if baseline <= _EPS:
        return True  # no baseline available → don't block on volume
    return breakout_volume >= multiple * baseline


def _strength(distance_pct: float, atr14_pct: float, volume: float, baseline: float) -> float:
    """Rank score: breakout depth (ATR-normalised) scaled by the volume surge.

    Distance beyond the range is normalised by the stock's own ATR% so a 0.5%
    poke on a quiet name doesn't outrank a 0.5% poke on a volatile one, and it is
    multiplied by the volume ratio so conviction (volume) breaks ties.
    """
    atr = max(atr14_pct, 0.1)  # floor so a missing/zero ATR doesn't blow up
    vol_ratio = (volume / baseline) if baseline > _EPS else 1.0
    return (distance_pct / atr) * max(vol_ratio, 1.0)


def breakout_trigger(
    pick: Pick,
    or_range: OpeningRange,
    price: float,
    breakout_volume: float,
    volume_multiple: float,
) -> Trigger | None:
    """Return a :class:`Trigger` if ``pick`` breaks its opening range, else ``None``.

    LONG when price > range high; SHORT when price < range low AND the pick has
    ``short_room_2pct >= long_room_2pct`` (else shorts are skipped). Both require
    above-average volume.
    """
    if not _volume_ok(or_range, breakout_volume, volume_multiple):
        return None

    if price > or_range.high + _EPS:
        distance_pct = (price - or_range.high) / or_range.high * 100.0
        return Trigger(
            symbol=pick.symbol,
            direction=LONG,
            trigger_price=price,
            strength=_strength(distance_pct, pick.atr14_pct, breakout_volume, or_range.avg_volume),
        )

    if price < or_range.low - _EPS:
        if pick.short_room_2pct < pick.long_room_2pct:
            return None  # skip shorts without enough downside room
        distance_pct = (or_range.low - price) / or_range.low * 100.0
        return Trigger(
            symbol=pick.symbol,
            direction=SHORT,
            trigger_price=price,
            strength=_strength(distance_pct, pick.atr14_pct, breakout_volume, or_range.avg_volume),
        )

    return None
