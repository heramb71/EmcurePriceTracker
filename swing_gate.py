#!/usr/bin/env python3
"""
P2 validation gate — does SELECTIVITY beat the cost drag?

Sweeps the scanner score threshold for each strategy and reports the cost-net
metrics, then runs a simple in-sample / out-of-sample (walk-forward) split on the
best configuration. The gate passes only if a config clears, OUT-OF-SAMPLE:
PF ≥ 1.3, positive expectancy, maxDD ≤ ~12%. Otherwise the honest answer is
"stay in cash".
"""
from __future__ import annotations

import logging

from src.swing import data, scanner, signals, universe
from src.swing.backtest import BacktestConfig, run
from src.swing.regime import regime_by_date
from src.swing.universe import NIFTY

logging.basicConfig(level=logging.ERROR)
CFG = BacktestConfig()


def _load():
    raw = data.fetch_universe(universe.symbols(), period="3y")
    nifty = data.fetch_index(NIFTY, period="3y")
    regime = regime_by_date(nifty)
    prepared = {}
    for s, df in raw.items():
        p = signals.prepare(df)
        p["score"] = scanner.compute_score(p)
        prepared[s] = p
    return prepared, regime


def _line(tag, r):
    print(f"  {tag:<22} n={r.n:>3}  win={r.win_rate:>4}%  PF={r.profit_factor:>4}  "
          f"exp=₹{r.expectancy:>6}  CAGR={r.cagr:>5}%  maxDD={r.max_drawdown_pct:>4}%")


def main():
    prepared, regime = _load()
    all_dates = sorted({d for df in prepared.values() for d in df["date"]})
    split = all_dates[int(len(all_dates) * 0.6)]   # 60/40 in/out-of-sample
    print(f"Loaded {list(prepared)}  | {all_dates[0]}→{all_dates[-1]}  | OOS starts {split}\n")

    for name in ("breakout", "pullback"):
        strat = signals.REGISTRY[name]
        print(f"=== {name.upper()} — selectivity sweep (full 3y, net of costs) ===")
        for thr in (0, 60, 70, 75, 80):
            _line(f"score>{thr}", run(prepared, regime, strat, CFG, min_score=thr))
        print()

    # Walk-forward on the most promising lever (pullback, moderate gate).
    print("=== WALK-FORWARD (pullback, score>70) ===")
    strat = signals.REGISTRY["pullback"]
    _line("in-sample", run(prepared, regime, strat, CFG, end=split, min_score=70))
    _line("out-of-sample", run(prepared, regime, strat, CFG, start=split, min_score=70))


if __name__ == "__main__":
    main()
