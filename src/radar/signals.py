"""The 5 radar signal detectors.

Each detector takes a :class:`~src.radar.features.StockFeatures` snapshot and the
current market regime and returns a :class:`SignalHit` or ``None``. A stock may
emit several hits in one scan; each is scored and tracked independently.

Stop/target/RR are computed per signal so an alert is self-contained. Long-only
(the universe is traded long); rules mirror the swing lab's tested shapes
(``src/swing/signals.py``) where applicable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from src.radar.features import StockFeatures
from src.radar.regime import TRENDING_BEAR

# Signal type identifiers (also the DB ``signal_type`` values).
SMA7_REVERSION = "sma7_reversion"
VWAP_PULLBACK = "vwap_pullback"
RVOL_REVERSAL = "rvol_reversal"
ATR_BREAKOUT = "atr_breakout"
GAP_REVERSION = "gap_reversion"

# Tunables (fractions of price unless noted).
_SMA7_GAP_FRAC = 0.014        # ≥1.4% below SMA7 (EMCURE ₹20/₹1400 ≈ 1.4%)
_PULLBACK_BAND = 0.01         # within 1% above the 20EMA counts as a dip
_RVOL_MIN = 1.5
_ATR_EXPANSION_MIN = 1.3
_GAP_DOWN_MIN_PCT = -2.0      # open ≥2% below prev close
_STOP_ATR_MULT = 1.5
_TARGET_ATR_MULT = 3.0


def _rev_stop_atr() -> float:
    return float(os.environ.get("RADAR_REVERSION_STOP_ATR", "0.8"))


def _min_rr() -> float:
    return float(os.environ.get("RADAR_MIN_RR", "1.0"))


def _reversion_stop(entry: float, target: float, atr: float) -> float:
    """Stop for a close-target reversion trade.

    The stop distance is capped so the trade clears a minimum RR toward its
    (nearby) mean target — the old 1.5×ATR stop made RR ≈ 0.3 when the SMA7 was
    only ~1.5% away. Distance = min(REVERSION_STOP_ATR×ATR, reward / MIN_RR).
    """
    reward = target - entry
    by_atr = _rev_stop_atr() * atr if atr > 0 else entry * 0.02
    by_rr = reward / _min_rr() if _min_rr() > 0 else by_atr
    dist = min(by_atr, by_rr) if by_rr > 0 else by_atr
    return entry - dist


@dataclass(frozen=True)
class SignalHit:
    """A detected setup, self-contained for alerting and outcome tracking."""

    stock: str
    signal_type: str
    conditions: tuple[str, ...]
    entry_zone: tuple[float, float]
    stop: float
    target: float
    rr: float


def _rr(entry: float, stop: float, target: float) -> float:
    risk = entry - stop
    reward = target - entry
    return round(reward / risk, 2) if risk > 0 else 0.0


def _hit(
    f: StockFeatures, signal_type: str, conditions: list[str],
    entry: float, stop: float, target: float,
) -> Optional[SignalHit]:
    """Assemble a hit, rejecting degenerate stop/target geometry."""
    if not (stop < entry < target):
        return None
    rr = _rr(entry, stop, target)
    if rr <= 0:
        return None
    return SignalHit(
        stock=f.stock,
        signal_type=signal_type,
        conditions=tuple(conditions),
        entry_zone=(round(min(entry, f.price), 2), round(max(entry, f.price), 2)),
        stop=round(stop, 2),
        target=round(target, 2),
        rr=rr,
    )


def detect_sma7_reversion(f: StockFeatures, regime: str) -> Optional[SignalHit]:
    """Price stretched ≥ threshold below SMA7 — fade back toward the mean."""
    if f.sma7 <= 0:
        return None
    gap_frac = f.gap_to_sma7 / f.price
    if gap_frac > -_SMA7_GAP_FRAC:
        return None
    entry = f.price
    target = f.sma7                       # mean-revert back to SMA7
    stop = _reversion_stop(entry, target, f.atr)
    return _hit(
        f, SMA7_REVERSION,
        [f"Price {abs(gap_frac)*100:.1f}% below SMA7 (₹{f.sma7})",
         f"RSI {f.rsi}", "Target = SMA7 mean"],
        entry, stop, target,
    )


def detect_vwap_pullback(f: StockFeatures, regime: str) -> Optional[SignalHit]:
    """Dip to the 20EMA inside an uptrend (swing lab's pullback variant)."""
    uptrend = f.ema20 > f.ema50
    near_ema = f.price <= f.ema20 * (1.0 + _PULLBACK_BAND)
    not_falling_knife = f.rsi > 40.0
    if not (uptrend and near_ema and not_falling_knife):
        return None
    entry = f.price
    stop = entry - _STOP_ATR_MULT * f.atr if f.atr > 0 else entry * 0.97
    target = entry + _TARGET_ATR_MULT * f.atr if f.atr > 0 else entry * 1.05
    return _hit(
        f, VWAP_PULLBACK,
        ["Uptrend (EMA20 > EMA50)", f"Pulled back to EMA20 (₹{f.ema20})",
         f"RSI {f.rsi} (not oversold-broken)"],
        entry, stop, target,
    )


def detect_rvol_reversal(f: StockFeatures, regime: str) -> Optional[SignalHit]:
    """High relative volume + an oversold stretch — volume-backed reversal."""
    if f.rvol < _RVOL_MIN or f.rsi >= 35.0:
        return None
    entry = f.price
    stop = entry - _STOP_ATR_MULT * f.atr if f.atr > 0 else entry * 0.97
    target = entry + _TARGET_ATR_MULT * f.atr if f.atr > 0 else entry * 1.05
    return _hit(
        f, RVOL_REVERSAL,
        [f"RVOL {f.rvol}× (>{_RVOL_MIN}×)", f"RSI {f.rsi} (stretched down)",
         "Volume-backed reversal"],
        entry, stop, target,
    )


def detect_atr_breakout(f: StockFeatures, regime: str) -> Optional[SignalHit]:
    """Volatility expansion + close above prior high and VWAP (breakout)."""
    if f.atr_expansion < _ATR_EXPANSION_MIN:
        return None
    if not (f.price > f.prev_high and f.price > f.vwap):
        return None
    entry = f.price
    stop = entry - _STOP_ATR_MULT * f.atr if f.atr > 0 else entry * 0.97
    target = entry + _TARGET_ATR_MULT * f.atr if f.atr > 0 else entry * 1.05
    return _hit(
        f, ATR_BREAKOUT,
        [f"ATR expanding {f.atr_expansion}×", f"Close > prev high (₹{f.prev_high})",
         "Close > VWAP"],
        entry, stop, target,
    )


def detect_gap_reversion(f: StockFeatures, regime: str) -> Optional[SignalHit]:
    """Gapped down on the open but reclaimed above it — gap fill toward prev close."""
    if f.gap_pct > _GAP_DOWN_MIN_PCT:
        return None
    if f.price <= f.open:                  # must have reclaimed the open
        return None
    if f.prev_close <= f.price:            # room to fill toward prev close
        return None
    entry = f.price
    target = f.prev_close                  # fill the gap
    stop = _reversion_stop(entry, target, f.atr)
    return _hit(
        f, GAP_REVERSION,
        [f"Gapped {f.gap_pct}% down", "Reclaimed the open",
         f"Target = prev close (₹{f.prev_close})"],
        entry, stop, target,
    )


_DETECTORS: tuple[Callable[[StockFeatures, str], Optional[SignalHit]], ...] = (
    detect_sma7_reversion,
    detect_vwap_pullback,
    detect_rvol_reversal,
    detect_atr_breakout,
    detect_gap_reversion,
)


def detect(f: StockFeatures, regime: str) -> list[SignalHit]:
    """Run all detectors for one snapshot. Skips longs in a bear regime."""
    if regime == TRENDING_BEAR:
        return []
    return [h for det in _DETECTORS if (h := det(f, regime)) is not None]
