"""
Empirical touch-probabilities for arbitrary price levels.

The legacy predictor only has a calibrated curve for the fixed +₹10/20/25 intraday
targets. The managed-cycle uses arbitrary rupee levels (+15/20/30, SL −100), so we
estimate their odds directly from EMCURE's own recent daily OHLC: over the last
`lookback` days, how often did the stock's high reach +Δ% (touch an up-target) or
its low fall −Δ% (touch the stop) within `horizon_days`.

Moves are measured in PERCENT so older, lower-priced history stays comparable. The
estimate is a touch probability (favorable/adverse excursion), not a close-above
probability — i.e. "did price trade through the level at any point in the window".
"""
from __future__ import annotations

from typing import Optional

import pandas as pd


def touch_probabilities(
    df_daily: Optional[pd.DataFrame],
    current_price: float,
    up_levels: list[float],
    stop_level: float,
    horizon_days: int = 5,
    lookback: int = 180,
) -> dict:
    """Probability (0–100 int) that price touches each up-level (via daily high)
    and the stop-level (via daily low) within `horizon_days`, from the recent
    `lookback` days of OHLC. Keys: each up-level price, plus "stop". Returns {} if
    there is not enough data to estimate."""
    if df_daily is None or df_daily.empty or current_price <= 0:
        return {}

    df = df_daily.tail(lookback + horizon_days)
    if not {"high", "low", "close"}.issubset(df.columns):
        return {}

    highs  = df["high"].astype(float).tolist()
    lows   = df["low"].astype(float).tolist()
    closes = df["close"].astype(float).tolist()
    n = len(closes)
    if n < horizon_days + 20:          # too little history to mean anything
        return {}

    # Favorable (up) and adverse (down) excursion fractions for each start window.
    up_exc: list[float] = []
    dn_exc: list[float] = []
    for i in range(n - horizon_days):
        start = closes[i]
        if start <= 0:
            continue
        win_high = max(highs[i + 1 : i + 1 + horizon_days])
        win_low  = min(lows[i + 1 : i + 1 + horizon_days])
        up_exc.append(win_high / start - 1.0)
        dn_exc.append(1.0 - win_low / start)

    total = len(up_exc)
    if total == 0:
        return {}

    out: dict = {}
    for lvl in up_levels:
        need = (lvl - current_price) / current_price
        out[lvl] = 99 if need <= 0 else round(100 * sum(x >= need for x in up_exc) / total)

    need_dn = (current_price - stop_level) / current_price
    out["stop"] = 99 if need_dn <= 0 else round(100 * sum(x >= need_dn for x in dn_exc) / total)
    return out
