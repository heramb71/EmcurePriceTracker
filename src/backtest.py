"""
Backtest engine for the intraday mean-reversion strategy.

Simulates every trading day using daily OHLCV (open/high/low/close).
Intraday hit-order is estimated by proximity to open price.
Overnight holds (drastic falls) look forward up to 4 days for NPLP recovery.

Usage:
    from src.backtest import run_backtest, print_report
    result = run_backtest(df_daily, capital=100_000, risk_rupees=4500)
    print_report(result)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from src.intraday import classify_7d_trend, compute_sma7_gap

logger = logging.getLogger(__name__)

# Fixed rupee targets
T1, T2, T3 = 10.0, 20.0, 25.0
DRASTIC_FALL_THRESHOLD = 15.0   # rupees below entry → trigger overnight hold
OVERNIGHT_HOLD_DAYS    = 4      # max days to wait for NPLP recovery


@dataclass
class Trade:
    date:       str
    trend:      str
    reason:     str
    entry:      float
    gap:        float
    sl:         float
    sl_diff:    float
    t1:         float
    t2:         float
    t3:         float
    qty:        int
    outcome:    str    # stop | t1 | t2 | t3 | square_off | nplp_recovered | overnight_loss
    exit_price: float
    pnl:        float
    high:       float
    low:        float
    close:      float


@dataclass
class BacktestResult:
    trades:       list[Trade] = field(default_factory=list)
    total_pnl:    float = 0.0
    win_rate:     float = 0.0
    profit_factor: float = 0.0
    expectancy:   float = 0.0
    max_drawdown: float = 0.0
    max_consec_losses: int = 0
    sharpe:       float = 0.0
    monthly:      dict = field(default_factory=dict)
    outcome_counts: dict = field(default_factory=dict)
    by_trend:     dict = field(default_factory=dict)
    by_reason:    dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Hit-order estimation from daily OHLC
# ─────────────────────────────────────────────────────────────────────────────

def _closer_to_open(price_a: float, price_b: float, open_: float) -> bool:
    """Return True if price_a is closer to the open than price_b."""
    return abs(price_a - open_) < abs(price_b - open_)


def _simulate_day(
    open_: float,
    high: float,
    low: float,
    close: float,
    entry: float,
    sl: float,
    t1: float,
    t2: float,
    t3: float,
    fwd_rows: list[dict],   # up to 4 next-day dicts for overnight logic
) -> tuple[str, float]:
    """
    Return (outcome, exit_price) using OHLC hit-order heuristic.
    """
    # Gap scenarios at open
    if open_ <= sl:
        return "stop", sl
    if open_ >= t3:
        return "t3", t3

    hit_sl  = low  <= sl
    hit_t1  = high >= t1
    hit_t2  = high >= t2
    hit_t3  = high >= t3

    # Resolve ambiguity by proximity to open
    if hit_sl and hit_t3:
        return ("t3", t3) if _closer_to_open(t3, sl, open_) else ("stop", sl)
    if hit_sl and hit_t2:
        return ("t2", t2) if _closer_to_open(t2, sl, open_) else ("stop", sl)
    if hit_sl and hit_t1:
        return ("t1", t1) if _closer_to_open(t1, sl, open_) else ("stop", sl)
    if hit_sl:
        return "stop", sl
    if hit_t3:
        return "t3", t3
    if hit_t2:
        return "t2", t2
    if hit_t1:
        return "t1", t1

    # Neither target nor stop — check for drastic fall (overnight hold)
    if close < entry - DRASTIC_FALL_THRESHOLD:
        for fwd in fwd_rows:
            if fwd["high"] >= entry:
                return "nplp_recovered", entry
        # Not recovered in 4 days — take loss at last available close
        return "overnight_loss", fwd_rows[-1]["close"] if fwd_rows else close

    return "square_off", close


# ─────────────────────────────────────────────────────────────────────────────
# Main backtest loop
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(
    df: pd.DataFrame,
    capital: float = 100_000.0,
    risk_rupees: float = 4_500.0,
    skip_downtrend: bool = True,
) -> BacktestResult:
    """
    Simulate the mean-reversion intraday strategy on `df` (daily OHLCV).

    Parameters
    ----------
    df             : DataFrame with columns open/high/low/close/volume + date.
    capital        : Rupees deployed per trade (default ₹1,00,000).
    risk_rupees    : Max loss per trade in rupees (default ₹4,500 = 4.5%).
    skip_downtrend : If True, skip entries when 7D trend is Downward.
    """
    df = df.reset_index(drop=True)
    n  = len(df)
    result = BacktestResult()

    for i in range(7, n):
        row   = df.iloc[i]
        open_ = float(row["open"])
        high  = float(row["high"])
        low   = float(row["low"])
        close = float(row["close"])
        date_ = str(row.get("date", ""))[:10]

        # ── 7D SMA gap ────────────────────────────────────────────────────────
        gap_data = compute_sma7_gap(open_, df.iloc[:i])
        gap      = gap_data["gap"]

        # ── 7D trend ─────────────────────────────────────────────────────────
        trend = classify_7d_trend(df.iloc[i - 7:i])

        # ── Entry conditions ─────────────────────────────────────────────────
        entry  = None
        reason = None

        if gap <= -20.0:
            if skip_downtrend and trend == "Downward":
                i += 1
                continue
            entry  = open_
            reason = "mean_reversion"
        elif trend == "Upward" and -5.0 <= gap <= 5.0:
            entry  = open_
            reason = "trend_entry"

        if entry is None:
            continue

        # ── Position sizing ───────────────────────────────────────────────────
        qty = int(capital / entry)
        if qty <= 0:
            continue

        sl_per = risk_rupees / qty
        sl     = round(entry - sl_per, 2)
        t1_p   = round(entry + T1, 2)
        t2_p   = round(entry + T2, 2)
        t3_p   = round(entry + T3, 2)

        # ── Forward rows for overnight hold ───────────────────────────────────
        fwd_rows = []
        for j in range(1, OVERNIGHT_HOLD_DAYS + 1):
            if i + j < n:
                r = df.iloc[i + j]
                fwd_rows.append({
                    "high":  float(r["high"]),
                    "close": float(r["close"]),
                })

        # ── Simulate day ──────────────────────────────────────────────────────
        outcome, exit_price = _simulate_day(
            open_, high, low, close, entry, sl, t1_p, t2_p, t3_p, fwd_rows
        )

        pnl = round((exit_price - entry) * qty, 2)

        result.trades.append(Trade(
            date=date_, trend=trend, reason=reason,
            entry=round(entry, 2), gap=round(gap, 1),
            sl=sl, sl_diff=round(sl_per, 2),
            t1=t1_p, t2=t2_p, t3=t3_p,
            qty=qty, outcome=outcome,
            exit_price=round(exit_price, 2), pnl=pnl,
            high=high, low=low, close=close,
        ))

    _compute_stats(result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────

def _compute_stats(result: BacktestResult) -> None:
    trades = result.trades
    if not trades:
        return

    pnls       = [t.pnl for t in trades]
    wins       = [p for p in pnls if p > 0]
    losses     = [p for p in pnls if p < 0]
    n          = len(pnls)

    result.total_pnl    = round(sum(pnls), 2)
    result.win_rate     = round(len(wins) / n * 100, 1)
    result.profit_factor = round(sum(wins) / abs(sum(losses)), 2) if losses else 999.0
    result.expectancy   = round(sum(pnls) / n, 2)

    # Max drawdown
    running_max = 0.0
    cum         = 0.0
    max_dd      = 0.0
    for p in pnls:
        cum        += p
        running_max = max(running_max, cum)
        max_dd      = min(max_dd, cum - running_max)
    result.max_drawdown = round(max_dd, 2)

    # Consecutive losses
    streak = max_streak = 0
    for p in pnls:
        if p < 0:
            streak    += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    result.max_consec_losses = max_streak

    # Sharpe (annualised, ~252 trading days)
    arr    = np.array(pnls)
    result.sharpe = round(float(arr.mean() / arr.std() * np.sqrt(252)), 2) if arr.std() > 0 else 0.0

    # Monthly breakdown
    monthly: dict = {}
    for t in trades:
        mo = t.date[:7]
        monthly.setdefault(mo, {"pnl": 0.0, "trades": 0, "wins": 0})
        monthly[mo]["pnl"]    += t.pnl
        monthly[mo]["trades"] += 1
        if t.pnl > 0:
            monthly[mo]["wins"] += 1
    result.monthly = {k: {**v, "pnl": round(v["pnl"], 2)} for k, v in sorted(monthly.items())}

    # Outcome counts
    from collections import Counter
    result.outcome_counts = dict(Counter(t.outcome for t in trades))

    # By trend
    for tr in ("Upward", "Downward", "Choppy"):
        sub = [t.pnl for t in trades if t.trend == tr]
        if sub:
            result.by_trend[tr] = {
                "count": len(sub),
                "win_rate": round(sum(1 for p in sub if p > 0) / len(sub) * 100, 1),
                "total_pnl": round(sum(sub), 2),
            }

    # By entry reason
    for r in ("mean_reversion", "trend_entry"):
        sub = [t.pnl for t in trades if t.reason == r]
        if sub:
            result.by_reason[r] = {
                "count": len(sub),
                "win_rate": round(sum(1 for p in sub if p > 0) / len(sub) * 100, 1),
                "total_pnl": round(sum(sub), 2),
            }


# ─────────────────────────────────────────────────────────────────────────────
# Report formatting
# ─────────────────────────────────────────────────────────────────────────────

def print_report(result: BacktestResult, ticker: str = "EMCURE") -> None:
    """Print a human-readable backtest report to stdout."""
    trades = result.trades
    if not trades:
        print("No trades generated.")
        return

    first, last = trades[0].date, trades[-1].date
    n = len(trades)

    print()
    print("=" * 62)
    print(f"  {ticker}.NS — Intraday Strategy Backtest")
    print(f"  {first}  →  {last}  ({n} trades)")
    print("=" * 62)

    # Core metrics
    print(f"\n{'Metric':<28}{'Value':>14}")
    print("-" * 42)
    _row = lambda k, v: print(f"  {k:<26}{v:>14}")
    _row("Total P&L",            f"₹{result.total_pnl:>+,.0f}")
    _row("Win rate",             f"{result.win_rate:.1f}%")
    _row("Profit factor",        f"{result.profit_factor:.2f}")
    _row("Expectancy / trade",   f"₹{result.expectancy:>+,.0f}")
    _row("Max drawdown",         f"₹{result.max_drawdown:>,.0f}")
    _row("Max consecutive losses", f"{result.max_consec_losses}")
    _row("Sharpe (annualised)",  f"{result.sharpe:.2f}")

    # Target hits
    oc = result.outcome_counts
    print()
    print("  Target breakdown:")
    for label, key in [("T3 +₹25", "t3"), ("T2 +₹20", "t2"), ("T1 +₹10", "t1"),
                        ("Stopped", "stop"), ("Square-off", "square_off"),
                        ("NPLP recovered", "nplp_recovered"), ("Overnight loss", "overnight_loss")]:
        cnt = oc.get(key, 0)
        pct = cnt / n * 100
        bar = "█" * int(pct / 3)
        print(f"    {label:<18} {cnt:>3} ({pct:4.0f}%)  {bar}")

    # By entry reason
    print()
    print("  By entry type:")
    for reason, stats in result.by_reason.items():
        print(f"    {reason:<18} {stats['count']:>3} trades  "
              f"win {stats['win_rate']:.0f}%  P&L ₹{stats['total_pnl']:>+,.0f}")

    # By trend
    print()
    print("  By 7D trend at entry:")
    for trend, stats in result.by_trend.items():
        print(f"    {trend:<12} {stats['count']:>3} trades  "
              f"win {stats['win_rate']:.0f}%  P&L ₹{stats['total_pnl']:>+,.0f}")

    # Monthly
    print()
    print("  Monthly P&L:")
    for mo, stats in result.monthly.items():
        pnl = stats["pnl"]
        bar_len = int(abs(pnl) / 400)
        bar = ("█" if pnl >= 0 else "░") * min(bar_len, 30)
        sign = "+" if pnl >= 0 else "-"
        wr   = round(stats["wins"] / stats["trades"] * 100) if stats["trades"] else 0
        print(f"    {mo}  {sign}₹{abs(pnl):>7,.0f}  ({stats['trades']:>2} trades, {wr:>2}% win)  {bar}")

    # Worst trades
    sorted_by_pnl = sorted(trades, key=lambda t: t.pnl)
    print()
    print("  Worst 5 trades:")
    for t in sorted_by_pnl[:5]:
        print(f"    {t.date}  entry ₹{t.entry:.0f}  gap {t.gap:+.0f}"
              f"  {t.outcome:<16}  P&L ₹{t.pnl:>+,.0f}")

    print()
    print("=" * 62)
    print()


def format_whatsapp_report(result: BacktestResult, ticker: str = "EMCURE") -> str:
    """Compact WhatsApp text summary of the backtest."""
    if not result.trades:
        return f"{ticker}.NS Backtest: no trades generated."

    first = result.trades[0].date
    last  = result.trades[-1].date
    n     = len(result.trades)
    oc    = result.outcome_counts

    lines = [
        f"📊 *{ticker}.NS — Backtest Report*",
        f"{first} → {last}  ({n} trades)",
        "",
        f"Total P&L:    ₹{result.total_pnl:>+,.0f}",
        f"Win rate:     {result.win_rate:.1f}%",
        f"Profit factor: {result.profit_factor:.2f}",
        f"Expectancy:   ₹{result.expectancy:>+,.0f}/trade",
        f"Max drawdown: ₹{result.max_drawdown:,.0f}",
        f"Max consec losses: {result.max_consec_losses}",
        "",
        "Target hits:",
        f"  T3 +₹25: {oc.get('t3', 0)} ({oc.get('t3', 0)/n*100:.0f}%)",
        f"  T2 +₹20: {oc.get('t2', 0)} ({oc.get('t2', 0)/n*100:.0f}%)",
        f"  T1 +₹10: {oc.get('t1', 0)} ({oc.get('t1', 0)/n*100:.0f}%)",
        f"  Stopped: {oc.get('stop', 0)} ({oc.get('stop', 0)/n*100:.0f}%)",
        f"  NPLP hold: {oc.get('nplp_recovered', 0)} recovered / "
        f"{oc.get('overnight_loss', 0)} loss",
        "",
        "Monthly highs/lows:",
    ]

    monthly_sorted = sorted(result.monthly.items(), key=lambda x: x[1]["pnl"])
    worst3 = monthly_sorted[:3]
    best3  = monthly_sorted[-3:][::-1]
    for mo, s in best3:
        lines.append(f"  {mo}: +₹{s['pnl']:,.0f} ✅")
    for mo, s in worst3:
        lines.append(f"  {mo}: ₹{s['pnl']:,.0f} ⚠️")

    return "\n".join(lines)
