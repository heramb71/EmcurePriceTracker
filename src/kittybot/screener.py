"""Pre-market screener: rank the kitty universe into the day's top-N candidates.

Pure metric computation over daily OHLCV frames (from ``src.shared.data``) so it
unit-tests on synthetic data. ``apps.kitty_screener`` fetches the bars and writes
``daily_picks.json``; everything measurable lives here:

* :func:`directional_hit_rate` — % of recent sessions a 2% move was available
* :func:`avg_range_pct`, :func:`atr_pct`, :func:`adtv_cr` — volatility / liquidity
* :func:`market_adjusted_hit_rate` — hit-rate with Nifty's own move subtracted
  first and recent days weighted more than old ones (candidate replacement for
  ``either_hit_rate`` — see ``apps/kitty_score_backtest.py``, not yet wired into
  :func:`screen_symbol`/production until backtested)
* :func:`screen_symbol` — one symbol's :class:`ScreenMetrics` (or ``None``)
* :func:`rank` / :func:`build_payload` — top-N selection + the JSON envelope

The score blends a stock's realistic ability to reach a 2% intraday target
(hit-rate, dominant) with its typical daily range (so there is room for a 2–5%
target at all). Illiquid or too-quiet names drop out via the ADTV gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from src.kittybot.config import KittyBotConfig
from src.shared.indicators import compute_atr

# A 2% intraday move is the smallest target the bot trades — the room/hit-rate
# stats are measured against it so they line up with suggested_target_pct's floor.
_TARGET_FLOOR_PCT = 2.0
_MIN_ROWS = 20            # need at least this many daily bars to trust the stats
_ADTV_LOOKBACK = 20
_MIN_TARGET_PCT = 2.0
_MAX_TARGET_PCT = 5.0
_TARGET_RANGE_FRACTION = 0.6  # aim for ~60% of the typical daily range as the target
_RECENCY_HALFLIFE_DAYS = 20.0  # a day 20 sessions ago counts half as much as today


@dataclass(frozen=True)
class ScreenMetrics:
    """One symbol's screening snapshot — serialises straight into a pick dict."""

    symbol: str
    score: float
    atr14_pct: float
    avg_range_60d_pct: float
    hit_rate_2pct: float
    long_room_2pct: float
    short_room_2pct: float
    suggested_target_pct: float
    suggested_stop_pct: float
    prev_close: float
    adtv_cr: float

    def to_pick(self) -> dict:
        """The daily_picks.json pick object (drops the internal adtv_cr helper)."""
        d = asdict(self)
        d.pop("adtv_cr", None)
        d["earnings_today"] = False  # screener has no earnings feed; bot filters if set
        return d


def _tail(df: pd.DataFrame, lookback: int) -> pd.DataFrame:
    return df.tail(min(lookback, len(df)))


def directional_hit_rate(df: pd.DataFrame, side: str, lookback: int,
                         threshold_pct: float = _TARGET_FLOOR_PCT) -> float:
    """% of the last ``lookback`` sessions where a ``threshold_pct`` move from the
    open was available in ``side`` ("long" = high above open, "short" = low below).
    """
    tail = _tail(df, lookback)
    if tail.empty:
        return 0.0
    if side == "long":
        moved = (tail["high"] - tail["open"]) / tail["open"] * 100.0
    else:
        moved = (tail["open"] - tail["low"]) / tail["open"] * 100.0
    return round(float((moved >= threshold_pct).mean()) * 100.0, 1)


def either_hit_rate(df: pd.DataFrame, lookback: int,
                    threshold_pct: float = _TARGET_FLOOR_PCT) -> float:
    """% of sessions a ``threshold_pct`` move was available in *either* direction."""
    tail = _tail(df, lookback)
    if tail.empty:
        return 0.0
    up = (tail["high"] - tail["open"]) / tail["open"] * 100.0 >= threshold_pct
    down = (tail["open"] - tail["low"]) / tail["open"] * 100.0 >= threshold_pct
    return round(float((up | down).mean()) * 100.0, 1)


def market_adjusted_move(stock_daily: pd.DataFrame, nifty_daily: pd.DataFrame,
                         side: str) -> pd.Series:
    """Per-day % move with Nifty's same-day move subtracted, oldest-first.

    ``side="long"`` measures high-vs-open; ``side="short"`` measures open-vs-low.
    Aligns the two frames on ``date`` first, so only days both cover are counted.
    Returns an empty Series when the frames share no dates.
    """
    merged = stock_daily[["date", "open", "high", "low"]].merge(
        nifty_daily[["date", "open", "high", "low"]], on="date", suffixes=("", "_nifty"),
    )
    if merged.empty:
        return pd.Series(dtype=float)
    if side == "long":
        stock_move = (merged["high"] - merged["open"]) / merged["open"] * 100.0
        nifty_move = (merged["high_nifty"] - merged["open_nifty"]) / merged["open_nifty"] * 100.0
    else:
        stock_move = (merged["open"] - merged["low"]) / merged["open"] * 100.0
        nifty_move = (merged["open_nifty"] - merged["low_nifty"]) / merged["open_nifty"] * 100.0
    return (stock_move - nifty_move).reset_index(drop=True)


def recency_weight(n: int, halflife_days: float = _RECENCY_HALFLIFE_DAYS) -> np.ndarray:
    """Exponential-decay weights for ``n`` oldest-first rows.

    The most recent row (index ``n-1``) always weighs 1.0; a row ``halflife_days``
    sessions older weighs half that, decaying further the older it gets.
    """
    if n <= 0:
        return np.array([])
    age = np.arange(n - 1, -1, -1)
    return 0.5 ** (age / halflife_days)


def market_adjusted_hit_rate(
    stock_daily: pd.DataFrame, nifty_daily: pd.DataFrame, lookback: int,
    threshold_pct: float = _TARGET_FLOOR_PCT, halflife_days: float = _RECENCY_HALFLIFE_DAYS,
) -> float:
    """Like :func:`either_hit_rate`, but market-adjusted and recency-weighted.

    Subtracts Nifty's own same-day move first (so a stock only gets credit for
    moving on its own, not for riding a broad market rally/selloff), then weighs
    recent hits more than old ones — a stock whose edge has gone cold gets a
    lower score than the raw count alone would give it, and one that's newly
    live gets more credit than its raw count alone would give it.
    """
    long_moved = market_adjusted_move(stock_daily, nifty_daily, "long")
    if long_moved.empty:
        return 0.0
    short_moved = market_adjusted_move(stock_daily, nifty_daily, "short")
    long_tail = long_moved.tail(lookback).reset_index(drop=True)
    short_tail = short_moved.tail(lookback).reset_index(drop=True)
    hit = ((long_tail >= threshold_pct) | (short_tail >= threshold_pct)).astype(float)
    weights = recency_weight(len(hit), halflife_days)
    return round(float(np.average(hit, weights=weights)) * 100.0, 1)


def avg_range_pct(df: pd.DataFrame, lookback: int) -> float:
    """Average daily high-low range as a % of the open, over ``lookback`` bars."""
    tail = _tail(df, lookback)
    if tail.empty:
        return 0.0
    rng = (tail["high"] - tail["low"]) / tail["open"] * 100.0
    val = float(rng.mean())
    return round(val, 2) if not pd.isna(val) else 0.0


def atr_pct(df: pd.DataFrame) -> float:
    """ATR(14) as a % of the last close."""
    last_close = float(df.iloc[-1]["close"])
    if last_close <= 0:
        return 0.0
    return round(compute_atr(df, 14) / last_close * 100.0, 2)


def adtv_cr(df: pd.DataFrame, lookback: int = _ADTV_LOOKBACK) -> float:
    """Average daily traded value (₹ crore) over the trailing ``lookback`` bars."""
    tail = _tail(df, lookback)
    if tail.empty:
        return 0.0
    traded = float((tail["close"] * tail["volume"]).mean())
    return round(traded / 1e7, 1) if not pd.isna(traded) else 0.0


def suggested_target_pct(avg_range: float) -> float:
    """A realistic intraday target: ~60% of the typical range, clamped to 2–5%."""
    raw = avg_range * _TARGET_RANGE_FRACTION
    return round(min(max(raw, _MIN_TARGET_PCT), _MAX_TARGET_PCT), 1)


def score(hit_rate_2pct: float, avg_range: float) -> float:
    """0–100 blend: 2% reachability (dominant) + enough daily range for a target."""
    range_component = min(avg_range * 20.0, 100.0)  # 5%/day range → full marks
    return round(min(max(0.7 * hit_rate_2pct + 0.3 * range_component, 0.0), 100.0), 1)


def screen_symbol(symbol: str, df: Optional[pd.DataFrame], cfg: KittyBotConfig
                  ) -> Optional[ScreenMetrics]:
    """Build :class:`ScreenMetrics` for one symbol, or ``None`` if it doesn't qualify.

    Rejects when data is thin (< 20 bars) or liquidity is below the ADTV floor.
    """
    if df is None or len(df) < _MIN_ROWS:
        return None
    liquidity = adtv_cr(df)
    if liquidity < cfg.screen_min_adtv_cr:
        return None

    lookback = cfg.screen_lookback_days
    avg_range = avg_range_pct(df, lookback)
    hit_rate = either_hit_rate(df, lookback)
    target = suggested_target_pct(avg_range)
    ratio = cfg.reward_risk_ratio if cfg.reward_risk_ratio > 0 else 2.0
    return ScreenMetrics(
        symbol=symbol.upper(),
        score=score(hit_rate, avg_range),
        atr14_pct=atr_pct(df),
        avg_range_60d_pct=avg_range,
        hit_rate_2pct=hit_rate,
        long_room_2pct=directional_hit_rate(df, "long", lookback),
        short_room_2pct=directional_hit_rate(df, "short", lookback),
        suggested_target_pct=target,
        suggested_stop_pct=round(target / ratio, 2),
        prev_close=round(float(df.iloc[-1]["close"]), 2),
        adtv_cr=liquidity,
    )


def rank(metrics: list[ScreenMetrics], max_picks: int) -> list[ScreenMetrics]:
    """Top-``max_picks`` by score, ties broken by symbol for determinism."""
    return sorted(metrics, key=lambda m: (-m.score, m.symbol))[:max_picks]


def build_payload(ranked: list[ScreenMetrics], universe_size: int,
                  generated_at: datetime) -> dict:
    """The daily_picks.json envelope the bot's ``load_kitty`` reads."""
    return {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "universe_size": universe_size,
        "picks": [m.to_pick() for m in ranked],
    }
