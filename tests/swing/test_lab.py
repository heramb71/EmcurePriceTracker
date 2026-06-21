"""Regression tests locking the swing lab harness behaviour.

These do NOT assert an edge (there is none — the gate fails). They protect the
backtester from silent look-ahead / cost / exit-logic regressions so the FAIL
verdict stays trustworthy.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.costs import compute_charges
from src.swing import indicators as ind
from src.swing import signals as sig
from src.swing.backtest import DP_CHARGE, _close


def _bar(low, high, close, ema20, vwap):
    return pd.Series({"Low": low, "High": high, "Close": close,
                      "ema20": ema20, "vwap": vwap})


# ── cost model integration ────────────────────────────────────────────────
def test_dp_charge_added_on_exit():
    pos = {"symbol": "X", "entry_date": pd.Timestamp("2025-01-01"),
           "entry": 100.0, "qty": 10, "days_held": 3}
    trade = _close(pos, pd.Timestamp("2025-01-05"), 110.0, "target")
    expected_charges = compute_charges(100.0, 110.0, 10) + DP_CHARGE
    assert trade.charges == pytest.approx(expected_charges, abs=0.01)
    assert trade.gross == pytest.approx(100.0)  # (110-100)*10
    assert trade.net == pytest.approx(trade.gross - trade.charges, abs=0.01)


# ── exit logic ─────────────────────────────────────────────────────────────
def test_stop_fires_at_stop_price():
    bar = _bar(low=94.0, high=101.0, close=95.0, ema20=98.0, vwap=99.0)
    out = sig.should_exit(bar, 100.0, stop=95.0, target=130.0, days_held=2,
                          regime="TRENDING_BULL")
    assert out == (True, "stop", 95.0)


def test_target_fires_at_target_price():
    bar = _bar(low=99.0, high=131.0, close=128.0, ema20=110.0, vwap=112.0)
    out = sig.should_exit(bar, 100.0, stop=95.0, target=130.0, days_held=2,
                          regime="TRENDING_BULL")
    assert out == (True, "target", 130.0)


def test_pullback_skips_ma_exit_when_disabled():
    # close below ema20: would exit if MA-exits on; must NOT for pullback.
    bar = _bar(low=99.0, high=101.0, close=99.5, ema20=100.0, vwap=100.5)
    out = sig.should_exit(bar, 100.0, stop=95.0, target=130.0, days_held=2,
                          regime="TRENDING_BULL", use_ma_exits=False)
    assert out[0] is False


def test_breakout_takes_ma_exit_when_enabled():
    bar = _bar(low=99.0, high=101.0, close=99.5, ema20=100.0, vwap=100.5)
    out = sig.should_exit(bar, 100.0, stop=95.0, target=130.0, days_held=2,
                          regime="TRENDING_BULL", use_ma_exits=True)
    assert out == (True, "below_ema20", 99.5)


# ── entry signals ──────────────────────────────────────────────────────────
def test_breakout_entry_when_all_conditions_met():
    # One bar with every breakout condition satisfied.
    df = pd.DataFrame({
        "Close": [110.0], "prev_high": [108.0], "vwap": [105.0],
        "ema20": [104.0], "ema50": [100.0], "rsi": [62.0], "rvol": [1.8],
    })
    assert sig.breakout_entry(df).iloc[0]


def test_breakout_entry_blocked_when_below_prev_high():
    df = pd.DataFrame({
        "Close": [107.0], "prev_high": [108.0], "vwap": [105.0],
        "ema20": [104.0], "ema50": [100.0], "rsi": [62.0], "rvol": [1.8],
    })
    assert not sig.breakout_entry(df).iloc[0]


def test_pullback_entry_when_dip_to_ema_in_uptrend():
    df = pd.DataFrame({
        "Close": [100.5], "ema20": [100.0], "ema50": [95.0], "rsi": [48.0],
    })
    assert sig.pullback_entry(df).iloc[0]


def test_atr_needs_warmup_no_lookahead():
    idx = pd.date_range("2025-01-01", periods=30, freq="D")
    close = pd.Series(np.linspace(100, 130, 30), index=idx)
    df = pd.DataFrame({"High": close * 1.01, "Low": close * 0.99, "Close": close})
    atr = ind.atr(df, 14)
    assert atr.iloc[:13].isna().all()  # needs warmup, no early leakage
