"""Parameterized SMA7 mean-reversion backtest for cross-stock generalization.

This is a scale-free re-expression of the live EMCURE engine (src/backtest.py +
src/intraday.py). The live engine hardcodes absolute-rupee levels (gap ≥ ₹20
below SMA7; targets +₹10/+₹20/+₹25) calibrated to EMCURE's ~₹1,400 price. To ask
"does the edge generalize?" fairly across names from ₹50 to ₹1,400, the trigger
and targets are expressed as fractions of price; everything else (skip-downtrend,
OHLC intraday hit-order heuristic, overnight-hold-on-drastic-fall) is identical.

Validated by reproducing EMCURE's known result before being applied elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.emcure.intraday import classify_7d_trend, compute_sma7

DRASTIC_FALL_FRAC = 0.011   # ~₹15 on EMCURE → fraction of entry
OVERNIGHT_HOLD_DAYS = 4


@dataclass(frozen=True)
class Params:
    gap_frac: float      # trigger: open this fraction below SMA7 (e.g. -0.014)
    t1_frac: float
    t2_frac: float
    t3_frac: float
    sl_frac: float       # stop this fraction below entry (EMCURE: risk/capital = 0.045)
    skip_downtrend: bool = True


@dataclass
class RevResult:
    pnl_pct: list[float] = field(default_factory=list)   # per-trade return on entry
    outcomes: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.pnl_pct)


def _closer_to_open(a: float, b: float, open_: float) -> bool:
    return abs(a - open_) < abs(b - open_)


def _simulate_day(o, h, l, c, entry, sl, t1, t2, t3, fwd) -> tuple[str, float]:
    """OHLC hit-order heuristic — identical logic to src/backtest._simulate_day."""
    if o <= sl:
        return "stop", sl
    if o >= t3:
        return "t3", t3
    hit_sl, hit_t1, hit_t2, hit_t3 = l <= sl, h >= t1, h >= t2, h >= t3
    if hit_sl and hit_t3:
        return ("t3", t3) if _closer_to_open(t3, sl, o) else ("stop", sl)
    if hit_sl and hit_t2:
        return ("t2", t2) if _closer_to_open(t2, sl, o) else ("stop", sl)
    if hit_sl and hit_t1:
        return ("t1", t1) if _closer_to_open(t1, sl, o) else ("stop", sl)
    if hit_sl:
        return "stop", sl
    if hit_t3:
        return "t3", t3
    if hit_t2:
        return "t2", t2
    if hit_t1:
        return "t1", t1
    if c < entry * (1 - DRASTIC_FALL_FRAC):
        for f in fwd:
            if f["high"] >= entry:
                return "nplp_recovered", entry
        return "overnight_loss", fwd[-1]["close"] if fwd else c
    return "square_off", c


def run(df: pd.DataFrame, p: Params) -> RevResult:
    """Run the parameterized reversion backtest on a lowercase-OHLC daily frame."""
    df = df.reset_index(drop=True)
    n = len(df)
    res = RevResult()
    for i in range(7, n):
        row = df.iloc[i]
        o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
        sma7 = compute_sma7(df.iloc[:i])
        if sma7 <= 0:
            continue
        gap_frac = (o - sma7) / sma7
        if gap_frac > p.gap_frac:        # not deep enough below SMA7
            continue
        if p.skip_downtrend and classify_7d_trend(df.iloc[i - 7:i]) == "Downward":
            continue

        entry = o
        sl = entry * (1 - p.sl_frac)
        t1, t2, t3 = (entry * (1 + f) for f in (p.t1_frac, p.t2_frac, p.t3_frac))
        fwd = [
            {"high": float(df.iloc[i + j]["high"]), "close": float(df.iloc[i + j]["close"])}
            for j in range(1, OVERNIGHT_HOLD_DAYS + 1) if i + j < n
        ]
        outcome, exit_px = _simulate_day(o, h, l, c, entry, sl, t1, t2, t3, fwd)
        res.pnl_pct.append(exit_px / entry - 1.0)
        res.outcomes.append(outcome)
        res.dates.append(str(row.get("date", ""))[:10])
    return res


def run_atr(df: pd.DataFrame, *, gap_atr: float, t_mults: tuple[float, float, float],
            sl_frac: float, skip_downtrend: bool = True) -> RevResult:
    """ATR-scaled variant: trigger and targets are multiples of each stock's own
    ATR(14), so the target is equally reachable across price/volatility scales.

    gap_atr   : trigger when open is this many ATRs below SMA7 (positive number)
    t_mults   : (t1,t2,t3) target ATR-multiples above entry
    sl_frac   : stop fraction below entry (price-%, as in the live engine)
    """
    from . import indicators as _ind
    df = df.reset_index(drop=True)
    ohlc = df.rename(columns={"high": "High", "low": "Low", "close": "Close"})
    atr = _ind.atr(ohlc, 14)
    n = len(df)
    res = RevResult()
    for i in range(14, n):
        row = df.iloc[i]
        o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
        sma7 = compute_sma7(df.iloc[:i])
        a = float(atr.iloc[i - 1])  # ATR known at prior close (no look-ahead)
        if sma7 <= 0 or not np.isfinite(a) or a <= 0:
            continue
        if (o - sma7) > -gap_atr * a:        # not deep enough below SMA7
            continue
        if skip_downtrend and classify_7d_trend(df.iloc[i - 7:i]) == "Downward":
            continue
        entry = o
        sl = entry * (1 - sl_frac)
        t1, t2, t3 = (entry + m * a for m in t_mults)
        fwd = [
            {"high": float(df.iloc[i + j]["high"]), "close": float(df.iloc[i + j]["close"])}
            for j in range(1, OVERNIGHT_HOLD_DAYS + 1) if i + j < n
        ]
        outcome, exit_px = _simulate_day(o, h, l, c, entry, sl, t1, t2, t3, fwd)
        res.pnl_pct.append(exit_px / entry - 1.0)
        res.outcomes.append(outcome)
        res.dates.append(str(row.get("date", ""))[:10])
    return res


@dataclass(frozen=True)
class RevStats:
    n: int
    win_rate: float
    profit_factor: float
    avg_pct: float        # mean per-trade return (%)
    median_pct: float
    sharpe: float


def stats(res: RevResult) -> RevStats:
    if res.n == 0:
        return RevStats(0, 0, 0, 0, 0, 0)
    a = np.array(res.pnl_pct)
    wins, losses = a[a > 0], a[a < 0]
    pf = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf")
    sharpe = float(a.mean() / a.std() * np.sqrt(252)) if a.std() > 0 else 0.0
    return RevStats(
        n=res.n,
        win_rate=round(len(wins) / res.n * 100, 1),
        profit_factor=round(pf, 2),
        avg_pct=round(float(a.mean()) * 100, 3),
        median_pct=round(float(np.median(a)) * 100, 3),
        sharpe=round(sharpe, 2),
    )
