"""Crash-safe persistence for KittyBot's cross-day state.

One small JSON file (default ``kittybot_state.json``, gitignored) holds:

* ``position``       — the open :class:`~src.kittybot.risk.TradePlan` (+ratcheted
                       stop, entry order id, session date), or ``None``
* ``session_date``   — the date the current position/decisions belong to
* ``loss_streak``    — consecutive losing days (legacy day-count halt input)
* ``last_result_date`` — the last date a result was recorded (streak de-dup)
* ``halt_until``     — ISO date a dated halt (daily-loss-limit or legacy
                       loss-streak) lifts, or ``None``
* ``realized_pnl_today`` — today's realized P&L, resets on date rollover
* ``pnl_day``        — ISO date ``realized_pnl_today`` belongs to
* ``cumulative_realized_pnl`` — all-time realized P&L since inception
* ``equity_high_water_mark`` — the highest ``capital + cumulative_realized_pnl``
                       ever observed, input to the drawdown breaker
* ``drawdown_halted`` — sticky circuit breaker, ``True`` until manually cleared

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
    "realized_pnl_today": 0.0,
    "pnl_day": None,
    "cumulative_realized_pnl": 0.0,
    "equity_high_water_mark": 0.0,
    "drawdown_halted": False,
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


def record_exit_attempt(path: str) -> int:
    """Increment and return the open position's failed-exit-attempt counter.

    Backs the engine's alert backoff — retried every tick but only re-alerted
    every Nth attempt — so a persistently rejecting broker can't spam Telegram.
    """
    with transaction(path) as state:
        pos = state.get("position")
        if not pos:
            return 0
        pos["exit_fail_count"] = pos.get("exit_fail_count", 0) + 1
        return pos["exit_fail_count"]


def mark_carryover_alerted(path: str, today: date) -> bool:
    """Record that today's stuck-position carryover alert has been sent.

    Returns ``True`` the first time this is called for ``today`` (so the
    caller should alert), ``False`` on any later call the same day.
    """
    with transaction(path) as state:
        pos = state.get("position")
        if not pos:
            return False
        if pos.get("carryover_alert_date") == today.isoformat():
            return False
        pos["carryover_alert_date"] = today.isoformat()
        return True


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


def record_realized_pnl(path: str, *, pnl: float, today: date, capital: float) -> tuple[float, float]:
    """Fold a closed trade's P&L into today's tally and the all-time equity
    high-water mark, resetting the daily tally on a date rollover.

    Returns ``(realized_pnl_today, equity_high_water_mark)`` — the inputs the
    daily-loss-limit and drawdown-breaker safety rails need.
    """
    with transaction(path) as state:
        if state.get("pnl_day") != today.isoformat():
            state["realized_pnl_today"] = 0.0
            state["pnl_day"] = today.isoformat()
        state["realized_pnl_today"] = round(state.get("realized_pnl_today", 0.0) + pnl, 2)
        state["cumulative_realized_pnl"] = round(state.get("cumulative_realized_pnl", 0.0) + pnl, 2)
        equity = capital + state["cumulative_realized_pnl"]
        # The HWM can never be below `capital` itself — that's day-one equity,
        # before any trade, and always a valid "high" to measure drawdown from.
        state["equity_high_water_mark"] = max(state.get("equity_high_water_mark", 0.0), capital, equity)
        return state["realized_pnl_today"], state["equity_high_water_mark"]


def set_drawdown_halt(path: str, halted: bool) -> None:
    """Set (or clear) the sticky drawdown circuit breaker — manual resume only,
    no auto-resume date."""
    with transaction(path) as state:
        state["drawdown_halted"] = halted


def is_drawdown_halted(state: dict) -> bool:
    """True when the drawdown circuit breaker is tripped, from a loaded state dict."""
    return bool(state.get("drawdown_halted", False))
