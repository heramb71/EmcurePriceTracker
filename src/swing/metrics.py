"""Performance metrics for a BacktestResult (§6 set + gate verdict)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest import BacktestResult

# Gate thresholds (P2, non-negotiable).
GATE_PF = 1.3
GATE_MAXDD = 0.12
GATE_MIN_TRADES = 10


@dataclass(frozen=True)
class Metrics:
    trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    avg_gain: float
    avg_loss: float
    avg_hold: float
    net_pnl: float
    total_return: float
    cagr: float
    max_drawdown: float
    sharpe: float

    def passes_gate(self) -> bool:
        return (
            self.trades >= GATE_MIN_TRADES
            and self.profit_factor >= GATE_PF
            and self.expectancy > 0
            and self.max_drawdown <= GATE_MAXDD
        )


def compute(result: BacktestResult) -> Metrics:
    ts = result.trades
    n = len(ts)
    if n == 0:
        return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    nets = np.array([t.net for t in ts])
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    gross_win = wins.sum()
    gross_loss = -losses.sum()

    pf = float(gross_win / gross_loss) if gross_loss > 0 else float("inf")
    win_rate = len(wins) / n
    expectancy = float(nets.mean())
    net_total = float(nets.sum())
    total_return = net_total / result.initial_capital
    final_eq = result.initial_capital + net_total
    cagr = (final_eq / result.initial_capital) ** (1 / result.years) - 1

    curve = result.equity_curve
    max_dd = _max_drawdown(curve)
    sharpe = _sharpe(curve)

    return Metrics(
        trades=n,
        win_rate=round(win_rate, 4),
        profit_factor=round(pf, 3),
        expectancy=round(expectancy, 2),
        avg_gain=round(float(wins.mean()), 2) if len(wins) else 0.0,
        avg_loss=round(float(losses.mean()), 2) if len(losses) else 0.0,
        avg_hold=round(float(np.mean([t.days_held for t in ts])), 1),
        net_pnl=round(net_total, 2),
        total_return=round(total_return, 4),
        cagr=round(cagr, 4),
        max_drawdown=round(max_dd, 4),
        sharpe=round(sharpe, 2),
    )


def _max_drawdown(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    running_max = curve.cummax()
    dd = (curve - running_max) / running_max
    return float(-dd.min())


def _sharpe(curve: pd.Series) -> float:
    if len(curve) < 3:
        return 0.0
    rets = curve.pct_change().dropna()
    if rets.std() == 0:
        return 0.0
    return float(rets.mean() / rets.std() * np.sqrt(252))
