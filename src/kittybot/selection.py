"""Select the single trade for the day (spec step 3).

From the set of fired opening-range breakouts, take the one with the strongest
signal. Ties break deterministically by symbol so the same inputs always yield
the same choice (important for reproducible tests and journals).
"""
from __future__ import annotations

from src.kittybot.opening_range import Trigger


def select_trigger(triggers: list[Trigger] | tuple[Trigger, ...]) -> Trigger | None:
    """Return the strongest trigger, or ``None`` when nothing fired.

    Ranking key: highest ``strength`` first; on an exact tie, the alphabetically
    first symbol wins (stable, order-independent).
    """
    if not triggers:
        return None
    return sorted(triggers, key=lambda t: (-t.strength, t.symbol))[0]
