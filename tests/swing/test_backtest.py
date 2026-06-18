"""Tests for the one-position portfolio backtester."""
import numpy as np
import pandas as pd

from src.swing import signals
from src.swing.backtest import BacktestConfig, run
from src.swing.regime import TRENDING_BULL
from src.swing.signals import Strategy


def _all_bull(dates):
    return {d: TRENDING_BULL for d in dates}


def _deterministic_target_frame():
    """55 flat bars (high=101, low=99 → ATR≈2), then a ramp that hits the target."""
    n = 60
    dates = list(pd.date_range("2024-01-01", periods=n, freq="B").date)
    close = [100.0] * n
    high = [101.0] * n
    low = [99.0] * n
    open_ = [100.0] * n
    # entry fires at index 55 → enter at open[56]=100; target = 100 + 3*2 = 106
    for j in range(57, n):
        high[j] = 110.0   # high clears 106 from bar 57 onward
        close[j] = 109.0
        low[j] = 105.0    # never breaches stop (97)
    return pd.DataFrame({"date": dates, "open": open_, "high": high,
                         "low": low, "close": close, "volume": [1000] * n})


def test_deterministic_target_hit_and_net():
    df = signals.prepare(_deterministic_target_frame())
    entry_at_55 = pd.Series(False, index=df.index)
    entry_at_55.iloc[55] = True
    strat = Strategy("t", lambda d: entry_at_55, atr_stop=1.5, atr_target=3.0,
                     max_hold=10, alt_exit_ma=None)

    res = run({"SYN": df}, _all_bull(df["date"]), strat, BacktestConfig())

    assert res.n == 1
    t = res.trades[0]
    assert t.outcome == "target"
    assert t.entry == 100.0 and t.exit_price == 106.0
    assert t.qty == 100          # risk 300 / stop_dist 3
    assert t.gross == 600.0      # (106-100) * 100
    assert t.net == round(600.0 - t.charges, 2) and t.charges > 0


def _uptrend_frame(seed, n=160):
    rng = np.random.default_rng(seed)
    close = np.linspace(100, 180, n) + rng.normal(0, 0.6, n)
    return pd.DataFrame({
        "date": list(pd.date_range("2023-01-02", periods=n, freq="B").date),
        "open": close - 0.2, "high": close + 1.2, "low": close - 1.2,
        "close": close, "volume": rng.integers(800, 1800, n),
    })


def test_one_position_invariant_no_overlap():
    prepared = {"A": signals.prepare(_uptrend_frame(1)),
                "B": signals.prepare(_uptrend_frame(2))}
    dates = sorted({d for df in prepared.values() for d in df["date"]})
    # Always-on entry (after warm-up) maximises overlap pressure so the one-position
    # sequencing is actually exercised, regardless of any entry rule's selectivity.
    always = Strategy("always", lambda d: pd.Series(
        [i >= 60 for i in range(len(d))], index=d.index), alt_exit_ma=None)
    res = run(prepared, _all_bull(dates), always, BacktestConfig())

    assert res.n >= 1
    ordered = sorted(res.trades, key=lambda t: t.entry_date)
    for prev, nxt in zip(ordered, ordered[1:]):
        # never re-enter before the prior position has exited
        assert nxt.entry_date > prev.exit_date
    # every outcome is one of the known exit reasons
    assert set(res.by_outcome) <= {"stop", "target", "ma_exit", "regime_exit", "time"}
