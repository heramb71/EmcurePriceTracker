"""
Swing-trading bot package.

A multi-stock, daily-bar swing/delivery system that ranks a liquid NSE universe,
takes at most one position at a time, and stays in cash unless a high-quality
setup clears the score gate. Built lab-first: the cost-aware portfolio backtester
(`src.swing.backtest`) is the validation gate that every strategy must pass before
any capital is deployed.

Module map:
    universe    — tradeable symbol list + metadata
    indicators  — vectorised indicators returning full pandas Series
    data        — multi-symbol + index (NIFTY/VIX) daily loaders
    regime      — NIFTY-based market-regime gate (bull/bear)
    signals     — entry (pullback / breakout) + exit rule evaluation
    backtest    — one-position portfolio simulator, cost-aware, walk-forward
"""
