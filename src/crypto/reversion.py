"""
Multi-day SMA7 mean-reversion backtest for 24/7 crypto markets.

The EMCURE engine's semantics are intraday NSE (enter at open, square off at
the 15:30 close). Crypto has no session close, so this engine holds a position
across daily bars until the target, the stop, or a max-hold time exit — the
shape a ported managed cycle would actually trade.

Rules (all percentage-based — price-scale-free):
  Signal   day i's open gaps ≥ ``gap_pct`` below the 7-day SMA (of prior
           closes), and the prior week is not classified as a downtrend.
  Entry    at day i's open.
  Exit     first of: stop (low ≤ entry × (1 − sl_pct)) — checked FIRST each
           bar, pessimistically; target (high ≥ entry × (1 + target_pct));
           time exit at the close of the ``max_hold_days``-th bar.
           On the entry bar, when both stop and target print in one candle the
           closer-to-open heuristic decides (same as the NSE engines).

Pure functions over a lowercase-OHLC daily frame — no I/O, unit-testable on
synthetic data. Stats mirror src/swing/reversion.RevStats.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.emcure.intraday import classify_7d_trend, compute_sma7


@dataclass(frozen=True)
class CryptoParams:
    gap_pct: float           # trigger: open this fraction below SMA7 (e.g. 0.05 = 5%)
    target_pct: float        # single take-profit above entry
    sl_pct: float            # stop below entry
    max_hold_days: int = 10  # time exit at this bar's close
    skip_downtrend: bool = True


@dataclass
class CryptoRevResult:
    pnl_pct: list[float] = field(default_factory=list)   # per-trade gross return
    outcomes: list[str] = field(default_factory=list)    # target | stop | time_exit
    hold_days: list[int] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.pnl_pct)


def _entry_bar_exit(o: float, h: float, l: float, sl: float, target: float) -> str | None:
    """Outcome on the entry bar itself, or None to keep holding."""
    if o <= sl:
        return "stop"
    hit_sl, hit_target = l <= sl, h >= target
    if hit_sl and hit_target:
        # Both printed in one candle — closer-to-open wins (NSE engines' heuristic).
        return "target" if abs(target - o) < abs(sl - o) else "stop"
    if hit_sl:
        return "stop"
    if hit_target:
        return "target"
    return None


def run(df: pd.DataFrame, p: CryptoParams) -> CryptoRevResult:
    """Run the reversion backtest over a lowercase-OHLC daily frame.

    Overlapping signals are skipped while a position is open (one position at
    a time — matching the managed cycle)."""
    df = df.reset_index(drop=True)
    n = len(df)
    res = CryptoRevResult()
    i = 7
    while i < n:
        row = df.iloc[i]
        o = float(row["open"])
        sma7 = compute_sma7(df.iloc[:i])
        if sma7 <= 0 or (o - sma7) / sma7 > -p.gap_pct:
            i += 1
            continue
        if p.skip_downtrend and classify_7d_trend(df.iloc[i - 7:i]) == "Downward":
            i += 1
            continue

        entry = o
        sl = entry * (1 - p.sl_pct)
        target = entry * (1 + p.target_pct)

        outcome, exit_px, exit_bar = None, None, i
        first = _entry_bar_exit(o, float(row["high"]), float(row["low"]), sl, target)
        if first is not None:
            outcome, exit_px = first, (target if first == "target" else sl)
        else:
            for j in range(1, p.max_hold_days + 1):
                if i + j >= n:
                    break
                bar = df.iloc[i + j]
                exit_bar = i + j
                if float(bar["low"]) <= sl:          # stop first — pessimistic
                    outcome, exit_px = "stop", sl
                    break
                if float(bar["high"]) >= target:
                    outcome, exit_px = "target", target
                    break
            if outcome is None:
                outcome, exit_px = "time_exit", float(df.iloc[exit_bar]["close"])

        res.pnl_pct.append(exit_px / entry - 1.0)
        res.outcomes.append(outcome)
        res.hold_days.append(exit_bar - i)
        res.dates.append(str(row.get("date", ""))[:10])
        i = exit_bar + 1        # no overlapping positions
    return res


@dataclass(frozen=True)
class CryptoRevStats:
    n: int
    win_rate: float          # % of trades with positive return
    profit_factor: float
    expectancy_pct: float    # mean per-trade return (%)
    max_dd_pct: float        # max drawdown of the compounded equity curve (%)
    avg_hold_days: float


def stats(pnl: list[float], hold_days: list[int] | None = None) -> CryptoRevStats:
    """Stats over a list of per-trade returns (fractions). Feed it gross,
    fee-adjusted, or post-tax series — the maths is the same."""
    if not pnl:
        return CryptoRevStats(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    a = np.array(pnl)
    wins, losses = a[a > 0], a[a < 0]
    pf = float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf")
    equity = np.cumprod(1 + a)
    peak = np.maximum.accumulate(equity)
    max_dd = float(((equity - peak) / peak).min()) if len(equity) else 0.0
    return CryptoRevStats(
        n=len(a),
        win_rate=round(len(wins) / len(a) * 100, 1),
        profit_factor=round(pf, 2) if pf != float("inf") else float("inf"),
        expectancy_pct=round(float(a.mean()) * 100, 3),
        max_dd_pct=round(max_dd * 100, 1),
        avg_hold_days=round(float(np.mean(hold_days)), 1) if hold_days else 0.0,
    )
