"""Shared fixtures/factories for radar tests."""
from __future__ import annotations

from src.radar.features import StockFeatures


def make_features(**over) -> StockFeatures:
    """A neutral baseline snapshot; override fields per test."""
    base = dict(
        stock="EMCURE",
        price=1400.0,
        prev_close=1395.0,
        open=1396.0,
        sma7=1410.0,
        gap_to_sma7=-10.0,
        vwap=1402.0,
        rsi=50.0,
        atr=20.0,
        atr_expansion=1.0,
        rvol=1.0,
        ema20=1405.0,
        ema50=1390.0,
        prev_high=1408.0,
        gap_pct=0.07,
        rs20=0.0,
        adtv_cr=500.0,
        above_50dma=True,
    )
    base.update(over)
    return StockFeatures(**base)
