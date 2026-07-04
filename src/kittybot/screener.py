"""Pre-market screener: rank the kitty universe into the day's top-N candidates.

Pure metric computation over daily OHLCV frames (from ``src.shared.data``) so it
unit-tests on synthetic data. ``apps.kitty_screener`` fetches the bars and writes
``daily_picks.json``; everything measurable lives here:

* :func:`directional_hit_rate` — % of recent sessions a 2% move was available
* :func:`avg_range_pct`, :func:`atr_pct`, :func:`adtv_cr` — volatility / liquidity
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
