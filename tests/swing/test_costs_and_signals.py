"""Tests for the DP-charge addition and the signal layer."""
import numpy as np
import pandas as pd

from src.costs import _DP_CHARGE, compute_charges
from src.swing import signals


def test_dp_charge_adds_flat_amount():
    base = compute_charges(100, 110, 50, include_dp=False)
    with_dp = compute_charges(100, 110, 50, include_dp=True)
    assert round(with_dp - base, 2) == _DP_CHARGE


def test_dp_default_preserves_legacy_behaviour():
    # Existing callers must see no change when include_dp is omitted.
    assert compute_charges(100, 110, 50) == compute_charges(100, 110, 50, include_dp=False)


def test_charges_zero_on_bad_input():
    assert compute_charges(0, 110, 50) == 0.0
    assert compute_charges(100, 110, 0) == 0.0


def _uptrend_frame(n=120):
    rng = np.random.default_rng(0)
    drift = np.linspace(100, 160, n)
    noise = rng.normal(0, 0.5, n)
    close = drift + noise
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n, freq="B").date,
        "open": close - 0.2,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": rng.integers(800, 1600, n),
    })


def test_prepare_adds_all_indicator_columns():
    df = signals.prepare(_uptrend_frame())
    for col in ("ema20", "ema50", "rsi", "atr", "avg_vol", "rvol", "prev_high", "vwap"):
        assert col in df.columns


def test_entries_return_bool_series_warmup_gated():
    df = signals.prepare(_uptrend_frame())
    for fn in (signals.entry_breakout, signals.entry_pullback):
        sig = fn(df)
        assert sig.dtype == bool
        assert not sig.iloc[: signals.WARMUP].any()  # nothing fires in warm-up


def test_registry_has_breakout_and_pullback():
    assert {"breakout", "pullback"} <= set(signals.REGISTRY)
