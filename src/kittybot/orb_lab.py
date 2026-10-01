"""Opening-range timing lab — replay KittyBot's day on historical intraday bars (pure).

Answers one question: does waiting longer before committing (a 30-minute opening
range, or a 15-minute range that must still hold at 09:45) beat the live
15-minute breakout once costs are paid? Every decision reuses the production
functions — screener ranking, gap filter, ``breakout_trigger``, ``select_trigger``,
``plan_trade``, ``breakeven_stop`` — so a variant differs from live ONLY in when
the range closes and when the first entry is allowed.

Bars are intraday OHLCV frames with a ``date`` column holding the bar's START
time in IST wall-clock. A decision is taken on a bar's CLOSE (start + interval),
which is what a poller would see. Known approximations (all shared by every
variant, so the comparison stays fair):

* 5-minute bars → a breakout is detected up to one bar late vs live 1-minute ticks.
* Target/stop fill at the level when the bar's high/low crosses it; when one bar
  spans both, the STOP is assumed first (conservative).
* No VIX rail and no earnings filter — no historical feed for either.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Mapping, Optional, Sequence

import pandas as pd

from src.kittybot.config import KittyBotConfig
from src.kittybot.filters import OpenQuote, gap_too_large
from src.kittybot.opening_range import (
    LONG,
    OpeningRange,
    Trigger,
    breakout_trigger,
    build_opening_range,
)
from src.kittybot.picks import Pick, parse_pick
from src.kittybot.risk import (
    STOP,
    TARGET,
    TIME,
    TradePlan,
    breakeven_stop,
    plan_trade,
    realized_pnl,
)
from src.kittybot.screener import rank, screen_symbol
from src.shared.costs import intraday_charges

MARKET_OPEN = dtime(9, 15)
EOD = "EOD"  # the day's bars ran out before 15:10 — exit at the last close


@dataclass(frozen=True)
class OrbVariant:
    """One timing rule. ``confirm_at`` is the earliest bar-CLOSE time an entry may
    be taken (``None`` → the first bar after the opening range, i.e. live)."""

    name: str
    or_minutes: int
    confirm_at: Optional[dtime] = None


VARIANTS = (
    OrbVariant("LIVE  15-min OR", 15),
    OrbVariant("30-min OR", 30),
    OrbVariant("15-min OR + hold @09:45", 15, dtime(9, 45)),
)


@dataclass(frozen=True)
class Candidate:
    """One kitty pick's inputs for a single session."""

    pick: Pick
    prev_close: float
    per_min_volume: float  # live baseline: 20-day avg daily volume / 375
    bars: pd.DataFrame     # that session's intraday bars, sorted by start time


def _plus(t: dtime, minutes: int) -> dtime:
    return (datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=minutes)).time()


def close_time(start: pd.Timestamp, interval_min: int) -> dtime:
    """Wall-clock time a bar that started at ``start`` closes."""
    return (start + pd.Timedelta(minutes=interval_min)).time()


def build_kitty(prior_daily: Mapping[str, pd.DataFrame], cfg: KittyBotConfig) -> list[Pick]:
    """The pre-market kitty exactly as the screener would have written it, using
    only daily bars that closed BEFORE the session (no look-ahead)."""
    metrics = [m for sym, df in prior_daily.items() if (m := screen_symbol(sym, df, cfg))]
    ratio = cfg.reward_risk_ratio
    picks = (parse_pick(m.to_pick(), ratio) for m in rank(metrics, cfg.max_picks))
    return [p for p in picks if p is not None]


def _opening_range(c: Candidate, or_minutes: int) -> Optional[OpeningRange]:
    t = c.bars["date"].dt.time
    window = c.bars[(t >= MARKET_OPEN) & (t < _plus(MARKET_OPEN, or_minutes))]
    rows = [{"high": r.high, "low": r.low, "volume": r.volume} for r in window.itertuples()]
    return build_opening_range(rows, avg_volume=c.per_min_volume)


def _entry_bars(c: Candidate, v: OrbVariant, cfg: KittyBotConfig, interval_min: int) -> pd.DataFrame:
    """Bars on whose close an entry may be taken under variant ``v``."""
    starts = c.bars["date"].dt.time
    closes = c.bars["date"].map(lambda ts: close_time(ts, interval_min))
    ok = (starts >= _plus(MARKET_OPEN, v.or_minutes)) & (closes < cfg.no_trade_after_t)
    ok &= closes >= cfg.select_time_t
    if v.confirm_at is not None:
        ok &= closes >= v.confirm_at
    return c.bars[ok]


def find_entry(cands: Sequence[Candidate], v: OrbVariant, cfg: KittyBotConfig,
               interval_min: int) -> Optional[tuple[Trigger, Candidate, pd.Timestamp]]:
    """Walk bar closes in time order; at the first close where any candidate
    breaks out, take the strongest trigger — the live single-trade rule."""
    by_ts: dict[pd.Timestamp, list[tuple[Candidate, OpeningRange, object]]] = {}
    for c in cands:
        or_range = _opening_range(c, v.or_minutes)
        if or_range is None:
            continue
        for bar in _entry_bars(c, v, cfg, interval_min).itertuples():
            by_ts.setdefault(bar.date, []).append((c, or_range, bar))
    for ts in sorted(by_ts):
        fired = []
        for c, or_range, bar in by_ts[ts]:
            trig = breakout_trigger(c.pick, or_range, float(bar.close),
                                    float(bar.volume) / interval_min, cfg.breakout_volume_multiple)
            if trig is not None:
                fired.append((trig, c))
        if fired:
            trig, c = max(fired, key=lambda f: f[0].strength)
            return trig, c, ts
    return None


def walk_exit(bars_after: pd.DataFrame, plan: TradePlan, cfg: KittyBotConfig,
              interval_min: int) -> tuple[str, float]:
    """Manage ``plan`` bar by bar: stop (checked first), target, breakeven
    ratchet, then the 15:10 hard exit at the bar close. Returns (reason, price)."""
    stop = plan.stop
    is_long = plan.direction == LONG
    last_close = plan.entry
    for bar in bars_after.itertuples():
        high, low, last_close = float(bar.high), float(bar.low), float(bar.close)
        if (low <= stop) if is_long else (high >= stop):
            return STOP, stop
        if (high >= plan.target) if is_long else (low <= plan.target):
            return TARGET, plan.target
        best = high if is_long else low
        stop = breakeven_stop(plan.entry, plan.direction, best, stop, cfg.breakeven_trigger_pct)
        if close_time(bar.date, interval_min) >= cfg.hard_exit_t:
            return TIME, last_close
    return EOD, last_close


def _gap_ok(c: Candidate, cfg: KittyBotConfig) -> bool:
    if c.bars.empty or c.prev_close <= 0:
        return False
    quote = OpenQuote(open=float(c.bars["open"].iloc[0]), prev_close=c.prev_close)
    return not gap_too_large(quote, cfg.gap_max_pct)


def simulate_day(cands: Sequence[Candidate], v: OrbVariant, cfg: KittyBotConfig,
                 interval_min: int) -> Optional[dict]:
    """One session under variant ``v`` → a closed-trade dict, or ``None`` (no trade)."""
    survivors = [c for c in cands if _gap_ok(c, cfg)]
    found = find_entry(survivors, v, cfg, interval_min)
    if found is None:
        return None
    trig, c, ts = found
    plan = plan_trade(trig.symbol, trig.direction, trig.trigger_price,
                      c.pick.suggested_target_pct, c.pick.suggested_stop_pct,
                      cfg.capital, cfg.risk_per_trade_pct)
    if plan is None:
        return None
    reason, exit_px = walk_exit(c.bars[c.bars["date"] > ts], plan, cfg, interval_min)
    gross = realized_pnl(plan, exit_px)
    buy, sell = (plan.entry, exit_px) if plan.direction == LONG else (exit_px, plan.entry)
    charges = intraday_charges(buy, sell, plan.qty)
    return {
        "day": ts.date(), "symbol": plan.symbol, "direction": plan.direction,
        "entry_time": close_time(ts, interval_min), "entry": plan.entry, "exit": exit_px,
        "qty": plan.qty, "reason": reason, "gross": gross, "charges": charges,
        "net": round(gross - charges, 2),
    }
