#!/usr/bin/env python3
"""Backtest: does a recency-weighted, market-adjusted hit-rate pick better daily
stocks than KittyBot's current score() — measured against a cheap, daily-only
proxy for "the pick actually moved."

Brainstormed 2026-08-09 (see _bmad-output/brainstorming/brainstorming-session-
2026-08-09-2210.md, Idea #4+6). First-pass validation only: this walks daily
history and checks whether each formula's #1-ranked stock went on to move >=2%
intraday on the SAME day it was picked for — it does not simulate the actual
opening-range breakout, entry, target, or stop (that needs 1-minute history and
is the second-wave extension flagged in the session's action plan). A stock's
own day-T bar is never used to rank it for day T — every decision uses only data
strictly before T, so there is no lookahead.

Usage:  python -m apps.kitty_score_backtest
        python -m apps.kitty_score_backtest --symbols TATAMOTORS,VEDL --days 400
        python -m apps.kitty_score_backtest --monte-carlo
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from src.kittybot import montecarlo, screener
from src.kittybot.config import KittyBotConfig
from src.radar.features import fetch_index_daily
from src.shared.data import fetch_daily

NIFTY = "^NSEI"
LOOKBACK = 60


def _fetch_universe(symbols: list[str], days: int) -> dict[str, pd.DataFrame]:
    data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = fetch_daily(sym, days=days)
        if df is not None and len(df) >= LOOKBACK + 20:
            data[sym] = df.sort_values("date").reset_index(drop=True)
        else:
            print(f"  ! {sym}: skipped (insufficient history)")
    return data


def _rank_for_day(data: dict[str, pd.DataFrame], nifty_hist: pd.DataFrame,
                  day: pd.Timestamp) -> tuple[str | None, str | None]:
    """Return ``(old_pick, new_pick)`` — the #1-scored symbol under each formula,
    using only data strictly before ``day`` (no lookahead)."""
    old_scores: dict[str, float] = {}
    new_scores: dict[str, float] = {}
    for sym, df in data.items():
        hist = df[df["date"] < day]
        if len(hist) < LOOKBACK:
            continue
        avg_range = screener.avg_range_pct(hist, LOOKBACK)
        old_hit = screener.either_hit_rate(hist, LOOKBACK)
        new_hit = screener.market_adjusted_hit_rate(hist, nifty_hist, LOOKBACK)
        old_scores[sym] = screener.score(old_hit, avg_range)
        new_scores[sym] = screener.score(new_hit, avg_range)
    old_pick = max(old_scores, key=old_scores.get) if old_scores else None
    new_pick = max(new_scores, key=new_scores.get) if new_scores else None
    return old_pick, new_pick


def _hit_on_day(df: pd.DataFrame, day: pd.Timestamp, threshold_pct: float = 2.0) -> bool | None:
    """Did this stock actually move >= threshold_pct intraday on ``day``?"""
    row = df[df["date"] == day]
    if row.empty:
        return None
    r = row.iloc[0]
    up = (r["high"] - r["open"]) / r["open"] * 100.0
    down = (r["open"] - r["low"]) / r["open"] * 100.0
    return bool(max(up, down) >= threshold_pct)


def _pct(hits: int, total: int) -> float:
    return round(hits / total * 100.0, 1) if total else 0.0


def _stats(rows: list[tuple]) -> dict:
    """Old-vs-new hit-rate stats for one slice of walked decision rows."""
    old_hits = sum(1 for r in rows if r[2])
    old_total = sum(1 for r in rows if r[2] is not None)
    new_hits = sum(1 for r in rows if r[4])
    new_total = sum(1 for r in rows if r[4] is not None)
    return {"old_hit_pct": _pct(old_hits, old_total), "old_n": old_total,
            "new_hit_pct": _pct(new_hits, new_total), "new_n": new_total}


def _summarize(label: str, rows: list[tuple]) -> None:
    s = _stats(rows)
    print(f"{label:<26}current {s['old_hit_pct']:>5.1f}% (n={s['old_n']:<4})  "
          f"vs  new {s['new_hit_pct']:>5.1f}% (n={s['new_n']})")


def run(symbols: list[str], days: int, warmup: int = 120) -> dict:
    print(f"Fetching {len(symbols)} symbols + Nifty ({days}d history) ...", flush=True)
    data = _fetch_universe(symbols, days)
    nifty = fetch_index_daily(NIFTY, days=days)
    if nifty is None or len(data) < 2:
        print("Not enough data to backtest (need Nifty + at least 2 qualifying symbols).")
        return {}
    nifty = nifty.sort_values("date").reset_index(drop=True)

    decision_dates = nifty["date"].tolist()[warmup:]
    rows: list[tuple] = []
    for day in decision_dates:
        nifty_hist = nifty[nifty["date"] < day]
        if len(nifty_hist) < LOOKBACK:
            continue
        old_pick, new_pick = _rank_for_day(data, nifty_hist, day)
        old_hit = _hit_on_day(data[old_pick], day) if old_pick else None
        new_hit = _hit_on_day(data[new_pick], day) if new_pick else None
        rows.append((day, old_pick, old_hit, new_pick, new_hit))

    if not rows:
        print("No decision days walked — increase --days or reduce --warmup.")
        return {}

    print(f"\nWalked {len(rows)} decision days ({rows[0][0].date()} → {rows[-1][0].date()})\n")
    _summarize("Full period:", rows)

    mid = len(rows) // 2
    print()
    _summarize("In-sample (1st half):", rows[:mid])
    _summarize("Out-of-sample (2nd half):", rows[mid:])

    return {
        "full": _stats(rows),
        "in_sample": _stats(rows[:mid]),
        "out_of_sample": _stats(rows[mid:]),
        "out_sample_old_hits": [bool(r[2]) for r in rows[mid:] if r[2] is not None],
        "out_sample_new_hits": [bool(r[4]) for r in rows[mid:] if r[4] is not None],
    }


def _print_monte_carlo(label: str, result: montecarlo.MonteCarloResult) -> None:
    print(f"  {label}: 10th/50th/90th pctile return "
          f"{result.pctile_10_return_pct:+.1f}% / {result.pctile_50_return_pct:+.1f}% / "
          f"{result.pctile_90_return_pct:+.1f}%  |  median/90th-pctile drawdown "
          f"{result.median_max_drawdown_pct:.1f}% / {result.pctile_90_max_drawdown_pct:.1f}%")
    print(f"    {result.verdict}")


def _run_monte_carlo(result: dict, n_paths: int, ruin_threshold_pct: float) -> None:
    """Resample both formulas' out-of-sample outcomes into equity-curve risk
    stats — the distribution-of-outcomes check an average-case-only backtest
    comparison can't answer on its own. Proxy stats, not a real trade replay —
    see src.kittybot.montecarlo's module docstring."""
    old_hits = result.get("out_sample_old_hits") or []
    new_hits = result.get("out_sample_new_hits") or []
    if not old_hits or not new_hits:
        print("\nMonte Carlo: skipped (not enough out-of-sample decision days).")
        return
    print(f"\nMonte Carlo ({n_paths} paths, ruin = equity below "
          f"{ruin_threshold_pct:.0f}% of start) — proxy stats from configured R-multiples, "
          "not a real trade replay:")
    _print_monte_carlo("current score()",
                       montecarlo.summarize(old_hits, n_paths=n_paths,
                                            ruin_threshold_pct=ruin_threshold_pct, seed=0))
    _print_monte_carlo("market-adjusted score()",
                       montecarlo.summarize(new_hits, n_paths=n_paths,
                                            ruin_threshold_pct=ruin_threshold_pct, seed=0))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtest current score() vs recency+market-adjusted score()")
    parser.add_argument("--symbols", type=str, default=None,
                        help="comma-separated universe override (default: kitty fallback universe)")
    parser.add_argument("--days", type=int, default=500, help="daily history depth to fetch")
    parser.add_argument("--warmup", type=int, default=120,
                        help="trailing days required before the first decision")
    parser.add_argument("--monte-carlo", action="store_true",
                        help="resample out-of-sample outcomes into equity-curve risk stats")
    parser.add_argument("--mc-paths", type=int, default=5000,
                        help="number of Monte Carlo paths to resample")
    parser.add_argument("--ruin-threshold-pct", type=float, default=50.0,
                        help="equity %% of starting capital counted as ruin")
    args = parser.parse_args()

    symbols = (args.symbols.split(",") if args.symbols
              else list(KittyBotConfig().fallback_universe))
    result = run(symbols, args.days, args.warmup)
    if args.monte_carlo and result:
        _run_monte_carlo(result, args.mc_paths, args.ruin_threshold_pct)
    return 0


if __name__ == "__main__":
    sys.exit(main())
