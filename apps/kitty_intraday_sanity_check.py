#!/usr/bin/env python3
"""SANITY CHECK ONLY — NOT statistically meaningful.

Tests the two intraday-dependent brainstorm ideas (see
_bmad-output/brainstorming/brainstorming-session-2026-08-09-2210.md, Idea #1
"Measured-Move Target" and Idea #5 "Breakout Confirmation") against real
intraday data. yfinance hard-caps 1-minute history at ~8 days per request —
there is no way to get more 1-minute history from this data source. 5-minute
bars buy roughly 60 days instead, at the cost of entry/exit timing precision
(a breakout is only ever detected on a bar boundary, so up to ~5 minutes late
at that granularity) — a real trade-off, not a free lunch. 15-minute bars are
too coarse for this system specifically (the opening range itself IS 15
minutes, so a 15m bar leaves nothing to build a range from). Even with 5m
bars this is still nowhere near a real backtest; it exists to catch an
obvious implementation bug and get a slightly less tiny anecdote, not to
decide whether these ideas work.

For each symbol × day it can fetch, builds the 15-minute opening range, then
walks the rest of that day's bars once to find the first breakout, and
simulates four variants from that same breakout point:

  baseline        — enter immediately, current fixed target/stop (Pick defaults)
  measured_target — enter immediately, target = breakout price +/- range height
  confirmed_entry — wait ~3 minutes for the level to hold, current target/stop
  both            — confirmed entry AND the measured-move target together

Usage:  python -m apps.kitty_intraday_sanity_check
        python -m apps.kitty_intraday_sanity_check --interval 5m --days 60
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import time as dtime

import pandas as pd

from src.kittybot.config import KittyBotConfig
from src.kittybot.opening_range import LONG, breakout_trigger, build_opening_range
from src.kittybot.picks import Pick
from src.shared.data import fetch_intraday

_CONFIRM_WINDOW_MINUTES = 3.0  # target confirmation duration; converted to bars per interval
_MAX_DAYS_BY_INTERVAL = {"1m": 7, "5m": 60, "15m": 60}  # yfinance's real, tested ceilings
VARIANTS = ("baseline", "measured_target", "confirmed_entry", "both")


def _interval_minutes(interval: str) -> int:
    match = re.match(r"(\d+)m$", interval)
    if not match:
        raise ValueError(f"unsupported interval {interval!r} (expected e.g. '1m', '5m')")
    return int(match.group(1))


def _fetch_days(symbol: str, interval: str, days: int) -> pd.DataFrame | None:
    df = fetch_intraday(symbol, interval=interval, days=days)
    if df is None or df.empty:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    df["session_date"] = df["date"].dt.date
    return df


def _opening_baseline_volume(df: pd.DataFrame, cfg: KittyBotConfig) -> float:
    """Crude in-sample volume baseline: mean opening-window volume across every
    fetched day for this symbol (a stand-in for the real 20-day trailing average,
    which needs far more history than this data source will ever hand over)."""
    open_end = (dtime(9, 15 + cfg.opening_range_minutes) if cfg.opening_range_minutes < 45
               else dtime(10, cfg.opening_range_minutes - 45))
    t = df["date"].dt.time
    window = df[(t >= cfg.observe_start_t) & (t < open_end)]
    per_day = window.groupby("session_date")["volume"].sum()
    return float(per_day.mean()) if not per_day.empty else 0.0


def _simulate_day(symbol: str, day_bars: pd.DataFrame, cfg: KittyBotConfig,
                  avg_volume: float, interval_minutes: int) -> list[dict]:
    open_end = (dtime(9, 15 + cfg.opening_range_minutes) if cfg.opening_range_minutes < 45
               else dtime(10, cfg.opening_range_minutes - 45))
    t = day_bars["date"].dt.time
    opening = day_bars[(t >= cfg.observe_start_t) & (t < open_end)]
    rest = day_bars[t >= open_end].reset_index(drop=True)
    if opening.empty or rest.empty:
        return []

    bars = [{"high": r.high, "low": r.low, "volume": r.volume} for r in opening.itertuples()]
    or_range = build_opening_range(bars, avg_volume=avg_volume)
    if or_range is None:
        return []

    pick = Pick(symbol=symbol)  # no screener history available in this tiny window
    trig_idx = None
    trigger = None
    for i, row in rest.iterrows():
        trig = breakout_trigger(pick, or_range, row["close"], row["volume"],
                                cfg.breakout_volume_multiple)
        if trig is not None:
            trig_idx, trigger = i, trig
            break
    if trigger is None:
        return []

    confirm_bars = max(1, round(_CONFIRM_WINDOW_MINUTES / interval_minutes))
    results = []
    for variant in VARIANTS:
        result = _run_variant(variant, rest, trig_idx, trigger, or_range, cfg, pick, confirm_bars)
        if result is not None:
            results.append({"symbol": symbol, "day": day_bars["session_date"].iloc[0],
                            "variant": variant, **result})
    return results


def _run_variant(variant: str, rest: pd.DataFrame, trig_idx: int, trigger,
                 or_range, cfg: KittyBotConfig, pick: Pick, confirm_bars: int) -> dict | None:
    confirmed = "confirmed" in variant or variant == "both"
    measured = "measured" in variant or variant == "both"

    entry_idx = trig_idx
    if confirmed:
        window = rest.iloc[trig_idx:trig_idx + 1 + confirm_bars]
        if len(window) < 2:
            return None  # not enough bars left today to confirm
        level = or_range.high if trigger.direction == LONG else or_range.low
        holds = ((window["close"] >= level).all() if trigger.direction == LONG
                else (window["close"] <= level).all())
        if not holds:
            return {"entry": None, "reason": "FAKEOUT_AVOIDED", "pnl_pct": 0.0}
        entry_idx = window.index[-1]

    entry_price = float(rest.iloc[entry_idx]["close"])
    if measured:
        height = or_range.width
        target = entry_price + height if trigger.direction == LONG else entry_price - height
        stop_pct = pick.suggested_stop_pct
        stop = (entry_price * (1 - stop_pct / 100.0) if trigger.direction == LONG
               else entry_price * (1 + stop_pct / 100.0))
    else:
        target = (entry_price * (1 + pick.suggested_target_pct / 100.0)
                  if trigger.direction == LONG
                  else entry_price * (1 - pick.suggested_target_pct / 100.0))
        stop = (entry_price * (1 - pick.suggested_stop_pct / 100.0)
               if trigger.direction == LONG
               else entry_price * (1 + pick.suggested_stop_pct / 100.0))

    reason, exit_price = _walk_to_exit(rest.iloc[entry_idx + 1:], trigger.direction,
                                       target, stop, cfg.hard_exit_t)
    if exit_price is None:
        return {"entry": entry_price, "reason": reason, "pnl_pct": None}
    pnl_pct = ((exit_price - entry_price) / entry_price * 100.0 if trigger.direction == LONG
              else (entry_price - exit_price) / entry_price * 100.0)
    return {"entry": entry_price, "reason": reason, "pnl_pct": round(pnl_pct, 3)}


def _walk_to_exit(bars_after: pd.DataFrame, direction: str, target: float, stop: float,
                  hard_exit_t: dtime) -> tuple[str, float | None]:
    """Returns ``(reason, exit_price)``; ``exit_price`` is ``None`` when the
    fetch window ran out before any outcome was observed — genuinely unknown,
    not a win or a loss, and must never be treated as either."""
    for _, bar in bars_after.iterrows():
        if bar["date"].time() >= hard_exit_t:
            return "TIME", float(bar["close"])
        if direction == LONG:
            if bar["high"] >= target:
                return "TARGET", target
            if bar["low"] <= stop:
                return "STOP", stop
        else:
            if bar["low"] <= target:
                return "TARGET", target
            if bar["high"] >= stop:
                return "STOP", stop
    return "NO_DATA", None


def run(symbols: list[str], interval: str, days: int) -> None:
    cfg = KittyBotConfig()
    interval_minutes = _interval_minutes(interval)
    all_results: list[dict] = []
    for symbol in symbols:
        df = _fetch_days(symbol, interval, days)
        if df is None:
            print(f"  ! {symbol}: no intraday data")
            continue
        avg_volume = _opening_baseline_volume(df, cfg)
        n_days = 0
        for _, day_bars in df.groupby("session_date"):
            n_days += 1
            all_results.extend(_simulate_day(symbol, day_bars, cfg, avg_volume, interval_minutes))
        print(f"  ✓ {symbol:<12} {n_days} trading days fetched")

    if not all_results:
        print("\nNo breakouts fired in the available window — nothing to compare.")
        return

    res = pd.DataFrame(all_results)
    print(f"\n⚠️  SANITY CHECK ONLY — {res['day'].nunique()} distinct trading days, "
          f"{len(res[res['variant'] == 'baseline'])} baseline breakouts. Not statistically meaningful.\n")
    print(f"{'variant':<18}{'n':>5}{'wins':>7}{'avoided':>9}{'no_data':>9}{'avg pnl%':>10}")
    for variant in VARIANTS:
        v = res[res["variant"] == variant]
        avoided = int((v["reason"] == "FAKEOUT_AVOIDED").sum())
        no_data = int((v["reason"] == "NO_DATA").sum())
        # Resolved trades only — NO_DATA means the fetch window ran out before
        # any real outcome was observed, and must never count as a win or loss.
        resolved = v[~v["reason"].isin(("FAKEOUT_AVOIDED", "NO_DATA"))]
        wins = int((resolved["pnl_pct"] > 0).sum())
        avg_pnl = resolved["pnl_pct"].mean() if not resolved.empty else 0.0
        print(f"{variant:<18}{len(v):>5}{wins:>7}{avoided:>9}{no_data:>9}{avg_pnl:>+10.2f}")

    print("\nPer-trade detail:")
    print(res[["symbol", "day", "variant", "reason", "pnl_pct"]].to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Intraday sanity check — NOT a real backtest")
    parser.add_argument("--symbols", type=str, default=None,
                        help="comma-separated universe override (default: kitty fallback universe)")
    parser.add_argument("--interval", type=str, default="1m", choices=list(_MAX_DAYS_BY_INTERVAL),
                        help="bar granularity — 1m is most precise but capped at ~7 days; "
                             "5m trades precision for ~60 days of history")
    parser.add_argument("--days", type=int, default=None,
                        help=f"history depth (default: yfinance's real ceiling per interval, "
                             f"{_MAX_DAYS_BY_INTERVAL})")
    args = parser.parse_args()
    symbols = (args.symbols.split(",") if args.symbols
              else list(KittyBotConfig().fallback_universe))
    days = args.days if args.days is not None else _MAX_DAYS_BY_INTERVAL[args.interval]
    run(symbols, args.interval, days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
