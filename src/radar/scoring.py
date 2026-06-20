"""0–100 confidence score + per-family gating + per-scan ranking.

Two scoring profiles, because the families have different DNA:

* Momentum (VWAP pullback, ATR breakout) — wants trend confirmation, so it
  weights RVOL / VWAP-strength / ATR-expansion / RSI-up, anchored on
  ``src/swing/scanner.py``: RVOL 20 · SMA7 15 · VWAP 15 · ATR 15 · RSI 10 ·
  RS 10 · regime 15.

* Reversion (SMA7, RVOL reversal, gap) — for a mean-reversion trade *depth below
  the mean is the signal*, not volume/volatility confirmation (a calm dip is
  still a valid dip). So it is depth-weighted: below-SMA7 35 · oversold-RSI 20 ·
  regime 15 · below-VWAP 10 · RVOL 10 (bonus, not required) · RS 5 · ATR 5.

Gating is also per-family: momentum must clear the strict ``RADAR_SCORE_GATE``
(default 75); reversion clears a lower ``RADAR_REVERSION_GATE`` (default 55),
matching its high-win-rate / low-target edge so reliable dips aren't filtered out
by a momentum-tuned bar.
"""
from __future__ import annotations

import os

from src.radar.features import StockFeatures
from src.radar.regime import SIDEWAYS, TRENDING_BULL
from src.radar.signals import (
    ATR_BREAKOUT,
    GAP_REVERSION,
    RVOL_REVERSAL,
    SMA7_REVERSION,
    SignalHit,
    VWAP_PULLBACK,
)

# ── Momentum weights (sum 100) ──
W_RVOL = 20.0
W_SMA7 = 15.0
W_VWAP = 15.0
W_ATR = 15.0
W_RSI = 10.0
W_RS = 10.0
W_REGIME = 15.0

# ── Reversion weights (sum 100) — depth-dominant ──
WR_DEPTH = 35.0    # how far below SMA7
WR_RSI = 20.0      # how oversold
WR_REGIME = 15.0
WR_VWAP = 10.0
WR_RVOL = 10.0     # volume confirmation is a bonus, not a gate
WR_RS = 5.0
WR_ATR = 5.0

# Default gates (overridable via env at runtime).
SCORE_GATE = 75            # momentum
REVERSION_GATE = 55        # reversion

_REVERSION = {SMA7_REVERSION, RVOL_REVERSAL, GAP_REVERSION}
_MOMENTUM = {VWAP_PULLBACK, ATR_BREAKOUT}

# Normalisation spans.
_DIST_SPAN = 0.03        # 3% move → full momentum distance sub-score
_REV_DEPTH_SPAN = 0.025  # 2.5% below SMA7 → full reversion depth sub-score
_RSI_SPAN = 20.0


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _regime_alignment(regime: str, is_reversion: bool) -> float:
    """Regime-fit multiplier, oriented by signal family.

    Momentum setups want a trending-up tape (half credit when range-bound).
    Mean-reversion setups work *best* in a range and are also fine buying dips in
    an uptrend, so they get full credit in both SIDEWAYS and TRENDING_BULL.
    TRENDING_BEAR scores zero (long setups are filtered upstream anyway).
    """
    if regime == TRENDING_BULL:
        return 1.0
    if regime == SIDEWAYS:
        return 1.0 if is_reversion else 0.5
    return 0.0


def _reversion_confidence(f: StockFeatures, regime: str) -> float:
    below_sma7 = (-f.gap_to_sma7 / f.price) if f.price else 0.0
    below_vwap = ((f.vwap - f.price) / f.price) if f.price else 0.0
    depth_s = _clip01(below_sma7 / _REV_DEPTH_SPAN) * WR_DEPTH
    rsi_s = _clip01((50.0 - f.rsi) / _RSI_SPAN) * WR_RSI
    vwap_s = _clip01(below_vwap / _DIST_SPAN) * WR_VWAP
    rvol_s = _clip01((f.rvol - 1.0) / 1.0) * WR_RVOL
    rs_s = _clip01(f.rs20 / 0.05) * WR_RS
    atr_s = _clip01((f.atr_expansion - 1.0) / 0.5) * WR_ATR
    regime_s = _regime_alignment(regime, True) * WR_REGIME
    return depth_s + rsi_s + vwap_s + rvol_s + rs_s + atr_s + regime_s


def _momentum_confidence(f: StockFeatures, regime: str) -> float:
    below_sma7 = (-f.gap_to_sma7 / f.price) if f.price else 0.0
    below_vwap = ((f.vwap - f.price) / f.price) if f.price else 0.0
    rvol_s = _clip01((f.rvol - 1.0) / 1.0) * W_RVOL
    sma7_s = _clip01(-below_sma7 / _DIST_SPAN) * W_SMA7   # above SMA7 favoured
    vwap_s = _clip01(-below_vwap / _DIST_SPAN) * W_VWAP   # above VWAP favoured
    rsi_s = _clip01((f.rsi - 50.0) / _RSI_SPAN) * W_RSI
    atr_s = _clip01((f.atr_expansion - 1.0) / 0.5) * W_ATR
    rs_s = _clip01(f.rs20 / 0.05) * W_RS
    regime_s = _regime_alignment(regime, False) * W_REGIME
    return rvol_s + sma7_s + vwap_s + atr_s + rsi_s + rs_s + regime_s


def confidence(f: StockFeatures, signal: SignalHit, regime: str) -> int:
    """Confidence 0–100 for one (snapshot, signal) pair, by signal family."""
    if signal.signal_type in _REVERSION:
        total = _reversion_confidence(f, regime)
    else:
        total = _momentum_confidence(f, regime)
    return int(round(max(0.0, min(100.0, total))))


def momentum_gate() -> int:
    return int(os.environ.get("RADAR_SCORE_GATE", SCORE_GATE))


def reversion_gate() -> int:
    return int(os.environ.get("RADAR_REVERSION_GATE", REVERSION_GATE))


def gate_for(signal_type: str) -> int:
    """The confidence bar a signal must clear, by family."""
    return reversion_gate() if signal_type in _REVERSION else momentum_gate()


def passes_gate(signal_type: str, conf: int) -> bool:
    return conf > gate_for(signal_type)


def rank(scored: list[tuple[SignalHit, int]]) -> list[tuple[SignalHit, int, int]]:
    """Sort (signal, confidence) high→low and attach a 1-based rank."""
    ordered = sorted(scored, key=lambda sc: sc[1], reverse=True)
    return [(sig, conf, i + 1) for i, (sig, conf) in enumerate(ordered)]
