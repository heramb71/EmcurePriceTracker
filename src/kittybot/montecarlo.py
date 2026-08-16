"""Monte Carlo resampling of a win/loss outcome series into equity-curve risk
stats (drawdown percentiles, risk-of-ruin) — the distribution-of-outcomes check
domain research flagged as missing from an average-case-only backtest.

Resamples the booleans from :mod:`apps.kitty_score_backtest` (did the day's #1
pick move >=2%?), NOT real trade P&L — each outcome is scaled to a synthetic
R-multiple using the bot's own ``reward_risk_ratio``/``risk_per_trade_pct``, so
results describe "if this hit-rate held and every trade was sized/exited
exactly per config," not a true fill-by-fill replay (that needs the
second-wave measured-move/confirmation backtest, not yet built). Every printed
verdict should carry this caveat.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

# Professional practice keeps modeled risk-of-ruin under this range; see the
# domain research (prop-firm risk practice + Monte Carlo validation sections).
_RUIN_BOUND_LOW_PCT = 1.0
_RUIN_BOUND_HIGH_PCT = 5.0


@dataclass(frozen=True)
class MonteCarloResult:
    n_paths: int
    trades_per_path: int
    pctile_10_return_pct: float
    pctile_50_return_pct: float
    pctile_90_return_pct: float
    median_max_drawdown_pct: float
    pctile_90_max_drawdown_pct: float
    ruin_probability_pct: float
    ruin_threshold_pct: float
    verdict: str


def resample_paths(
    outcomes: Sequence[bool],
    *,
    n_paths: int = 5000,
    trades_per_path: Optional[int] = None,
    win_r: float = 2.0,
    loss_r: float = -1.0,
    risk_pct: float = 1.0,
    seed: Optional[int] = None,
) -> np.ndarray:
    """Bootstrap ``n_paths`` compounding equity curves of ``trades_per_path``
    trades each (default ``len(outcomes)``), resampled with replacement from
    ``outcomes``.

    Each drawn outcome becomes a per-trade return of ``win_r * risk_pct`` %
    (hit) or ``loss_r * risk_pct`` % (miss); returns compound multiplicatively.
    Shape ``(n_paths, trades_per_path + 1)`` — column 0 is the starting equity
    (1.0, i.e. 100%) so drawdown can be measured from the true starting point.
    """
    if not outcomes:
        raise ValueError("resample_paths requires a non-empty outcomes sequence")
    n = trades_per_path if trades_per_path is not None else len(outcomes)
    rng = np.random.default_rng(seed)
    pool = np.asarray(outcomes, dtype=bool)
    draws = rng.choice(pool, size=(n_paths, n), replace=True)
    per_trade_return_pct = np.where(draws, win_r * risk_pct, loss_r * risk_pct)
    multipliers = 1.0 + per_trade_return_pct / 100.0
    equity = np.cumprod(multipliers, axis=1)
    return np.concatenate([np.ones((n_paths, 1)), equity], axis=1)


def path_max_drawdowns(equity_paths: np.ndarray) -> np.ndarray:
    """Per-path peak-to-trough max drawdown %, on the compounding curve."""
    running_max = np.maximum.accumulate(equity_paths, axis=1)
    drawdown_pct = (running_max - equity_paths) / running_max * 100.0
    return drawdown_pct.max(axis=1)


def ruin_probability(equity_paths: np.ndarray, ruin_threshold_pct: float = 50.0) -> float:
    """% of paths whose equity ever falls below ``ruin_threshold_pct`` % of
    the starting value (1.0)."""
    threshold = ruin_threshold_pct / 100.0
    breached = (equity_paths < threshold).any(axis=1)
    return round(float(breached.mean()) * 100.0, 2)


def _verdict(ruin_pct: float) -> str:
    if ruin_pct <= _RUIN_BOUND_HIGH_PCT:
        return (f"ruin probability {ruin_pct:.1f}% — within the "
                f"{_RUIN_BOUND_LOW_PCT:.0f}-{_RUIN_BOUND_HIGH_PCT:.0f}% professional bound")
    return (f"ruin probability {ruin_pct:.1f}% — ABOVE the "
            f"{_RUIN_BOUND_LOW_PCT:.0f}-{_RUIN_BOUND_HIGH_PCT:.0f}% professional bound, "
            "do not go live on this edge without cutting position size")


def summarize(
    outcomes: Sequence[bool],
    *,
    n_paths: int = 5000,
    trades_per_path: Optional[int] = None,
    win_r: float = 2.0,
    loss_r: float = -1.0,
    risk_pct: float = 1.0,
    ruin_threshold_pct: float = 50.0,
    seed: Optional[int] = None,
) -> MonteCarloResult:
    """Run the resample + all stats, bundled with a plain-English verdict."""
    paths = resample_paths(
        outcomes, n_paths=n_paths, trades_per_path=trades_per_path,
        win_r=win_r, loss_r=loss_r, risk_pct=risk_pct, seed=seed,
    )
    final_return_pct = (paths[:, -1] - 1.0) * 100.0
    drawdowns = path_max_drawdowns(paths)
    ruin_pct = ruin_probability(paths, ruin_threshold_pct)
    return MonteCarloResult(
        n_paths=paths.shape[0],
        trades_per_path=paths.shape[1] - 1,
        pctile_10_return_pct=round(float(np.percentile(final_return_pct, 10)), 2),
        pctile_50_return_pct=round(float(np.percentile(final_return_pct, 50)), 2),
        pctile_90_return_pct=round(float(np.percentile(final_return_pct, 90)), 2),
        median_max_drawdown_pct=round(float(np.percentile(drawdowns, 50)), 2),
        pctile_90_max_drawdown_pct=round(float(np.percentile(drawdowns, 90)), 2),
        ruin_probability_pct=ruin_pct,
        ruin_threshold_pct=ruin_threshold_pct,
        verdict=_verdict(ruin_pct),
    )
