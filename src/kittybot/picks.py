"""Load and parse the daily kitty of pre-ranked candidates.

The screener (a separate pre-market job) writes ``daily_picks.json``::

    {
      "generated_at": "2026-07-04T08:45:00+05:30",
      "picks": [
        {"symbol": "TATAMOTORS", "score": 91.2, "atr14_pct": 2.8,
         "avg_range_60d_pct": 3.1, "hit_rate_2pct": 0.62, "long_room_2pct": 3.4,
         "short_room_2pct": 1.1, "suggested_target_pct": 3.0,
         "suggested_stop_pct": 1.5, "prev_close": 985.4, "earnings_today": false},
        ...
      ]
    }

If the file is missing or unreadable the bot falls back to a bare universe (from
config) with default target/stop percentages and no scores — parsing here never
raises, so a bad file degrades to the fallback rather than crashing the engine.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Optional

from src.kittybot.config import KittyBotConfig
from src.shared.atomic_json import read_json

logger = logging.getLogger(__name__)

# Spec: target must land in 2–5%; stop is half the target (2:1 reward:risk).
_MIN_TARGET_PCT = 2.0
_MAX_TARGET_PCT = 5.0


@dataclass(frozen=True)
class Pick:
    """One ranked candidate from the daily kitty."""

    symbol: str
    score: float = 0.0
    atr14_pct: float = 0.0
    avg_range_60d_pct: float = 0.0
    hit_rate_2pct: float = 0.0
    long_room_2pct: float = 0.0
    short_room_2pct: float = 0.0
    suggested_target_pct: float = 3.0
    suggested_stop_pct: float = 1.5
    prev_close: Optional[float] = None
    earnings_today: bool = False
    earnings_date: Optional[str] = None  # "YYYY-MM-DD", alternative to the bool flag


@dataclass(frozen=True)
class DailyKitty:
    """The parsed kitty for a session."""

    generated_at: Optional[datetime]
    picks: tuple[Pick, ...]
    source: str  # "json" | "fallback"


def _f(raw: dict, key: str, default: float) -> float:
    try:
        val = raw.get(key, default)
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _clamp_levels(target_pct: float, stop_pct: float, ratio: float) -> tuple[float, float]:
    """Clamp the target into [2, 5]% and derive/repair the stop to keep 2:1."""
    target = min(max(target_pct, _MIN_TARGET_PCT), _MAX_TARGET_PCT)
    # Trust the screener's stop when present and sane; otherwise derive from ratio.
    if stop_pct <= 0:
        stop_pct = target / ratio if ratio > 0 else target / 2
    return round(target, 4), round(stop_pct, 4)


def parse_pick(raw: dict[str, Any], ratio: float) -> Optional[Pick]:
    """Parse one pick dict. Returns ``None`` when it lacks a usable symbol."""
    symbol = str(raw.get("symbol", "")).strip().upper()
    if not symbol:
        return None
    target, stop = _clamp_levels(
        _f(raw, "suggested_target_pct", 3.0),
        _f(raw, "suggested_stop_pct", 0.0),
        ratio,
    )
    prev_close_raw = raw.get("prev_close")
    return Pick(
        symbol=symbol,
        score=_f(raw, "score", 0.0),
        atr14_pct=_f(raw, "atr14_pct", 0.0),
        avg_range_60d_pct=_f(raw, "avg_range_60d_pct", 0.0),
        hit_rate_2pct=_f(raw, "hit_rate_2pct", 0.0),
        long_room_2pct=_f(raw, "long_room_2pct", 0.0),
        short_room_2pct=_f(raw, "short_room_2pct", 0.0),
        suggested_target_pct=target,
        suggested_stop_pct=stop,
        prev_close=float(prev_close_raw) if prev_close_raw not in (None, "") else None,
        earnings_today=bool(raw.get("earnings_today", False)),
        earnings_date=(str(raw["earnings_date"]) if raw.get("earnings_date") else None),
    )


def _parse_generated_at(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning("kitty: unparseable generated_at %r", raw)
        return None


def parse_kitty(raw: dict[str, Any], cfg: KittyBotConfig) -> DailyKitty:
    """Parse a raw JSON object into a :class:`DailyKitty` (pure, never raises)."""
    picks: list[Pick] = []
    for item in raw.get("picks", []) or []:
        if isinstance(item, dict):
            pick = parse_pick(item, cfg.reward_risk_ratio)
            if pick is not None:
                picks.append(pick)
    picks = _dedupe(picks)[: cfg.max_picks]
    return DailyKitty(
        generated_at=_parse_generated_at(raw.get("generated_at")),
        picks=tuple(picks),
        source="json",
    )


def _dedupe(picks: list[Pick]) -> list[Pick]:
    """Keep the first occurrence of each symbol, preserving order."""
    seen: set[str] = set()
    out: list[Pick] = []
    for p in picks:
        if p.symbol not in seen:
            seen.add(p.symbol)
            out.append(p)
    return out


def fallback_kitty(cfg: KittyBotConfig) -> DailyKitty:
    """A scoreless kitty from the configured universe (default target/stop)."""
    target, stop = _clamp_levels(3.0, 0.0, cfg.reward_risk_ratio)
    picks = tuple(
        Pick(symbol=sym, suggested_target_pct=target, suggested_stop_pct=stop)
        for sym in cfg.fallback_universe[: cfg.max_picks]
    )
    return DailyKitty(generated_at=None, picks=picks, source="fallback")


def load_kitty(cfg: KittyBotConfig) -> DailyKitty:
    """Load the kitty from ``cfg.picks_path``; fall back to the universe on any miss."""
    raw = read_json(cfg.picks_path, None)
    if not isinstance(raw, dict):
        logger.warning("kitty: %s missing/invalid — using fallback universe", cfg.picks_path)
        return fallback_kitty(cfg)
    kitty = parse_kitty(raw, cfg)
    if not kitty.picks:
        logger.warning("kitty: %s had no usable picks — using fallback", cfg.picks_path)
        return fallback_kitty(cfg)
    return kitty


def with_earnings_date_today(pick: Pick, today: date) -> Pick:
    """Return a copy with ``earnings_today`` set if ``earnings_date`` is today.

    Lets a screener that only emits a date (not a bool) still be filtered.
    """
    if pick.earnings_date and pick.earnings_date == today.isoformat():
        return replace(pick, earnings_today=True)
    return pick
