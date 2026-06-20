"""0–100 confidence score + per-scan ranking.

Weights follow the brief's inputs (sum to 100):
    RVOL 20 · dist-from-SMA7 15 · dist-from-VWAP 15 · ATR-expansion 15 ·
    RSI 10 · RS/sector 10 · regime alignment 15.

Reversion and momentum families read some inputs in opposite directions (a
reversion setup *wants* a low RSI and a price below VWAP; a momentum setup wants
the reverse), so those sub-scores are oriented by signal family. Anchored on
``src/swing/scanner.py`` formula shapes, adapted to scalar inputs.
"""
from __future__ import annotations

from src.radar.features import StockFeatures
from src.radar.regime import SIDEWAYS, TRENDING_BEAR, TRENDING_BULL
from src.radar.signals import (
    ATR_BREAKOUT,
    GAP_REVERSION,
    RVOL_REVERSAL,
    SMA7_REVERSION,
    SignalHit,
    VWAP_PULLBACK,
)

W_RVOL = 20.0
W_SMA7 = 15.0
W_VWAP = 15.0
W_ATR = 15.0
W_RSI = 10.0
W_RS = 10.0
W_REGIME = 15.0

SCORE_GATE = 75  # alert only when confidence > this

_REVERSION = {SMA7_REVERSION, RVOL_REVERSAL, GAP_REVERSION}
_MOMENTUM = {VWAP_PULLBACK, ATR_BREAKOUT}

# Distance/RSI normalisation spans.
_DIST_SPAN = 0.03   # 3% move maps to a full sub-score
_RSI_SPAN = 20.0    # 20 RSI points maps to a full sub-score


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _regime_alignment(regime: str) -> float:
    if regime == TRENDING_BULL:
        return 1.0
    if regime == SIDEWAYS:
        return 0.5
    return 0.0  # TRENDING_BEAR (long setups are filtered upstream anyway)


def confidence(f: StockFeatures, signal: SignalHit, regime: str) -> int:
    """Confidence 0–100 for one (snapshot, signal) pair."""
    is_reversion = signal.signal_type in _REVERSION

    rvol_s = _clip01((f.rvol - 1.0) / 1.0) * W_RVOL

    below_sma7 = (-f.gap_to_sma7 / f.price) if f.price else 0.0
    below_vwap = ((f.vwap - f.price) / f.price) if f.price else 0.0
    if is_reversion:
        sma7_s = _clip01(below_sma7 / _DIST_SPAN) * W_SMA7
        vwap_s = _clip01(below_vwap / _DIST_SPAN) * W_VWAP
        rsi_s = _clip01((50.0 - f.rsi) / _RSI_SPAN) * W_RSI
    else:  # momentum
        sma7_s = _clip01(-below_sma7 / _DIST_SPAN) * W_SMA7  # above SMA7 favoured
        vwap_s = _clip01(-below_vwap / _DIST_SPAN) * W_VWAP  # above VWAP favoured
        rsi_s = _clip01((f.rsi - 50.0) / _RSI_SPAN) * W_RSI

    atr_s = _clip01((f.atr_expansion - 1.0) / 0.5) * W_ATR
    rs_s = _clip01(f.rs20 / 0.05) * W_RS
    regime_s = _regime_alignment(regime) * W_REGIME

    total = rvol_s + sma7_s + vwap_s + atr_s + rsi_s + rs_s + regime_s
    return int(round(max(0.0, min(100.0, total))))


def rank(scored: list[tuple[SignalHit, int]]) -> list[tuple[SignalHit, int, int]]:
    """Sort (signal, confidence) high→low and attach a 1-based rank."""
    ordered = sorted(scored, key=lambda sc: sc[1], reverse=True)
    return [(sig, conf, i + 1) for i, (sig, conf) in enumerate(ordered)]
