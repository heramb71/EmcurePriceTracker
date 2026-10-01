#!/usr/bin/env python3
"""Checkpoint-timing lab — does waiting for more of the open help either bot?

Two questions, both answered on ~60 days of real 5-minute bars (yfinance's cap),
NET of charges, reusing the production decision code:

  1. KittyBot — 15-min opening range (live) vs 30-min range vs 15-min range that
     must still hold at 09:45. Full day replayed: pre-market screener ranking on
     prior daily bars → gap filter → strongest breakout → 2:1 plan → breakeven
     ratchet → 15:10 exit. MIS intraday costs. (src/kittybot/orb_lab.py)
  2. EMCURE managed cycle — entries allowed from the first poll (live) vs only
     after 09:30 vs only after 09:45. Real decide(), CNC ledger costs, live
     re-entry guards, ManagedConfig.from_env() so the SERVER's .env is replayed.
     (src/emcure/entry_timing_lab.py)

A variant "beats live" only with ≥20 trades AND higher expectancy AND higher
profit factor. ~60 sessions is thin evidence: a pass earns a paper-trading
trial, not a live switch.

Run:  python -m apps.checkpoint_timing_lab              # both
      python -m apps.checkpoint_timing_lab --only kitty # or --only emcure
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd
from dotenv import load_dotenv

from src.emcure.entry_timing_lab import GATES, add_atr14, replay
from src.emcure.managed_cycle import ManagedConfig
from src.kittybot.config import load_config
from src.kittybot.orb_lab import VARIANTS, Candidate, build_kitty, simulate_day
from src.shared.data import fetch_daily, fetch_intraday
from src.shared.trade_stats import beats, summarize

INTERVAL, INTERVAL_MIN, INTRADAY_DAYS = "5m", 5, 60
DAILY_DAYS = 400          # screener needs 60 sessions of lookback before the window
SESSION_MINUTES = 375     # 09:15–15:30, matches kittybot.marketdata
EMCURE = "EMCURE"


def _row(label: str, s: dict, live: dict | None) -> str:
    if not s["n"]:
        return f"  {label:<26} no trades"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    verdict = "" if live is None else ("  ✅ beats live" if beats(s, live) else "  ✗")
    return (f"  {label:<26} n={s['n']:>3}  win={s['win_pct']:>5.1f}%  PF={pf:>5}  "
            f"exp=₹{s['expectancy']:>7,.0f}  net=₹{s['net']:>9,.0f}  "
            f"maxDD=₹{s['max_dd']:>7,.0f}{verdict}")


def _load_kitty_data(symbols: list[str]) -> tuple[dict, dict]:
    daily, intra = {}, {}
    for sym in symbols:
        d, i = fetch_daily(sym, days=DAILY_DAYS), fetch_intraday(sym, INTERVAL, INTRADAY_DAYS)
        if d is None or d.empty or i is None or i.empty:
            print(f"  ! {sym}: no data — skipped")
            continue
        d = d.assign(d=pd.to_datetime(d["date"]).dt.date)
        daily[sym], intra[sym] = d, i.assign(session=i["date"].dt.date)
    return daily, intra


def _candidates(session, daily: dict, intra: dict, cfg) -> list[Candidate]:
    prior = {s: df[df["d"] < session].drop(columns="d") for s, df in daily.items()}
    out = []
    for pick in build_kitty(prior, cfg):
        bars = intra[pick.symbol]
        day_bars = bars[bars["session"] == session].reset_index(drop=True)
        hist = prior[pick.symbol]
        if day_bars.empty or hist.empty:
            continue
        per_min = float(hist["volume"].tail(20).mean()) / SESSION_MINUTES
        out.append(Candidate(pick, float(hist["close"].iloc[-1]), per_min, day_bars))
    return out


def run_kitty() -> None:
    cfg = load_config()
    print(f"\nKittyBot — fetching {len(cfg.fallback_universe)} symbols "
          f"({DAILY_DAYS}d daily + {INTRADAY_DAYS}d {INTERVAL})...", flush=True)
    daily, intra = _load_kitty_data(list(cfg.fallback_universe))
    sessions = sorted({s for df in intra.values() for s in df["session"].unique()})
    trades = {v.name: [] for v in VARIANTS}
    for session in sessions:
        cands = _candidates(session, daily, intra, cfg)
        for v in VARIANTS:
            if (t := simulate_day(cands, v, cfg, INTERVAL_MIN)) is not None:
                trades[v.name].append(t)

    print(f"\nKITTYBOT ORB TIMING — {len(sessions)} sessions, one trade/day max, "
          f"NET of MIS charges (capital ₹{cfg.capital:,.0f}, risk {cfg.risk_per_trade_pct}%)")
    stats = {name: summarize([t["net"] for t in ts]) for name, ts in trades.items()}
    live = stats[VARIANTS[0].name]
    for v in VARIANTS:
        print(_row(v.name, stats[v.name], None if v is VARIANTS[0] else live))
    for v in VARIANTS:
        reasons = pd.Series([t["reason"] for t in trades[v.name]]).value_counts().to_dict()
        print(f"    {v.name:<26} exits: {reasons}")


def run_emcure() -> None:
    cfg = ManagedConfig.from_env()
    print(f"\nEMCURE — fetching {DAILY_DAYS}d daily + {INTRADAY_DAYS}d {INTERVAL}...", flush=True)
    daily, intra = fetch_daily(EMCURE, days=DAILY_DAYS), fetch_intraday(EMCURE, INTERVAL, INTRADAY_DAYS)
    if daily is None or daily.empty or intra is None or intra.empty:
        print("  ! EMCURE: no data")
        return
    daily = add_atr14(daily)
    print(f"\nEMCURE MANAGED-CYCLE ENTRY GATE — {intra['date'].dt.date.nunique()} sessions, "
          f"NET of CNC charges (gap ₹{cfg.reentry_gap:g} / SL ₹{cfg.sl_rupees:g} / "
          f"targets {cfg.targets} / qty {cfg.qty} / regime {cfg.regime_filter})")
    stats = {label: summarize([t["net"] for t in replay(intra, daily, cfg, nb, INTERVAL_MIN)])
             for label, nb in GATES}
    live = stats[GATES[0][0]]
    for i, (label, _) in enumerate(GATES):
        print(_row(label, stats[label], None if i == 0 else live))


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Checkpoint-timing comparison lab")
    parser.add_argument("--only", choices=("kitty", "emcure"), default=None)
    args = parser.parse_args()
    if args.only in (None, "kitty"):
        run_kitty()
    if args.only in (None, "emcure"):
        run_emcure()
    print("\n⚠️  ~60 sessions only. '✅ beats live' = n≥20 AND higher expectancy AND "
          "higher PF — earns a paper trial, not a live switch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
