"""Point-in-time replay of the radar over recent market data (research only).

Reconstructs each hourly snapshot using ONLY data available at that moment — no
look-ahead: daily indicators (SMA7/RSI/ATR/EMA/prev-close) come from prior
completed sessions, while price/VWAP/gap/RVOL come from the current day up to the
replay hour. It then runs the exact production modules (regime → signals →
scoring → gate) and, for gated hits, evaluates the realized forward path over the
remaining available bars (target-vs-stop, MFE/MAE).

Not wired into the service; a manual review tool. Run:
    python radar_replay.py [num_days] [min_adtv_cr]
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import pandas as pd

from src.data import fetch_daily, fetch_intraday
from src.indicators import compute_atr, compute_avg_volume, compute_ema, compute_rsi
from src.intraday import compute_sma7
from src.radar import scoring, signals, tracker
from src.radar.alert_format import signal_label
from src.radar.features import StockFeatures, _avg_atr, _return, fetch_index_daily
from src.radar.regime import breadth, current_regime
from src.radar.universe import NIFTY, SYMBOLS, adtv_cr

INTERVAL = "60m"
INTRADAY_DAYS = 30
BARS_PER_DAY = 7  # 09:15..15:15 hourly

# Confidence gate — honours RADAR_SCORE_GATE so an aggressive profile (e.g. 65)
# can be replayed exactly as the service would apply it.
GATE = int(os.environ.get("RADAR_SCORE_GATE", scoring.SCORE_GATE))


def _intraday(sym: str) -> pd.DataFrame | None:
    df = fetch_intraday(sym, INTERVAL, INTRADAY_DAYS)
    if df is None or df.empty:
        return None
    df = df.copy()
    df["dt"] = pd.to_datetime(df["date"])
    df["d"] = df["dt"].dt.date
    return df.sort_values("dt").reset_index(drop=True)


def _daily_slice(df_daily: pd.DataFrame, before) -> pd.DataFrame:
    """Daily bars strictly before date ``before`` (prior completed sessions)."""
    return df_daily[pd.to_datetime(df_daily["date"]).dt.date < before]


def snapshot_at(
    sym: str,
    df_daily: pd.DataFrame,
    intra_day: pd.DataFrame,
    day,
    upto_idx: int,
    nifty_slice: pd.DataFrame | None,
) -> StockFeatures | None:
    """Reconstruct a point-in-time snapshot at bar ``upto_idx`` of ``day``."""
    prior = _daily_slice(df_daily, day)
    if len(prior) < 50:
        return None
    bars = intra_day.iloc[: upto_idx + 1]
    if bars.empty:
        return None

    price = float(bars["close"].iloc[-1])
    prev_close = float(prior["close"].iloc[-1])
    prev_high = float(prior["high"].iloc[-1])
    open_ = float(bars["open"].iloc[0])

    sma7 = compute_sma7(prior)
    rsi = compute_rsi(prior["close"])
    atr = compute_atr(prior)
    avg_atr = _avg_atr(prior)
    atr_expansion = round(atr / avg_atr, 2) if avg_atr > 0 else 1.0

    avg_vol = compute_avg_volume(prior, days=20)
    cum_vol = int(bars["volume"].sum())
    frac = (upto_idx + 1) / BARS_PER_DAY
    rvol = round((cum_vol / frac) / avg_vol, 2) if avg_vol > 0 and frac > 0 else 0.0

    ema20 = compute_ema(prior["close"], 20)
    ema50 = compute_ema(prior["close"], 50)
    dma50 = float(prior["close"].tail(50).mean())

    typ = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vwap = float((typ * bars["volume"]).sum() / bars["volume"].sum()) if bars["volume"].sum() else price

    gap_pct = round((open_ - prev_close) / prev_close * 100, 2) if prev_close else 0.0
    rs20 = _return(prior, 20) - (_return(nifty_slice, 20) if nifty_slice is not None and len(nifty_slice) > 20 else 0.0)

    return StockFeatures(
        stock=sym, price=round(price, 2), prev_close=round(prev_close, 2),
        open=round(open_, 2), sma7=round(sma7, 2),
        gap_to_sma7=round(price - sma7, 2), vwap=round(vwap, 2), rsi=rsi,
        atr=atr, atr_expansion=atr_expansion, rvol=rvol, ema20=ema20,
        ema50=ema50, prev_high=round(prev_high, 2), gap_pct=gap_pct,
        rs20=round(rs20, 4), adtv_cr=round(adtv_cr(prior), 1),
        above_50dma=price > dma50,
    )


def _forward_bars(intra: pd.DataFrame, after_dt) -> list[dict]:
    fwd = intra[intra["dt"] > after_dt]
    return [{"high": float(r.high), "low": float(r.low), "close": float(r.close)}
            for r in fwd.itertuples()]


def main() -> None:
    num_days = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    min_adtv = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0

    print(f"Loading data… (universe={','.join(SYMBOLS)}, interval={INTERVAL})")
    daily = {s: fetch_daily(s, days=160) for s in SYMBOLS}
    intra = {s: _intraday(s) for s in SYMBOLS}
    nifty_daily = fetch_index_daily(NIFTY, days=160)

    # Test window = last `num_days` trading dates present in the intraday data.
    any_intra = next(d for d in intra.values() if d is not None)
    all_days = sorted(set(any_intra["d"]))
    test_days = all_days[-num_days:]
    print(f"Replay window: {test_days[0]} → {test_days[-1]}  "
          f"({len(test_days)} sessions, {min_adtv:.0f}Cr liquidity floor)\n")

    fired: list[dict] = []

    for day in test_days:
        # Regime for the day uses data through the PRIOR close (no look-ahead).
        nifty_slice = _daily_slice(nifty_daily, day) if nifty_daily is not None else None
        above = []
        for s in SYMBOLS:
            ds = _daily_slice(daily[s], day) if daily[s] is not None else None
            if ds is not None and len(ds) >= 50:
                above.append(float(ds["close"].iloc[-1]) > float(ds["close"].tail(50).mean()))
        regime = current_regime(nifty_slice, breadth(above)) if nifty_slice is not None else "SIDEWAYS"

        day_bars = {s: (intra[s][intra[s]["d"] == day].reset_index(drop=True)
                        if intra[s] is not None else pd.DataFrame()) for s in SYMBOLS}
        n_bars = max((len(b) for b in day_bars.values()), default=0)

        print(f"═══ {day}  ({day.strftime('%a')})   regime={regime} ═══")
        for h in range(n_bars):
            ts_label = None
            hour_hits = []
            for s in SYMBOLS:
                b = day_bars[s]
                if h >= len(b):
                    continue
                ts = b["dt"].iloc[h]
                ts_label = ts.strftime("%H:%M")
                snap = snapshot_at(s, daily[s], b, day, h, nifty_slice)
                if snap is None or snap.adtv_cr < min_adtv:
                    continue
                for hit in signals.detect(snap, regime):
                    conf = scoring.confidence(snap, hit, regime)
                    hour_hits.append((snap, hit, conf, ts))
            if hour_hits:
                hour_hits.sort(key=lambda x: x[2], reverse=True)
                print(f"  {ts_label}")
                for snap, hit, conf, ts in hour_hits:
                    gate = "ALERT" if conf > GATE else "     "
                    print(f"    [{gate}] {snap.stock:<9} {signal_label(hit.signal_type):<26} "
                          f"conf={conf:>3} px=₹{snap.price:<8.1f} "
                          f"SL ₹{hit.stop:.1f} / T ₹{hit.target:.1f} RR {hit.rr:.1f}")
                    if conf > GATE:
                        fwd = _forward_bars(intra[snap.stock], ts)
                        price, mfe, mae, outcome = tracker.evaluate_window(
                            snap.price, hit.stop, hit.target, fwd)
                        fired.append({
                            "ts": ts, "stock": snap.stock, "sig": hit.signal_type,
                            "conf": conf, "outcome": outcome, "mfe": mfe, "mae": mae,
                        })
        print()

    _summary(fired)


def _summary(fired: list[dict]) -> None:
    print("═══════════════ GATED-ALERT SUMMARY (forward path over remaining week) ═══════════════")
    if not fired:
        print("No signals cleared the confidence gate during the replay window.")
        return
    wins = sum(1 for f in fired if f["outcome"] == "WIN")
    losses = sum(1 for f in fired if f["outcome"] == "LOSS")
    neutral = sum(1 for f in fired if f["outcome"] in ("NEUTRAL", None))
    print(f"Total gated alerts: {len(fired)}   WIN={wins}  LOSS={losses}  "
          f"NEUTRAL/open={neutral}")
    decided = wins + losses
    if decided:
        print(f"Win rate (decided): {wins/decided*100:.0f}%   "
              f"(target-before-stop over remaining bars)")
    print("-" * 90)
    for f in fired:
        mfe = f"{f['mfe']:+.1f}" if f["mfe"] is not None else "  —"
        mae = f"{f['mae']:+.1f}" if f["mae"] is not None else "  —"
        print(f"  {f['ts'].strftime('%m-%d %H:%M')}  {f['stock']:<9} "
              f"{signal_label(f['sig']):<26} conf={f['conf']:>3}  "
              f"{str(f['outcome'] or 'OPEN'):<8} MFE={mfe} MAE={mae}")


if __name__ == "__main__":
    main()
