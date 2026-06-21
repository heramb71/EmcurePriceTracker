#!/usr/bin/env python3
"""Swing-bot validation GATE — re-verify the P2 verdict on current data.

Downloads the universe (3y daily), then for each lookback window runs the
cost-aware one-position portfolio backtest for both entry variants, with and
without the score>80 gate. Finishes with a capital-scaling test to isolate the
gross edge from cost drag, and prints the PASS/FAIL gate verdict.

Usage:  python swing_gate.py
"""
from __future__ import annotations

import sys

import pandas as pd
import yfinance as yf

from src.swing import backtest as bt
from src.swing import metrics as mx
from src.swing.universe import NIFTY, SYMBOLS, to_yf

WINDOWS = {"3M": 63, "6M": 126, "1Y": 252, "3Y": 756}
SCALE_CAPITALS = [15_000, 50_000, 100_000, 500_000]


def _fetch(symbol: str) -> pd.DataFrame | None:
    df = yf.download(to_yf(symbol), period="3y", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return df if len(df) > 60 else None


def _load() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    print("Downloading universe (3y daily)...", flush=True)
    raw: dict[str, pd.DataFrame] = {}
    for s in SYMBOLS:
        df = _fetch(s)
        if df is None:
            print(f"  ! {s}: insufficient data, skipped")
            continue
        raw[s] = df
        print(f"  ✓ {s}: {len(df)} bars ({df.index[0].date()} → {df.index[-1].date()})")
    nifty = _fetch(NIFTY.replace("^", "")) if False else _fetch_index()
    return raw, nifty


def _fetch_index() -> pd.DataFrame:
    df = yf.download(NIFTY, period="3y", interval="1d", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


def _slice(raw, nifty, bars):
    sub = {s: df.tail(bars) for s, df in raw.items() if len(df) >= 30}
    return sub, nifty.tail(bars + 50)  # extra nifty history for 50-DMA


def _row(label, m: mx.Metrics) -> str:
    return (f"  {label:<26} n={m.trades:>3}  win={m.win_rate*100:>5.1f}%  "
            f"PF={m.profit_factor:>5.2f}  exp=₹{m.expectancy:>8.2f}  "
            f"CAGR={m.cagr*100:>6.1f}%  maxDD={m.max_drawdown*100:>5.1f}%  "
            f"Sharpe={m.sharpe:>5.2f}")


def main() -> int:
    raw, nifty = _load()
    if not raw:
        print("No tradable symbols downloaded — aborting.")
        return 1

    print("\n" + "=" * 96)
    print("WINDOW SWEEP — one position, next-open entry, net of Zerodha CNC + DP")
    print("=" * 96)
    for wname, bars in WINDOWS.items():
        sub, nft = _slice(raw, nifty, bars)
        print(f"\n[{wname}]  ({len(sub)} symbols)")
        for variant in ("breakout", "pullback"):
            for gate in (True, False):
                res = bt.run(sub, nft, capital=15_000, variant=variant,
                             use_score_gate=gate)
                m = mx.compute(res)
                tag = f"{variant}{' +gate>80' if gate else ' (no gate)'}"
                print(_row(tag, m))

    print("\n" + "=" * 96)
    print("CAPITAL-SCALING TEST — best variant (pullback, no score gate), "
          "risk=2% uncapped\n  isolates gross edge from ₹-cost drag over the full 3Y")
    print("=" * 96)
    for cap in SCALE_CAPITALS:
        res = bt.run(raw, nifty, capital=cap, variant="pullback",
                     use_score_gate=False, risk_cap=None)
        m = mx.compute(res)
        print(_row(f"₹{cap:>7,}", m))

    print("\n" + "=" * 96)
    print("GATE VERDICT (3Y, ₹15k, as-specified pullback +gate>80)")
    print("=" * 96)
    res = bt.run(raw, nifty, capital=15_000, variant="pullback", use_score_gate=True)
    m = mx.compute(res)
    print(_row("as-specified", m))
    print(f"\n  Required: PF≥{mx.GATE_PF}  expectancy>0  maxDD≤{mx.GATE_MAXDD*100:.0f}%  "
          f"trades≥{mx.GATE_MIN_TRADES}")
    verdict = "PASS — proceed to production" if m.passes_gate() else "FAIL — stay in cash"
    print(f"  VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
