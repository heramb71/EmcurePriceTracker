"""Crash-safe persistence for KittyBot's cross-day state.

One small JSON file (default ``kittybot_state.json``, gitignored) holds:

* ``position``       — the open :class:`~src.kittybot.risk.TradePlan` (+ratcheted
                       stop, entry order id, session date), or ``None``
* ``session_date``   — the date the current position/decisions belong to
* ``loss_streak``    — consecutive losing days
* ``last_result_date`` — the last date a result was recorded (streak de-dup)
* ``halt_until``     — ISO date the loss-streak halt lifts, or ``None``

All writes go through :mod:`src.shared.atomic_json` (temp+fsync+replace, plus a
flock) so a mid-write crash can never truncate the file and strand a live
position. Read-modify-write happens inside :func:`transaction`.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from typing import Iterator, Optional

from src.kittybot.risk import TradePlan
from src.shared.atomic_json import locked, read_json, write_json

logger = logging.getLogger(__name__)

_EMPTY: dict = {
    "position": None,
    "session_date": None,
    "loss_streak": 0,
    "last_result_date": None,
    "halt_until": None,
}


def load(path: str) -> dict:
    """Return the persisted state, or a fresh empty skeleton."""
    state = read_json(path, None)
    if not isinstance(state, dict):
        return dict(_EMPTY)
    return {**_EMPTY, **state}


@contextmanager
def transaction(path: str) -> Iterator[dict]:
    """Lock the state file, yield its contents, persist the mutations on exit."""
    with locked(path):
        state = load(path)
        yield state
        write_json(path, state)


def get_position(path: str) -> Optional[dict]:
    """The open position dict (plan + live fields), or ``None``."""
    return load(path).get("position")


def open_position(path: str, plan: TradePlan, *, session_date: date, entry_order_id: str | None,
                  fill_price: float) -> None:
    """Record a freshly-filled entry as the day's open position."""
    with transaction(path) as state:
        state["position"] = {
            **asdict(plan),
            "entry_fill": round(fill_price, 2),
            "live_stop": plan.stop,
            "entry_order_id": entry_order_id,
            "session_date": session_date.isoformat(),
        }
        state["session_date"] = session_date.isoformat()


def update_stop(path: str, new_stop: float) -> None:
    """Persist a ratcheted (breakeven) stop on the open position."""
    with transaction(path) as state:
        if state.get("position"):
            state["position"]["live_stop"] = round(new_stop, 2)


def close_position(path: str, *, result_date: date, is_loss: bool) -> None:
    """Clear the open position and fold the outcome into the loss streak.

    A loss increments the streak; a win/scratch resets it to zero. Guarded by
    ``last_result_date`` so recording twice for the same day can't double-count.
    """
    with transaction(path) as state:
        state["position"] = None
        if state.get("last_result_date") == result_date.isoformat():
            return  # already counted this day's result
        state["loss_streak"] = state.get("loss_streak", 0) + 1 if is_loss else 0
        state["last_result_date"] = result_date.isoformat()


def set_halt(path: str, halt_until: Optional[date]) -> None:
    """Set (or clear) the loss-streak halt date."""
    with transaction(path) as state:
        state["halt_until"] = halt_until.isoformat() if halt_until else None


def parse_halt_until(state: dict) -> Optional[date]:
    """Parse ``halt_until`` from a loaded state dict into a ``date`` (or ``None``)."""
    raw = state.get("halt_until")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning("state: bad halt_until %r", raw)
        return None
