"""Forward-outcome evaluator.

For each (signal, horizon) whose window has matured, replay the price path from
the alert and compute Maximum Favorable/Adverse Excursion and a WIN/LOSS/NEUTRAL
label. WIN = target reached before stop within the window; LOSS = stop first;
NEUTRAL = neither. All hits are long (the universe is traded long).

The window-evaluation core is a pure function (testable with synthetic bars,
mirroring ``tests/swing/test_reversion.py`` hit-order discipline); the rest just
fetches bars via ``src.shared.data`` and writes results via ``src.radar.store``.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

from src.radar import store
from src.shared.data import fetch_daily, fetch_intraday

logger = logging.getLogger(__name__)

# Horizons short enough that intraday 5m bars (≈5 trading days available) give a
# faithful path; longer ones evaluate on daily bars.
_INTRADAY_HORIZONS = {"1h", "4h", "1d"}


def evaluate_window(
    entry: float, stop: float, target: float, bars: list[dict]
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """Evaluate a long position over ``bars`` (chronological, high/low/close).

    Returns ``(price_at_end, mfe, mae, outcome)``. Same-bar stop+target is scored
    as LOSS (conservative). Empty bars ⇒ all ``None``.
    """
    if not bars:
        return None, None, None, None

    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    mfe = round(max(highs) - entry, 2)
    mae = round(min(lows) - entry, 2)
    price_at_end = round(float(bars[-1]["close"]), 2)

    outcome = "NEUTRAL"
    for b in bars:
        hit_stop = b["low"] <= stop
        hit_target = b["high"] >= target
        if hit_stop:                 # conservative: stop wins ties
            outcome = "LOSS"
            break
        if hit_target:
            outcome = "WIN"
            break
    return price_at_end, mfe, mae, outcome


def _bars_in_window(
    df: Optional[pd.DataFrame], start: datetime, end: datetime
) -> list[dict]:
    """Rows with ``start < date <= end`` as ordered high/low/close dicts."""
    if df is None or df.empty:
        return []
    dates = pd.to_datetime(df["date"])
    mask = (dates > start) & (dates <= end)
    sub = df[mask]
    return [
        {"high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])}
        for _, r in sub.iterrows()
    ]


def evaluate_due(
    conn: sqlite3.Connection,
    now: Optional[datetime] = None,
    *,
    daily_fetch: Callable[[str], Optional[pd.DataFrame]] = lambda s: fetch_daily(s, days=60),
    intraday_fetch: Callable[[str], Optional[pd.DataFrame]] = lambda s: fetch_intraday(s, "5m", 5),
) -> int:
    """Fill every matured, unrecorded outcome. Returns the count written."""
    now = now or datetime.now()
    due = store.due_outcomes(conn, now=now)
    if not due:
        return 0

    # Fetch each stock's bars once per sweep.
    stocks = {d["stock"] for d in due}
    daily_cache = {s: daily_fetch(s) for s in stocks}
    intraday_cache = {s: intraday_fetch(s) for s in stocks}

    written = 0
    for d in due:
        start: datetime = d["ts"]
        end = start + store.horizon_delta(d["horizon"])
        use_intraday = d["horizon"] in _INTRADAY_HORIZONS
        df = intraday_cache[d["stock"]] if use_intraday else daily_cache[d["stock"]]
        bars = _bars_in_window(df, start, end)
        if not bars and use_intraday:
            # Intraday history aged out — fall back to daily bars.
            bars = _bars_in_window(daily_cache[d["stock"]], start, end)

        price, mfe, mae, outcome = evaluate_window(
            d["price_at_alert"], d["suggested_stop"], d["suggested_target"], bars
        )
        if outcome is None:
            # No bars yet (e.g. data lag); leave it due for a later sweep.
            continue
        store.record_outcome(
            conn, signal_id=d["signal_id"], horizon=d["horizon"],
            price=price, mfe=mfe, mae=mae, outcome=outcome, evaluated_at=now,
        )
        written += 1
    return written
