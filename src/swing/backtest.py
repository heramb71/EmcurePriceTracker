"""
One-position portfolio backtester for the swing system.

This is the validation gate. It simulates the bot exactly as it would trade:
  - at most ONE open position across the whole universe;
  - scan only when flat AND the NIFTY regime is bull; among symbols signalling
    that day, take the highest-RVOL candidate (the rank proxy);
  - enter at the NEXT session's open (no look-ahead — a real EOD bot can't fill at
    the signal-day close);
  - manage with an ATR stop/target, optional moving-average exit, regime-flip exit,
    and a max-hold time stop (stop checked first each day — conservative);
  - book P&L NET of Zerodha delivery costs incl. the DP charge.

After an exit it resumes scanning from the next session, so winners and losers are
sequenced realistically and the equity curve reflects true capital constraints.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from src.costs import compute_charges
from src.swing.regime import is_bull
from src.swing.signals import Strategy


@dataclass(frozen=True)
class BacktestConfig:
    capital: float = 15_000.0
    deploy_frac: float = 0.90       # keep ≥10% cash
    risk_pct: float = 0.02
    risk_cap: float = 300.0         # ₹ max risk per trade
    min_rr: float = 2.0             # skip setups below this reward:risk
    include_dp: bool = True         # apply the flat DP sell charge


@dataclass
class Trade:
    symbol: str
    entry_date: date
    exit_date: date
    entry: float
    exit_price: float
    qty: int
    stop: float
    target: float
    outcome: str        # stop | target | ma_exit | regime_exit | time
    gross: float
    charges: float
    net: float
    bars_held: int


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    config: Optional[BacktestConfig] = None
    start: Optional[date] = None
    end: Optional[date] = None
    # populated by _metrics
    n: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    cagr: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consec_losses: int = 0
    sharpe: float = 0.0
    trades_per_week: float = 0.0
    by_symbol: dict = field(default_factory=dict)
    by_outcome: dict = field(default_factory=dict)
    monthly: dict = field(default_factory=dict)


class _Sym:
    """Column arrays + date→row index for one symbol (fast inner loop)."""

    def __init__(self, df: pd.DataFrame, entry: pd.Series, ma_cols: tuple[str, ...]):
        self.date = list(df["date"])
        self.open = df["open"].to_numpy(float)
        self.high = df["high"].to_numpy(float)
        self.low = df["low"].to_numpy(float)
        self.close = df["close"].to_numpy(float)
        self.atr = df["atr"].to_numpy(float)
        self.rvol = df["rvol"].to_numpy(float)
        # Quality score (0–100) if the scanner ran; else zeros (gate/rank inert).
        self.score = (df["score"].to_numpy(float) if "score" in df.columns
                      else np.zeros(len(df)))
        self.entry = entry.to_numpy(bool)
        self.ma = {c: df[c].to_numpy(float) for c in ma_cols if c in df.columns}
        self.idx = {d: i for i, d in enumerate(self.date)}
        self.n = len(self.close)


def run(
    prepared: dict[str, pd.DataFrame],
    regime: dict[date, str],
    strategy: Strategy,
    cfg: BacktestConfig = BacktestConfig(),
    start: Optional[date] = None,
    end: Optional[date] = None,
    min_score: float = 0.0,
) -> BacktestResult:
    """Simulate `strategy` over the prepared universe within [start, end].

    `min_score` gates entries to setups whose scanner score exceeds it (0 = no
    gate). Requires a 'score' column on the prepared frames (see scanner)."""
    ma_cols = (strategy.alt_exit_ma,) if strategy.alt_exit_ma else ()
    syms = {s: _Sym(df, strategy.entry(df), ma_cols) for s, df in prepared.items()}

    union = sorted({d for s in syms.values() for d in s.date
                    if (start is None or d >= start) and (end is None or d <= end)})
    result = BacktestResult(config=cfg, start=union[0] if union else None,
                            end=union[-1] if union else None)

    ptr = 0
    while ptr < len(union):
        d = union[ptr]
        if not is_bull(regime.get(d)):
            ptr += 1
            continue
        pick = _best_candidate(syms, d, min_score)
        if pick is None:
            ptr += 1
            continue
        sym, i = pick
        trade = _simulate_trade(syms[sym], sym, i, strategy, cfg, regime)
        if trade is None:
            ptr += 1
            continue
        result.trades.append(trade)
        ptr = _resume_after(union, ptr, trade.exit_date)

    _metrics(result)
    return result


def _best_candidate(
    syms: dict[str, _Sym], d: date, min_score: float = 0.0
) -> Optional[tuple[str, int]]:
    """Best symbol signalling an entry on date d, or None.

    Ranks by quality score (falls back to RVOL when scores are inert/zero) and
    drops any candidate whose score does not clear `min_score`."""
    best: Optional[tuple[str, int]] = None
    best_key = -1.0
    for sym, s in syms.items():
        i = s.idx.get(d)
        if i is None or not s.entry[i]:
            continue
        if s.score[i] <= min_score and min_score > 0:
            continue
        key = s.score[i] if s.score[i] > 0 else (s.rvol[i] if np.isfinite(s.rvol[i]) else 0.0)
        if key > best_key:
            best, best_key = (sym, i), key
    return best


def _simulate_trade(
    s: _Sym, sym: str, i: int, strat: Strategy, cfg: BacktestConfig,
    regime: dict[date, str],
) -> Optional[Trade]:
    """Enter at open[i+1], manage to an exit; None if untradeable (sizing/gap)."""
    if i + 1 >= s.n:
        return None
    entry = s.open[i + 1]
    a = s.atr[i]
    if not np.isfinite(a) or a <= 0 or entry <= 0:
        return None
    stop = entry - strat.atr_stop * a
    target = entry + strat.atr_target * a
    stop_dist = entry - stop
    if stop_dist <= 0 or (target - entry) / stop_dist < cfg.min_rr - 1e-9:
        return None

    deployable = cfg.capital * cfg.deploy_frac
    risk = min(cfg.capital * cfg.risk_pct, cfg.risk_cap)
    qty = min(int(risk // stop_dist), int(deployable // entry))
    if qty < 1:
        return None

    alt = strat.alt_exit_ma
    outcome = exit_px = exit_date = None
    last = min(i + 1 + strat.max_hold, s.n)
    for j in range(i + 1, last):
        dj = s.date[j]
        if s.low[j] <= stop:
            outcome, exit_px = "stop", stop
        elif s.high[j] >= target:
            outcome, exit_px = "target", target
        elif alt and alt in s.ma and s.close[j] < s.ma[alt][j]:
            outcome, exit_px = "ma_exit", s.close[j]
        elif not is_bull(regime.get(dj)):
            outcome, exit_px = "regime_exit", s.close[j]
        if outcome:
            exit_date = dj
            break
    if outcome is None:
        j = last - 1
        outcome, exit_px, exit_date = "time", s.close[j], s.date[j]

    gross = (exit_px - entry) * qty
    charges = compute_charges(entry, exit_px, qty, include_dp=cfg.include_dp)
    return Trade(
        symbol=sym, entry_date=s.date[i + 1], exit_date=exit_date,
        entry=round(entry, 2), exit_price=round(exit_px, 2), qty=qty,
        stop=round(stop, 2), target=round(target, 2), outcome=outcome,
        gross=round(gross, 2), charges=charges, net=round(gross - charges, 2),
        bars_held=j - (i + 1) + 1,
    )


def _resume_after(union: list[date], ptr: int, exit_date: date) -> int:
    """First union index strictly after exit_date (so we don't re-enter mid-trade)."""
    for k in range(ptr + 1, len(union)):
        if union[k] > exit_date:
            return k
    return len(union)


def _metrics(r: BacktestResult) -> None:
    trades = r.trades
    r.n = len(trades)
    if not trades:
        return
    nets = np.array([t.net for t in trades])
    wins, losses = nets[nets > 0], nets[nets <= 0]
    cap = r.config.capital

    r.win_rate = round(100 * len(wins) / r.n, 1)
    r.avg_win = round(float(wins.mean()), 1) if len(wins) else 0.0
    r.avg_loss = round(float(losses.mean()), 1) if len(losses) else 0.0
    r.profit_factor = round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else 99.0
    r.expectancy = round(float(nets.mean()), 1)
    r.net_pnl = round(float(nets.sum()), 0)
    r.return_pct = round(100 * nets.sum() / cap, 1)

    equity = cap + np.cumsum(nets)
    peak = np.maximum.accumulate(equity)
    r.max_drawdown_pct = round(float(((peak - equity) / peak).max() * 100), 1)
    r.sharpe = round(float(nets.mean() / nets.std()), 2) if nets.std() > 0 else 0.0

    streak = mx = 0
    for x in nets:
        streak = streak + 1 if x <= 0 else 0
        mx = max(mx, streak)
    r.max_consec_losses = mx

    span_days = max((trades[-1].exit_date - trades[0].entry_date).days, 1)
    years = span_days / 365.25
    r.cagr = round(float(((equity[-1] / cap) ** (1 / years) - 1) * 100), 1)
    r.trades_per_week = round(r.n / (span_days / 7), 2)

    r.by_symbol = dict(Counter(t.symbol for t in trades))
    r.by_outcome = dict(Counter(t.outcome for t in trades))
    monthly: dict[str, float] = {}
    for t in trades:
        mo = t.exit_date.isoformat()[:7]
        monthly[mo] = round(monthly.get(mo, 0.0) + t.net, 0)
    r.monthly = dict(sorted(monthly.items()))


def print_report(r: BacktestResult, title: str = "") -> None:
    """Human-readable summary to stdout."""
    if not r.n:
        print(f"{title or 'Backtest'}: no trades."); return
    print(f"\n{'=' * 60}\n  {title or 'Swing Backtest'}   {r.start} → {r.end}\n{'=' * 60}")
    rows = [
        ("Trades", f"{r.n}  (~{r.trades_per_week}/wk)"),
        ("Win rate", f"{r.win_rate}%"),
        ("Avg win / loss", f"₹{r.avg_win} / ₹{r.avg_loss}"),
        ("Profit factor", f"{r.profit_factor}"),
        ("Expectancy/trade", f"₹{r.expectancy}"),
        ("Net P&L", f"₹{r.net_pnl:,.0f}  ({r.return_pct}% on ₹{r.config.capital:,.0f})"),
        ("CAGR", f"{r.cagr}%"),
        ("Max drawdown", f"{r.max_drawdown_pct}%"),
        ("Max consec losses", f"{r.max_consec_losses}"),
        ("Per-trade Sharpe", f"{r.sharpe}"),
    ]
    for k, v in rows:
        print(f"  {k:<20}{v}")
    print(f"  By outcome: {r.by_outcome}")
    print(f"  By symbol:  {r.by_symbol}")
