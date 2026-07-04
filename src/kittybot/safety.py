"""Circuit breakers that skip the whole day (spec: safety rails).

Three independent guards, each a pure predicate so they test on synthetic inputs:

* :func:`vix_spike`      — India VIX up more than N% intraday at select time
* :func:`picks_stale`    — daily_picks.json older than the max age
* :func:`loss_streak_halt` — halt after N consecutive losing days, resume next week

:func:`evaluate` bundles them into a decision the engine journals. The loss-streak
"resume next week" boundary is computed by :func:`resume_date` (the Monday after
the halt) so the stateful part stays a one-line date comparison in the engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional


@dataclass(frozen=True)
class SafetyCheck:
    name: str
    blocked: bool
    detail: str


@dataclass(frozen=True)
class SafetyDecision:
    skip_day: bool
    checks: tuple[SafetyCheck, ...]

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if c.blocked]


def vix_spike(vix_now: Optional[float], vix_prev_close: Optional[float], max_pct: float) -> bool:
    """True when India VIX is up more than ``max_pct`` % vs its previous close.

    Missing data (either value ``None`` or non-positive) does not block — the bot
    treats an unavailable VIX as "no spike detected" rather than skipping blindly.
    """
    if not vix_now or not vix_prev_close or vix_prev_close <= 0:
        return False
    change_pct = (vix_now - vix_prev_close) / vix_prev_close * 100.0
    return change_pct > max_pct


def picks_stale(generated_at: Optional[datetime], now: datetime, max_age_hours: float) -> bool:
    """True when the kitty is missing a timestamp or older than ``max_age_hours``."""
    if generated_at is None:
        return True
    # Normalise tz-awareness so naive/aware timestamps compare cleanly.
    if generated_at.tzinfo is not None and now.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=None)
    elif generated_at.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return (now - generated_at) > timedelta(hours=max_age_hours)


def loss_streak_halt(consecutive_losing_days: int, max_days: int) -> bool:
    """True when the losing streak has reached the halt threshold."""
    return consecutive_losing_days >= max_days


def resume_date(halt_date: date) -> date:
    """The Monday of the week after ``halt_date`` — when trading may resume.

    "Resume next week" = the start of the following calendar week, so a halt on
    any weekday reopens the next Monday.
    """
    days_until_next_monday = 7 - halt_date.weekday()
    return halt_date + timedelta(days=days_until_next_monday)


def halt_active(halt_until: Optional[date], today: date) -> bool:
    """True when a loss-streak halt is still in force for ``today``."""
    return halt_until is not None and today < halt_until


def evaluate(
    *,
    vix_now: Optional[float],
    vix_prev_close: Optional[float],
    vix_spike_pct: float,
    generated_at: Optional[datetime],
    now: datetime,
    picks_max_age_hours: float,
    halt_until: Optional[date],
) -> SafetyDecision:
    """Combine all rails into one skip/trade decision with per-check detail."""
    checks = (
        SafetyCheck(
            "India VIX spike",
            vix_spike(vix_now, vix_prev_close, vix_spike_pct),
            (f"{vix_now} vs prev {vix_prev_close} (> {vix_spike_pct}% blocks)"
             if vix_now and vix_prev_close else "VIX unavailable — not blocking"),
        ),
        SafetyCheck(
            "Picks freshness",
            picks_stale(generated_at, now, picks_max_age_hours),
            (f"generated_at={generated_at.isoformat() if generated_at else 'missing'}, "
             f"max age {picks_max_age_hours}h"),
        ),
        SafetyCheck(
            "Loss-streak halt",
            halt_active(halt_until, now.date()),
            (f"halted until {halt_until}" if halt_until else "no active halt"),
        ),
    )
    return SafetyDecision(skip_day=any(c.blocked for c in checks), checks=checks)
