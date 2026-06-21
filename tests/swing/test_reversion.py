"""Tests for the parameterized SMA7 mean-reversion engine (cross-stock lab)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.swing.reversion import Params, _simulate_day, run, stats


def _p(**kw):
    base = dict(gap_frac=-0.014, t1_frac=0.007, t2_frac=0.014, t3_frac=0.018,
                sl_frac=0.045, skip_downtrend=True)
    base.update(kw)
    return Params(**base)


def test_simulate_target_hit_before_stop():
    # Day reaches t1 (101) but not stop (95.5); should book t1.
    out = _simulate_day(o=100, h=101.5, l=99.0, c=101.0,
                        entry=100, sl=95.5, t1=101, t2=102, t3=103, fwd=[])
    assert out == ("t1", 101)


def test_simulate_gap_open_below_stop():
    out = _simulate_day(o=94, h=96, l=93, c=95,
                        entry=100, sl=95.5, t1=101, t2=102, t3=103, fwd=[])
    assert out == ("stop", 95.5)


def test_simulate_square_off_when_flat():
    out = _simulate_day(o=100, h=100.5, l=99.5, c=100.2,
                        entry=100, sl=95.5, t1=101, t2=102, t3=103, fwd=[])
    assert out == ("square_off", 100.2)


def test_run_triggers_only_below_gap_threshold():
    # Flat series sitting exactly on its SMA7 → never gaps below → no trades.
    idx = pd.date_range("2025-01-01", periods=40, freq="D")
    flat = pd.DataFrame({
        "date": idx, "open": 100.0, "high": 100.5, "low": 99.5,
        "close": 100.0, "volume": 1e6,
    })
    assert run(flat, _p()).n == 0


def test_stats_on_known_returns():
    from src.swing.reversion import RevResult
    res = RevResult(pnl_pct=[0.01, 0.01, -0.02, 0.01], outcomes=["t"] * 4,
                    dates=["d"] * 4)
    s = stats(res)
    assert s.n == 4
    assert s.win_rate == 75.0
    assert s.profit_factor == pytest.approx(1.5, abs=0.01)  # 0.03 / 0.02
