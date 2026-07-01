"""Cost-aware, one-position-at-a-time portfolio backtester for the swing lab.

Discipline baked in to avoid the look-ahead bugs that produced the discarded
"+₹1,496" artifact:
  - signal is computed on bar t (close); entry fills at bar t+1 OPEN
  - only ONE position is ever open across the whole universe
  - intrabar stop/target use bar Low/High; everything else fills at close
  - every trade nets Zerodha CNC charges (src.shared.costs) + a CDSL DP sell charge

Returns a BacktestResult with the full §6 metric set.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.shared.costs import compute_charges

from . import indicators as ind
from . import signals as sig
from .regime import TRENDING_BULL, regime_series
from .scanner import SCORE_GATE, score_frame
from .universe import NIFTY, avg_traded_value_cr, MIN_AVG_TRADED_VALUE_CR

# CDSL depository participant charge on the SELL leg (₹13.5 + 18% GST), per scrip.
DP_CHARGE = 15.93


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry: float
    exit: float
    qty: int
    days_held: int
    gross: float
    charges: float
    net: float
    reason: str


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    initial_capital: float = 0.0
    years: float = 0.0


def build_features(df: pd.DataFrame, nifty_close: pd.Series) -> pd.DataFrame:
    """Attach all indicator columns + relative strength to a raw OHLCV frame."""
    feat = df.copy()
    feat["ema20"] = ind.ema(feat["Close"], 20)
    feat["ema50"] = ind.ema(feat["Close"], 50)
    feat["rsi"] = ind.rsi(feat["Close"], 14)
    feat["atr"] = ind.atr(feat, 14)
    feat["vwap"] = ind.rolling_vwap(feat, 20)
    feat["rvol"] = ind.rvol(feat["Volume"], 20)
    feat["atr_exp"] = ind.atr_expansion(feat["atr"], 20)
    feat["prev_high"] = feat["High"].shift(1)
    stock_ret = ind.rolling_return(feat["Close"], 20)
    nifty_ret = ind.rolling_return(nifty_close.reindex(feat.index).ffill(), 20)
    feat["rs20"] = stock_ret - nifty_ret
    feat["score"] = score_frame(feat)
    feat["liquid"] = (
        (feat["Close"] * feat["Volume"]).rolling(20).mean() / 1e7
    ) >= MIN_AVG_TRADED_VALUE_CR
    return feat


def _size(equity: float, entry: float, atr_at_entry: float,
          risk_pct: float, risk_cap: float | None) -> int:
    risk_rupees = equity * risk_pct
    if risk_cap is not None:
        risk_rupees = min(risk_rupees, risk_cap)
    stop_distance = sig.STOP_ATR_MULT * atr_at_entry
    if stop_distance <= 0:
        return 0
    qty = math.floor(risk_rupees / stop_distance)
    qty = min(qty, math.floor(0.90 * equity / entry))
    return max(qty, 0)


def run(
    raw: dict[str, pd.DataFrame],
    nifty: pd.DataFrame,
    *,
    capital: float = 15000.0,
    variant: str = "pullback",
    use_score_gate: bool = True,
    score_gate: float = SCORE_GATE,
    risk_pct: float = 0.02,
    risk_cap: float | None = 300.0,
) -> BacktestResult:
    """Run the portfolio backtest. ``variant`` ∈ {pullback, breakout}."""
    entry_fn = sig.pullback_entry if variant == "pullback" else sig.breakout_entry
    use_ma_exits = variant != "pullback"  # MA-cross exits are self-defeating for dip-buys

    feats = {s: build_features(df, nifty["Close"]) for s, df in raw.items()}
    entries = {s: entry_fn(f) for s, f in feats.items()}
    regime = regime_series(nifty)

    dates = sorted(set.intersection(*[set(f.index) for f in feats.values()],
                                    set(regime.index)))
    dates = [d for d in dates if not pd.isna(regime.loc[d])]

    equity = capital
    position = None
    trades: list[Trade] = []
    eq_points: list[tuple[pd.Timestamp, float]] = []

    for i in range(1, len(dates)):
        today, prev = dates[i], dates[i - 1]

        if position is not None:
            f = feats[position["symbol"]]
            if today not in f.index:
                continue
            bar = f.loc[today]
            position["days_held"] += 1
            exit_now, reason, fill = sig.should_exit(
                bar, position["entry"], position["stop"], position["target"],
                position["days_held"], regime.loc[today], use_ma_exits,
            )
            if exit_now:
                trades.append(_close(position, today, fill, reason))
                equity += trades[-1].net
                position = None

        if position is None and regime.loc[prev] == TRENDING_BULL:
            pick = _select(feats, entries, prev, use_score_gate, score_gate)
            if pick is not None:
                f = feats[pick]
                if today in f.index:
                    entry_px = float(f.loc[today, "Open"])
                    atr_e = float(f.loc[prev, "atr"])
                    qty = _size(equity, entry_px, atr_e, risk_pct, risk_cap)
                    if qty >= 1 and not math.isnan(atr_e) and atr_e > 0:
                        position = {
                            "symbol": pick, "entry_date": today, "entry": entry_px,
                            "qty": qty, "days_held": 0,
                            "stop": entry_px - sig.STOP_ATR_MULT * atr_e,
                            "target": entry_px + sig.TARGET_ATR_MULT * atr_e,
                        }
        eq_points.append((today, equity))

    years = max((dates[-1] - dates[0]).days / 365.25, 1e-9) if len(dates) > 1 else 1e-9
    curve = pd.Series(dict(eq_points))
    return BacktestResult(trades, curve, capital, years)


def _select(feats, entries, date, use_gate, gate):
    candidates: dict[str, float] = {}
    for s, f in feats.items():
        if date not in f.index:
            continue
        if not bool(entries[s].loc[date]):
            continue
        if not bool(f.loc[date, "liquid"]):
            continue
        score = float(f.loc[date, "score"])
        if use_gate and score < gate:
            continue
        candidates[s] = score
    if not candidates:
        return None
    return max(candidates, key=candidates.get)


def _close(pos, exit_date, fill, reason) -> Trade:
    gross = (fill - pos["entry"]) * pos["qty"]
    charges = compute_charges(pos["entry"], fill, pos["qty"]) + DP_CHARGE
    return Trade(
        symbol=pos["symbol"], entry_date=pos["entry_date"], exit_date=exit_date,
        entry=pos["entry"], exit=fill, qty=pos["qty"], days_held=pos["days_held"],
        gross=round(gross, 2), charges=round(charges, 2),
        net=round(gross - charges, 2), reason=reason,
    )
