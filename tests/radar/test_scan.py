"""Tests for the scan pipeline using an injected snapshot function (no network)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.radar import scan
from src.radar.regime import TRENDING_BULL

from .conftest import make_features


def _bull_nifty(n: int = 120) -> pd.DataFrame:
    closes = 20000.0 * (1.01) ** np.arange(n)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="D"),
            "open": closes, "high": closes * 1.005, "low": closes * 0.995,
            "close": closes, "volume": np.full(n, 1_000_000),
        }
    )


def test_run_scan_ranks_and_gates_liquidity():
    # EMCURE: stretched-below setup, liquid. SUZLON: illiquid → excluded.
    snaps = {
        "EMCURE": make_features(stock="EMCURE", price=1400.0, sma7=1440.0,
                                gap_to_sma7=-40.0, rsi=30.0, rvol=2.0,
                                atr=20.0, adtv_cr=500.0, above_50dma=True),
        "SUZLON": make_features(stock="SUZLON", price=50.0, sma7=52.0,
                                gap_to_sma7=-2.0, adtv_cr=5.0, above_50dma=True),
    }

    def fake_snapshot(sym, nifty):
        return snaps.get(sym)

    result = scan.run_scan(
        nifty_daily=_bull_nifty(),
        symbols=("EMCURE", "SUZLON"),
        snapshot_fn=fake_snapshot,
    )

    assert result.regime == TRENDING_BULL
    assert "SUZLON" in result.illiquid
    assert all(hit.stock == "EMCURE" for hit, _, _ in result.ranked)
    assert result.ranked  # at least one EMCURE signal scored


def test_above_gate_filters_low_confidence():
    snaps = {"EMCURE": make_features(stock="EMCURE", price=1400.0, sma7=1405.0,
                                     gap_to_sma7=-5.0, adtv_cr=500.0)}
    result = scan.run_scan(
        nifty_daily=_bull_nifty(), symbols=("EMCURE",),
        snapshot_fn=lambda s, n: snaps.get(s),
    )
    # Near-mean snapshot produces no strong setup → nothing above the 75 gate.
    assert result.above_gate() == []
