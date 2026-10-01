"""Entry-timing lab — does delaying the managed cycle's first entry help? (pure)

Replays the REAL ``managed_cycle.decide()`` over intraday bars, one decision per
bar close, with one extra knob: ``not_before`` — the earliest bar-close time of
day a NEW entry may be taken. Exits are never gated (a held position is managed
from the first bar of every session, like live). Everything else mirrors
production and ``apps/strategy_lab_intraday``:

* SMA7 / ATR14 / 7-day trend from PRIOR closed daily bars only.
* Fill at the bar close (what the 5-minute poll sees).
* While holding, ``day_high``/``day_low`` are the since-entry extremes of POLLED
  prices, seeded at the entry price (production ``high_since_entry``).
* Positions carry overnight (CNC). Live guards are honoured: the re-entry
  cooldown after any exit, no re-entry the same day after a stop-out, and the
  realized daily-loss cap.
* Costs: the ledger's ``round_trip_charges`` (statutory + DP debit).
"""
from __future__ import annotations

from datetime import time as dtime
from typing import Optional

import numpy as np
import pandas as pd

from src.emcure.intraday import classify_7d_trend
from src.emcure.managed_cycle import ManagedConfig, decide, resolve_stop
from src.shared.costs import round_trip_charges

# (label, not_before) — None = live behaviour (enter on the first poll that fires).
GATES: tuple[tuple[str, Optional[dtime]], ...] = (
    ("LIVE  from 09:15", None),
    ("only after 09:30", dtime(9, 30)),
    ("only after 09:45", dtime(9, 45)),
)


def session_context(daily: pd.DataFrame, session) -> Optional[dict]:
    """SMA7 / ATR14 / trend / recent closes from daily bars strictly BEFORE
    ``session``. ``daily`` needs ``d`` (date) and ``atr14`` columns."""
    prior = daily[daily["d"] < session]
    if len(prior) < 20:
        return None
    atr = float(prior["atr14"].iloc[-1])
    if not np.isfinite(atr) or atr <= 0:
        return None
    return {
        "sma7": float(prior["close"].tail(7).mean()),
        "atr14": atr,
        "trend_7d": classify_7d_trend(prior),
        "recent_closes": list(prior["close"].tail(3)),
    }


def add_atr14(daily: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``daily`` with ``atr14`` (simple 14-bar mean TR) and ``d`` columns."""
    out = daily.copy()
    h, l, c = out["high"], out["low"], out["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    out["d"] = pd.to_datetime(out["date"]).dt.date
    return out


def _entry_allowed(ts: pd.Timestamp, not_before: Optional[dtime], interval_min: int,
                   guard: dict, cfg: ManagedConfig) -> bool:
    """Timing gate + the live re-entry guards (cooldown, stop-out, loss cap)."""
    if not_before is not None and (ts + pd.Timedelta(minutes=interval_min)).time() < not_before:
        return False
    if guard["day"] == ts.date():
        if cfg.block_reentry_after_stop and guard["stopped"]:
            return False
        if cfg.max_daily_loss > 0 and -guard["realized"] >= cfg.max_daily_loss:
            return False
    last = guard["last_exit"]
    return last is None or (ts - last) >= pd.Timedelta(minutes=cfg.reentry_cooldown_min)


def _close_trade(pos: dict, exit_px: float, kind: str, ts: pd.Timestamp, guard: dict) -> dict:
    gross = (exit_px - pos["entry"]) * pos["qty"]
    charges = round_trip_charges(pos["entry"], exit_px, pos["qty"])
    net = round(gross - charges, 2)
    if guard["day"] != ts.date():
        guard.update(day=ts.date(), stopped=False, realized=0.0)
    guard["realized"] += net
    guard["stopped"] |= kind == "stop"
    guard["last_exit"] = ts
    return {"entry_ts": pos["ts"], "exit_ts": ts, "entry": pos["entry"], "exit": exit_px,
            "qty": pos["qty"], "kind": kind, "gross": round(gross, 2),
            "charges": charges, "net": net}


def replay(intra: pd.DataFrame, daily: pd.DataFrame, cfg: ManagedConfig,
           not_before: Optional[dtime], interval_min: int = 5) -> list[dict]:
    """Closed trades for one entry gate. ``intra`` has ``date`` (bar start, IST)
    and ``close``; ``daily`` is :func:`add_atr14` output."""
    trades: list[dict] = []
    pos: Optional[dict] = None
    guard = {"day": None, "stopped": False, "realized": 0.0, "last_exit": None}
    sessions = intra.assign(session=intra["date"].dt.date).groupby("session")
    for session, bars in sessions:
        ctx = session_context(daily, session)
        if ctx is None:
            continue
        for ts, close in zip(bars["date"], bars["close"].astype(float)):
            if pos is None:
                if not _entry_allowed(ts, not_before, interval_min, guard, cfg):
                    continue
                market = {"price": close, "day_high": close, "day_low": close,
                          "gap": close - ctx["sma7"], **ctx}
                if decide(None, market, cfg).action == "reenter":
                    sl = round(close - resolve_stop(cfg, ctx["atr14"]), 2)
                    pos = {"entry": close, "qty": cfg.qty, "sl": sl, "ts": ts,
                           "hi": close, "lo": close}
                continue
            pos["hi"], pos["lo"] = max(pos["hi"], close), min(pos["lo"], close)
            d = decide(pos, {"price": close, "day_high": pos["hi"], "day_low": pos["lo"]}, cfg)
            if d.action in ("sell", "exit_sl"):
                kind = "stop" if d.action == "exit_sl" else "target"
                # A stop gapped through (overnight / fast bar) fills at the polled
                # price, not the trigger — never assume a better fill than seen.
                px = min(float(d.price), close) if kind == "stop" else float(d.price)
                trades.append(_close_trade(pos, px, kind, ts, guard))
                pos = None
    return trades
