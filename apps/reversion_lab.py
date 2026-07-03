#!/usr/bin/env python3
"""Does the EMCURE SMA7 mean-reversion edge generalize to the other 5 names?

Experiment design (honest, two readings of "identical parameters"):
  A. LITERAL identical — the live engine's absolute-rupee rules (gap ≥ ₹20 below
     SMA7, targets +₹10/₹20/₹25) applied unchanged to every name. Answers the
     question as stated; exposes the price-scale problem.
  B. SCALE-NORMALIZED identical — the same rules expressed as % of price,
     calibrated once on EMCURE, applied identically to all 6. The real test of
     whether the *edge* (reversion after gapping below SMA7) is present elsewhere.

Validation: the normalized engine must reproduce EMCURE's live-engine result
before we trust it on other names. Walk-forward: in-sample (1st half) vs
out-of-sample (2nd half) per name. Costs: intraday round-trip drag netted.

Usage:  python reversion_lab.py
"""
from __future__ import annotations

import sys

import pandas as pd
import yfinance as yf

from src.emcure.backtest import run_backtest
from src.swing.reversion import Params, run, stats
from src.swing.universe import SYMBOLS, to_yf

# EMCURE absolute rules → fractions, calibrated at its mean price (printed below).
# gap ₹20, targets ₹10/₹20/₹25, SL = risk/capital = 4500/100000 = 4.5%.
EMCURE_ABS = {"gap": 20.0, "t1": 10.0, "t2": 20.0, "t3": 25.0, "sl_frac": 0.045}

CAPITAL = 100_000.0
RISK = 4_500.0


def _fetch(symbol: str) -> pd.DataFrame | None:
    df = yf.download(to_yf(symbol), period="3y", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df.reset_index().rename(columns={"Date": "date", "index": "date"})
    return df if len(df) > 60 else None


def intraday_cost_frac(capital: float) -> float:
    """Zerodha MIS intraday round-trip cost as a fraction of notional (≈capital)."""
    buy = sell = capital
    brokerage = min(0.0003 * buy, 20) + min(0.0003 * sell, 20)
    stt = 0.00025 * sell
    txn = 0.0000297 * (buy + sell)
    sebi = 0.000001 * (buy + sell)
    stamp = 0.00003 * buy
    gst = 0.18 * (brokerage + txn + sebi)
    return (brokerage + stt + txn + sebi + stamp + gst) / capital


def normalized_params(mean_price: float) -> Params:
    return Params(
        gap_frac=-EMCURE_ABS["gap"] / mean_price,
        t1_frac=EMCURE_ABS["t1"] / mean_price,
        t2_frac=EMCURE_ABS["t2"] / mean_price,
        t3_frac=EMCURE_ABS["t3"] / mean_price,
        sl_frac=EMCURE_ABS["sl_frac"],
    )


def _hdr(title):
    print("\n" + "=" * 90 + f"\n{title}\n" + "=" * 90)


def main() -> int:
    print("Downloading 6 names (3y daily)...", flush=True)
    data: dict[str, pd.DataFrame] = {}
    for s in SYMBOLS:
        df = _fetch(s)
        if df is not None:
            data[s] = df
            print(f"  ✓ {s:<10} {len(df):>4} bars  mean ₹{df['close'].mean():>8.1f}")
        else:
            print(f"  ! {s}: skipped")
    if "EMCURE" not in data:
        print("EMCURE missing — cannot calibrate.")
        return 1

    cost = intraday_cost_frac(CAPITAL)
    emcure_mean = float(data["EMCURE"]["close"].mean())
    p_norm = normalized_params(emcure_mean)

    # ── Validation: normalized engine vs live absolute engine on EMCURE ──
    _hdr("VALIDATION — normalized engine must match the live EMCURE engine")
    live = run_backtest(data["EMCURE"], capital=CAPITAL, risk_rupees=RISK)
    live_exp_pct = live.expectancy / CAPITAL * 100
    nrm = stats(run(data["EMCURE"], p_norm))
    print(f"  EMCURE mean price ₹{emcure_mean:.1f}  →  gap {p_norm.gap_frac*100:.2f}%  "
          f"targets {p_norm.t1_frac*100:.2f}/{p_norm.t2_frac*100:.2f}/{p_norm.t3_frac*100:.2f}%  "
          f"SL {p_norm.sl_frac*100:.1f}%")
    print(f"  live  engine : n={len(live.trades):>3}  win={live.win_rate:>5.1f}%  "
          f"PF={live.profit_factor:>5.2f}  avg={live_exp_pct:+.3f}%/trade")
    print(f"  norm  engine : n={nrm.n:>3}  win={nrm.win_rate:>5.1f}%  "
          f"PF={nrm.profit_factor:>5.2f}  avg={nrm.avg_pct:+.3f}%/trade")
    print(f"  intraday round-trip cost ≈ {cost*100:.3f}% of notional (at ₹{CAPITAL:,.0f})")

    # ── A. LITERAL identical (absolute ₹) via the live engine ──
    _hdr("A. LITERAL identical params (absolute ₹20 gap, +₹10/₹20/₹25) — live engine")
    print(f"  {'symbol':<10}{'trades':>7}{'win%':>8}{'PF':>7}{'avg%/trd':>10}{'net%/trd':>10}")
    for s, df in data.items():
        r = run_backtest(df, capital=CAPITAL, risk_rupees=RISK)
        avg = r.expectancy / CAPITAL * 100
        print(f"  {s:<10}{len(r.trades):>7}{r.win_rate:>8.1f}{r.profit_factor:>7.2f}"
              f"{avg:>+10.3f}{avg - cost*100:>+10.3f}")

    # ── B. SCALE-NORMALIZED identical (%) ──
    _hdr("B. SCALE-NORMALIZED identical params (EMCURE %s applied to all) — full 3y")
    print(f"  {'symbol':<10}{'trades':>7}{'win%':>8}{'PF':>7}{'avg%/trd':>10}{'net%/trd':>10}{'Sharpe':>8}")
    for s, df in data.items():
        st = stats(run(df, p_norm))
        net = st.avg_pct - cost * 100
        print(f"  {s:<10}{st.n:>7}{st.win_rate:>8.1f}{st.profit_factor:>7.2f}"
              f"{st.avg_pct:>+10.3f}{net:>+10.3f}{st.sharpe:>8.2f}")

    # ── Walk-forward: out-of-sample (2nd half) per name, normalized params ──
    _hdr("C. WALK-FORWARD — out-of-sample (2nd half) only, normalized params")
    print(f"  {'symbol':<10}{'IS n':>6}{'IS win%':>9}{'IS PF':>7}   "
          f"{'OOS n':>6}{'OOS win%':>9}{'OOS PF':>8}{'OOS net%':>10}")
    for s, df in data.items():
        mid = len(df) // 2
        is_st = stats(run(df.iloc[:mid], p_norm))
        oos_st = stats(run(df.iloc[mid:], p_norm))
        oos_net = oos_st.avg_pct - cost * 100
        print(f"  {s:<10}{is_st.n:>6}{is_st.win_rate:>9.1f}{is_st.profit_factor:>7.2f}   "
              f"{oos_st.n:>6}{oos_st.win_rate:>9.1f}{oos_st.profit_factor:>8.2f}{oos_net:>+10.3f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
