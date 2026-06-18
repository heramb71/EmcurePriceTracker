#!/usr/bin/env python3
"""
Swing backtest lab — load the universe, build the NIFTY regime, and compare
strategies through the cost-aware one-position portfolio engine.

Usage:
    python swing_lab.py                # all registered strategies, 3y
    python swing_lab.py --period 1y
    python swing_lab.py --strategy pullback

This is the P2 gate's front door: every strategy is judged here, net of costs,
under the real one-position constraint.
"""
from __future__ import annotations

import argparse
import logging

from src.swing import data, signals, universe
from src.swing.backtest import BacktestConfig, print_report, run
from src.swing.regime import regime_by_date
from src.swing.universe import NIFTY

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    ap = argparse.ArgumentParser(description="Swing backtest lab")
    ap.add_argument("--period", default="3y", help="yfinance period (e.g. 6mo, 1y, 3y)")
    ap.add_argument("--strategy", default=None, help="single strategy name; default = all")
    ap.add_argument("--capital", type=float, default=15_000.0)
    args = ap.parse_args()

    print(f"Loading universe {universe.symbols()} + NIFTY ({args.period})…")
    raw = data.fetch_universe(universe.symbols(), period=args.period)
    nifty = data.fetch_index(NIFTY, period=args.period)
    if not raw or nifty is None:
        print("Data load failed — aborting."); return

    regime = regime_by_date(nifty)
    prepared = {s: signals.prepare(df) for s, df in raw.items()}
    cfg = BacktestConfig(capital=args.capital)

    names = [args.strategy] if args.strategy else list(signals.REGISTRY)
    for name in names:
        strat = signals.REGISTRY.get(name)
        if strat is None:
            print(f"Unknown strategy '{name}' — known: {list(signals.REGISTRY)}"); continue
        result = run(prepared, regime, strat, cfg)
        print_report(result, title=f"{name.upper()}  (universe, 1-position, net of costs)")


if __name__ == "__main__":
    main()
