"""The scan pipeline: snapshots → regime → signals → scored, ranked hits.

Pure orchestration with no I/O side effects (no DB writes, no alerts) so it can
be reused by the CLI (`radar.py scan-now`) and the headless loop alike, and unit
tested by injecting a snapshot function.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from src.radar import scoring, signals
from src.radar.features import StockFeatures, build_snapshot, fetch_index_daily
from src.radar.regime import SIDEWAYS, breadth, current_regime
from src.radar.universe import MIN_AVG_TRADED_VALUE_CR, NIFTY, SYMBOLS

logger = logging.getLogger(__name__)


def _min_adtv_cr() -> float:
    """Liquidity floor (₹ crore). ``RADAR_MIN_ADTV_CR`` overrides the ₹100 Cr
    spec default — note the literal default excludes EMCURE (~₹30 Cr) and the
    smaller PSU names, so a lower floor is usually wanted for this universe."""
    return float(os.environ.get("RADAR_MIN_ADTV_CR", MIN_AVG_TRADED_VALUE_CR))

SnapshotFn = Callable[[str, Optional[pd.DataFrame]], Optional[StockFeatures]]


@dataclass(frozen=True)
class ScanResult:
    regime: str
    breadth: float
    # (signal, confidence, rank) sorted high→low by confidence
    ranked: list[tuple[signals.SignalHit, int, int]] = field(default_factory=list)
    snapshots: dict[str, StockFeatures] = field(default_factory=dict)
    illiquid: tuple[str, ...] = ()

    def above_gate(self) -> list[tuple[signals.SignalHit, int, int]]:
        return [r for r in self.ranked if r[1] > scoring.SCORE_GATE]


def run_scan(
    nifty_daily: Optional[pd.DataFrame] = None,
    symbols: tuple[str, ...] = SYMBOLS,
    snapshot_fn: SnapshotFn = build_snapshot,
) -> ScanResult:
    """Scan the universe once and return ranked, scored hits."""
    if nifty_daily is None:
        nifty_daily = fetch_index_daily(NIFTY)

    snapshots: dict[str, StockFeatures] = {}
    for sym in symbols:
        snap = snapshot_fn(sym, nifty_daily)
        if snap is not None:
            snapshots[sym] = snap

    breadth_pct = breadth([s.above_50dma for s in snapshots.values()])
    regime = current_regime(nifty_daily, breadth_pct) if nifty_daily is not None else SIDEWAYS

    min_adtv = _min_adtv_cr()
    scored: list[tuple[signals.SignalHit, int]] = []
    illiquid: list[str] = []
    for sym, snap in snapshots.items():
        # Liquidity gate uses ADTV already on the snapshot.
        if snap.adtv_cr < min_adtv:
            illiquid.append(sym)
            continue
        for hit in signals.detect(snap, regime):
            conf = scoring.confidence(snap, hit, regime)
            scored.append((hit, conf))

    return ScanResult(
        regime=regime,
        breadth=round(breadth_pct, 2),
        ranked=scoring.rank(scored),
        snapshots=snapshots,
        illiquid=tuple(illiquid),
    )
