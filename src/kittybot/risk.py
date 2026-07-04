"""Position sizing, target/stop levels, breakeven, and exit rules (pure).

This is the risk-bearing core, kept free of I/O so it is exhaustively unit-tested:

* :func:`compute_levels`   — target & stop prices from the pick's %s and direction
* :func:`position_size`    — qty capped so (entry−stop)·qty ≤ risk % of capital
* :func:`plan_trade`       — bundle the above into an immutable :class:`TradePlan`
* :func:`breakeven_stop`   — ratchet the stop to entry once price is +1%
* :func:`exit_reason`      — TARGET / STOP / TIME / None for a live price & clock
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime
from typing import Optional

from src.kittybot.opening_range import LONG, SHORT

TARGET = "TARGET"
STOP = "STOP"
TIME = "TIME"


@dataclass(frozen=True)
class TradePlan:
    """A fully-specified, sized intraday trade."""

    symbol: str
    direction: str      # LONG | SHORT
    entry: float
    stop: float
    target: float
    qty: int
    risk_rupees: float  # (entry − stop) · qty, the actual ₹ at risk


def compute_levels(
    entry: float, direction: str, target_pct: float, stop_pct: float
) -> tuple[float, float]:
    """Return ``(target_price, stop_price)`` for a direction and its %s.

    LONG: target above / stop below entry. SHORT: mirrored.
    """
    if direction == LONG:
        target = entry * (1 + target_pct / 100.0)
        stop = entry * (1 - stop_pct / 100.0)
    elif direction == SHORT:
        target = entry * (1 - target_pct / 100.0)
        stop = entry * (1 + stop_pct / 100.0)
    else:  # defensive — callers pass LONG/SHORT
        raise ValueError(f"unknown direction {direction!r}")
    return round(target, 2), round(stop, 2)


def position_size(capital: float, entry: float, stop: float, risk_pct: float) -> int:
    """Largest whole-share qty keeping ₹-at-risk ≤ ``risk_pct`` % of capital.

    Returns 0 when inputs are degenerate (no stop distance, non-positive capital)
    so the caller treats it as "no trade" rather than sizing into a bad fill.
    """
    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0 or capital <= 0 or risk_pct <= 0:
        return 0
    risk_budget = capital * risk_pct / 100.0
    return int(risk_budget // per_share_risk)


def plan_trade(
    symbol: str,
    direction: str,
    entry: float,
    target_pct: float,
    stop_pct: float,
    capital: float,
    risk_pct: float,
) -> Optional[TradePlan]:
    """Build a sized :class:`TradePlan`, or ``None`` if it can't be sized (qty 0)."""
    target, stop = compute_levels(entry, direction, target_pct, stop_pct)
    qty = position_size(capital, entry, stop, risk_pct)
    if qty <= 0:
        return None
    return TradePlan(
        symbol=symbol,
        direction=direction,
        entry=round(entry, 2),
        stop=stop,
        target=target,
        qty=qty,
        risk_rupees=round(abs(entry - stop) * qty, 2),
    )


def breakeven_stop(
    entry: float, direction: str, current_price: float, current_stop: float, trigger_pct: float
) -> float:
    """Ratchet the stop to breakeven (entry) once price is +``trigger_pct``% onside.

    Only ever moves the stop in the protective direction — never loosens it. For a
    LONG the stop can only rise; for a SHORT it can only fall.
    """
    if direction == LONG:
        if current_price >= entry * (1 + trigger_pct / 100.0):
            return max(current_stop, round(entry, 2))
        return current_stop
    if direction == SHORT:
        if current_price <= entry * (1 - trigger_pct / 100.0):
            return min(current_stop, round(entry, 2))
        return current_stop
    raise ValueError(f"unknown direction {direction!r}")


def exit_reason(
    plan: TradePlan, price: float, now: dtime, hard_exit: dtime, stop: float | None = None
) -> Optional[str]:
    """Why to exit now: TARGET, STOP, TIME, or ``None`` to hold.

    ``stop`` overrides ``plan.stop`` so a ratcheted breakeven stop is honoured.
    The hard-time exit takes precedence — the spec mandates a flat book by 15:10
    regardless of P&L.
    """
    if now >= hard_exit:
        return TIME
    effective_stop = plan.stop if stop is None else stop
    if plan.direction == LONG:
        if price >= plan.target:
            return TARGET
        if price <= effective_stop:
            return STOP
    else:  # SHORT
        if price <= plan.target:
            return TARGET
        if price >= effective_stop:
            return STOP
    return None


def realized_pnl(plan: TradePlan, exit_price: float) -> float:
    """Gross ₹ P&L for closing ``plan`` at ``exit_price`` (LONG or SHORT)."""
    if plan.direction == LONG:
        return round((exit_price - plan.entry) * plan.qty, 2)
    return round((plan.entry - exit_price) * plan.qty, 2)
