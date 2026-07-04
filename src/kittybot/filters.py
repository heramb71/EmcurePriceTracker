"""Pre-trade discards (spec step 1): earnings-today and opening-gap filters.

Pure functions over :class:`~src.kittybot.picks.Pick` plus an ``open`` quote, so
they unit-test directly on synthetic data. The engine calls :func:`apply_filters`
once, at ~09:15 after the open prints, and journals every discard with its reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.kittybot.picks import Pick, with_earnings_date_today


@dataclass(frozen=True)
class OpenQuote:
    """The opening print for a symbol and its previous close."""

    open: float
    prev_close: float


def is_earnings_today(pick: Pick, today: date) -> bool:
    """True when the pick has a results/earnings announcement today."""
    return with_earnings_date_today(pick, today).earnings_today


def gap_pct(quote: OpenQuote) -> float:
    """Signed opening gap vs previous close, in percent (0 if prev_close ≤ 0)."""
    if quote.prev_close <= 0:
        return 0.0
    return (quote.open - quote.prev_close) / quote.prev_close * 100.0


def gap_too_large(quote: OpenQuote, max_pct: float) -> bool:
    """True when the absolute opening gap exceeds ``max_pct``."""
    return abs(gap_pct(quote)) > max_pct


def discard_reason(
    pick: Pick, quote: OpenQuote | None, today: date, max_gap_pct: float
) -> str | None:
    """Return why a pick is discarded, or ``None`` if it survives.

    A pick with no opening quote (data unavailable) is discarded — the bot cannot
    evaluate its gap, and trading blind violates the safety-first posture.
    """
    if is_earnings_today(pick, today):
        return "earnings/results announcement today"
    if quote is None:
        return "no opening quote available"
    if gap_too_large(quote, max_gap_pct):
        return f"opening gap {gap_pct(quote):+.2f}% exceeds ±{max_gap_pct:.1f}%"
    return None


def apply_filters(
    picks: tuple[Pick, ...] | list[Pick],
    quotes: dict[str, OpenQuote],
    today: date,
    max_gap_pct: float,
) -> tuple[list[Pick], list[tuple[Pick, str]]]:
    """Split picks into ``(kept, discarded)`` where discarded carries the reason."""
    kept: list[Pick] = []
    discarded: list[tuple[Pick, str]] = []
    for pick in picks:
        reason = discard_reason(pick, quotes.get(pick.symbol), today, max_gap_pct)
        if reason is None:
            kept.append(pick)
        else:
            discarded.append((pick, reason))
    return kept, discarded
