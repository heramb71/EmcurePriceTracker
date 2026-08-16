"""Circuit breakers that skip the whole day (spec: safety rails).

Pure predicates so they test on synthetic inputs:

* :func:`vix_spike`      — India VIX up more than N% intraday at select time
* :func:`picks_stale`    — daily_picks.json older than the max age
* :func:`daily_loss_limit_breached` — today's realized loss hit N% of capital
  (catches abnormal slippage/gap-through-stop, not a normal planned stop-out;
  self-resolving — skips only the next trading day)
* :func:`drawdown_breaker` — cumulative equity fell N% below its all-time high
  (the real prop-firm-style circuit breaker; sticky, manual resume only)
* :func:`loss_streak_halt` — legacy day-count halt (N consecutive losing days,
  resume next week). Kept for backward compatibility but not wired by default
  — domain research on prop-firm risk practice found no basis for a
  day-count-based cooldown; see ``KittyBotConfig.enable_loss_streak_halt``.

:func:`evaluate` bundles them into a decision the engine journals. Both new
rails are live-mode-only by convention enforced in the engine, not here — paper
mode still computes and journals them (diagnostic signal), it just never
persists a blocking halt.
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


def daily_loss_limit_breached(realized_pnl_today: float, capital: float, max_loss_pct: float) -> bool:
    """True when today's realized loss reached ``max_loss_pct`` % of capital.

    Deliberately set above the per-trade risk cap so it only fires on abnormal
    slippage/gap-through-stop, not a normal planned stop-out. A winning or
    scratch day never blocks.
    """
    if capital <= 0:
        return False
    return realized_pnl_today <= -(capital * max_loss_pct / 100.0)


def drawdown_breaker(equity_high_water_mark: float, current_equity: float, max_drawdown_pct: float) -> bool:
    """True when ``current_equity`` has fallen ``max_drawdown_pct`` % below its
    all-time high — a capital-protection circuit breaker, not a per-day rail.
    """
    if equity_high_water_mark <= 0:
        return False
    dd_pct = (equity_high_water_mark - current_equity) / equity_high_water_mark * 100.0
    return dd_pct >= max_drawdown_pct


def loss_streak_halt(consecutive_losing_days: int, max_days: int) -> bool:
    """True when the losing streak has reached the halt threshold.

    Legacy day-count rail — see module docstring. Kept working and tested, but
    not wired into :func:`evaluate` by default.
    """
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
    drawdown_halted: bool = False,
) -> SafetyDecision:
    """Combine all rails into one skip/trade decision with per-check detail.

    ``halt_until`` covers any self-resolving, dated halt (the daily-loss-limit
    rail, or the legacy day-count rail if opted back in). ``drawdown_halted``
    is the separate sticky circuit breaker with no auto-resume date.
    """
    dated_active = halt_active(halt_until, now.date())
    if drawdown_halted:
        halt_detail = "cumulative drawdown breaker tripped — manual resume required"
    elif dated_active:
        halt_detail = f"halted until {halt_until}"
    else:
        halt_detail = "no active halt"
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
            "Halt",
            dated_active or drawdown_halted,
            halt_detail,
        ),
    )
    return SafetyDecision(skip_day=any(c.blocked for c in checks), checks=checks)
