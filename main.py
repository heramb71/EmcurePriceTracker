#!/usr/bin/env python3
"""
Emcure Pharmaceuticals — Intraday Swing Trader Dashboard
Run: python main.py
     TICKER=RELIANCE python main.py
"""

# Must be set before any sklearn/joblib import to prevent loky multiprocessing
# segfault on macOS ARM (Apple Silicon) with Python 3.13
import os

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import logging
import time
from datetime import datetime

from dotenv import load_dotenv
from rich.live import Live

load_dotenv()

TICKER              = os.getenv("TICKER", "EMCURE")
REFRESH_SECONDS     = int(os.getenv("REFRESH_SECONDS", "300"))
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM  = os.getenv("TWILIO_WHATSAPP_FROM", "")
TWILIO_WHATSAPP_TO    = os.getenv("TWILIO_WHATSAPP_TO", "")

# Capital in rupees — configurable via .env
CAPITAL             = float(os.getenv("CAPITAL", "100000"))
# Max rupee risk per trade (e.g. 4.5% of ₹1L = ₹4500)
RISK_RUPEES         = float(os.getenv("RISK_RUPEES", "4500"))
# Legacy percentage-based risk kept for Supertrend strategy sizing
RISK_PCT            = float(os.getenv("RISK_PCT", "1.0"))
MAX_DAILY_LOSS_PCT  = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from src.data       import fetch_daily, fetch_intraday, get_latest_quote, fetch_live_quote
from src.intraday   import (
    compute_sma7_gap,
    classify_7d_trend,
    compute_orb,
    entry_signal,
    rupee_targets,
    time_exit_action,
)
from src.news_monitor import NewsMonitor
from src.predictor   import (
    predict_trade,
    format_pre_open_briefing,
    format_post_open_briefing,
)
from src.indicators import (
    compute_rsi,
    compute_macd,
    compute_bollinger,
    compute_ema,
    compute_atr,
    compute_vwap,
    compute_avg_volume,
)
from src.pivots import classic_pivots, camarilla_pivots, atr_levels
from src.sentiment import load_sentiment_model, fetch_news, aggregate_sentiment
from src.scoring import (
    detect_regime,
    compute_score,
    compute_ml_target_probabilities,
    compute_intraday_probabilities,
)
from src.alerts import (
    send_alert,
    send_whatsapp_alert,
    format_alert,
    format_whatsapp_alert,
    should_alert,
    format_position_open_alert,
    format_partial_alert,
    format_position_close_alert,
)
from src.dashboard import build_dashboard
from src.supertrend import compute_supertrend
from src.strategy import (
    check_buy_gate,
    compute_position_size,
    manage_position,
    unrealised_pnl,
)
from src.state import (
    load_state,
    save_state,
    reset_session_if_new_day,
    open_position,
    book_partial,
    close_position,
    check_circuit_breaker,
)


def _refresh(ticker: str, news_snapshot: dict | None = None) -> dict:
    """Fetch all market data and compute every signal. Returns a flat result dict."""
    df_daily = fetch_daily(ticker)
    if df_daily is None or df_daily.empty:
        return {}

    df_intraday = fetch_intraday(ticker, days=20)

    # Prefer live quote (fresher price during market hours); fall back to daily
    quote = fetch_live_quote(ticker) or get_latest_quote(df_daily)
    close = df_daily["close"]

    # Indicators
    rsi = compute_rsi(close)
    macd_line, macd_signal, macd_hist = compute_macd(close)
    bb_upper, bb_mid, bb_lower = compute_bollinger(close)
    ema20 = compute_ema(close, 20)
    ema50 = compute_ema(close, 50)
    atr = compute_atr(df_daily)
    vwap = compute_vwap(df_intraday) if df_intraday is not None else 0.0
    avg_volume = compute_avg_volume(df_daily)

    indicators = {
        "rsi": rsi,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "ema20": ema20,
        "ema50": ema50,
        "atr": atr,
        "vwap": vwap,
        "avg_volume": avg_volume,
    }

    # Pivots — use previous day's OHLC
    prev = df_daily.iloc[-2] if len(df_daily) > 1 else df_daily.iloc[-1]
    pivots = classic_pivots(
        float(prev["high"]), float(prev["low"]), float(prev["close"])
    )
    cam = camarilla_pivots(
        float(prev["high"]), float(prev["low"]), float(prev["close"])
    )
    atr_lvls = atr_levels(quote["price"], atr)

    # Sentiment
    articles = fetch_news()
    sentiment = aggregate_sentiment(articles)

    # Market regime + combined score
    regime = detect_regime(df_daily)
    score_result = compute_score(
        quote=quote,
        pivots=pivots,
        cam=cam,
        atr_lvls=atr_lvls,
        rsi=rsi,
        macd_hist=macd_hist,
        vwap=vwap,
        ema20=ema20,
        ema50=ema50,
        sentiment=sentiment,
        avg_volume=avg_volume,
        regime=regime,
    )

    # Build today's feature vector for ML-conditioned probabilities
    price = quote["price"]
    bb_std = indicators["bb_upper"] - indicators["bb_mid"]  # bb_mid = sma20, upper = +2σ
    bb_band = bb_std * 2  # total band width = 4σ
    bb_pct_today = float(
        max(0.0, min(1.0, (price - (indicators["bb_mid"] - bb_std * 2)) / bb_band))
        if bb_band > 0
        else 0.5
    )
    vol_ratio_today = min(3.0, quote["volume"] / avg_volume) if avg_volume > 0 else 1.0
    pct5 = df_daily["close"].pct_change(5).iloc[-1]
    mom_5d_today = float(pct5 * 100) if not (pct5 != pct5) else 0.0  # NaN guard

    today_features = {
        "rsi": rsi,
        "macd_hist": macd_hist,
        "bb_pct": bb_pct_today,
        "above_ema20": 1 if price > ema20 else 0,
        "ema_cross": 1 if ema20 > ema50 else 0,
        "vol_ratio": vol_ratio_today,
        "mom_5d": mom_5d_today,
    }

    # ML-conditioned swing target probabilities (logistic regression on last 250 bars)
    target_probs = compute_ml_target_probabilities(
        df_daily, today_features, [2.0, 5.0, 7.0, 10.0], stop_pct=2.0
    )

    # Historical hit-rate for intraday targets (stop = -0.5%)
    intraday_probs = compute_intraday_probabilities(
        df_intraday, [0.5, 1.0, 1.5], stop_pct=0.5
    )

    # ──────── Supertrend Strategy ────────
    st_df = compute_supertrend(df_daily, period=10, multiplier=3.0)
    if not st_df.empty:
        st_last = {
            "supertrend": float(st_df["supertrend"].iloc[-1] or 0.0),
            "direction": int(st_df["direction"].iloc[-1] or -1),
            "atr": float(st_df["atr"].iloc[-1] or 0.0),
        }
    else:
        st_last = {"supertrend": 0.0, "direction": -1, "atr": 0.0}

    last_candle = {
        "open": float(df_daily["open"].iloc[-1]),
        "high": float(df_daily["high"].iloc[-1]),
        "low": float(df_daily["low"].iloc[-1]),
        "close": float(df_daily["close"].iloc[-1]),
    }

    buy_signal = check_buy_gate(quote, indicators, st_last, last_candle, regime)
    sizing = (
        compute_position_size(CAPITAL, RISK_PCT, quote["price"], atr)
        if buy_signal["triggered"]
        else None
    )

    state = load_state()
    state = reset_session_if_new_day(state)
    halted, halted_reason = check_circuit_breaker(state, CAPITAL, MAX_DAILY_LOSS_PCT)

    events: list[tuple[str, dict]] = []
    position = state.get("position")

    if position:
        action = manage_position(
            position, quote["price"], st_last["supertrend"], st_last["direction"]
        )
        if action:
            if action["action"] == "exit_partial":
                state, partial_pnl = book_partial(state, action["price"])
                events.append(
                    (
                        "partial",
                        {
                            "price": action["price"],
                            "pnl": partial_pnl,
                            "position": dict(state["position"]),
                        },
                    )
                )
            elif action["action"] == "exit_full":
                state, total_pnl = close_position(state, action["price"], action["reason"])
                events.append(
                    (
                        "close",
                        {
                            "trade": state["journal"][-1],
                            "reason": action["reason"],
                        },
                    )
                )
    elif not halted and buy_signal["triggered"] and sizing:
        state = open_position(state, ticker, sizing, atr)
        events.append(("open", {"sizing": sizing, "buy_signal": buy_signal}))

    save_state(state)

    position_now = state.get("position")
    pnl_unr = unrealised_pnl(position_now, quote["price"]) if position_now else 0.0

    # ──────── Intraday strategy signals (SMA7 gap + trend + ORB) ────────────
    price       = quote["price"]
    sma7_gap    = compute_sma7_gap(price, df_daily)
    trend_7d    = classify_7d_trend(df_daily)
    orb         = compute_orb(df_intraday)
    intra_sig   = entry_signal(price, sma7_gap, trend_7d, orb)
    rupee_lvls  = (
        rupee_targets(price, CAPITAL, RISK_RUPEES)
        if intra_sig["action"] in ("BUY", "STRONG_BUY")
        else None
    )
    time_action = time_exit_action(position_now, price)

    # ──────── Trade confidence predictor ─────────────────────────────────────
    session_state = state.get("session", {})
    prior_losses  = int(session_state.get("consecutive_losses", 0))
    qty_for_pred  = int(CAPITAL / price) if price > 0 else 0
    trade_pred    = predict_trade(
        gap          = sma7_gap["gap"],
        atr          = atr,
        prior_losses = prior_losses,
        qty          = qty_for_pred,
        risk_per_trade = RISK_RUPEES,
    )

    # Pass news sentiment into the snapshot for predictor context
    news_sent_label = (news_snapshot or {}).get("label", "Neutral")
    news_sent_score = (news_snapshot or {}).get("avg_score", 0.0)

    return {
        "quote":          quote,
        "pivots":         pivots,
        "cam":            cam,
        "atr_lvls":       atr_lvls,
        "indicators":     indicators,
        "sentiment":      sentiment,
        "score_result":   score_result,
        "target_probs":   target_probs,
        "intraday_probs": intraday_probs,
        "buy_signal":     buy_signal,
        "sizing":         sizing,
        "strategy_state": state,
        "pnl_unrealised": pnl_unr,
        "halted_reason":  halted_reason if halted else "",
        "strategy_events": events,
        # Intraday strategy
        "sma7_gap":         sma7_gap,
        "trend_7d":         trend_7d,
        "orb":              orb,
        "intra_signal":     intra_sig,
        "rupee_levels":     rupee_lvls,
        "time_action":      time_action,
        "trade_pred":       trade_pred,
        "prior_losses":     prior_losses,
        "news_snapshot":    news_snapshot or {},
        "news_sent_label":  news_sent_label,
        "news_sent_score":  news_sent_score,
    }


def main() -> None:
    print(f"\n🚀 Starting {TICKER} Intraday Dashboard…")
    print(f"   Capital: ₹{CAPITAL:,.0f}  Risk/trade: ₹{RISK_RUPEES:,.0f}")
    print(f"   Refreshing every {REFRESH_SECONDS}s. Press Ctrl+C to exit.\n")

    # Load FinBERT once before the loop — it can take 30–60 s on first run
    sentiment_model = load_sentiment_model()

    # Start 24-hour news monitor in background
    news_monitor = NewsMonitor(sentiment_model=sentiment_model)
    news_monitor.start()

    last_alerted: dict = {}
    wa_ready = all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                    TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO])

    with Live(screen=True, refresh_per_second=1) as live:
        while True:
            news_snap = news_monitor.snapshot()
            data = _refresh(TICKER, news_snapshot=news_snap)
            last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # ── Scheduled pre-open briefing (9:00 AM) ────────────────────────
            now_t = datetime.now()
            if wa_ready and now_t.hour == 9 and now_t.minute == 0:
                pre_key = f"pre_open_{now_t.date()}"
                if pre_key not in last_alerted:
                    q         = data.get("quote", {})
                    s7        = data.get("sma7_gap", {})
                    briefing  = format_pre_open_briefing(
                        ticker         = TICKER,
                        price          = q.get("prev_close") or q.get("price", 0),
                        sma7           = s7.get("sma7", 0),
                        trend_7d       = data.get("trend_7d", "Unknown"),
                        atr            = data.get("indicators", {}).get("atr", 30),
                        capital        = CAPITAL,
                        risk_rupees    = RISK_RUPEES,
                        prior_losses   = data.get("prior_losses", 0),
                        sentiment_label= data.get("news_sent_label", "Neutral"),
                        sentiment_score= data.get("news_sent_score", 0.0),
                        now            = now_t,
                    )
                    send_whatsapp_alert(
                        TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                        TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO,
                        briefing,
                    )
                    last_alerted[pre_key] = now_t

            # ── Scheduled post-open update (9:20 AM) ─────────────────────────
            if wa_ready and now_t.hour == 9 and now_t.minute == 20:
                post_key = f"post_open_{now_t.date()}"
                if post_key not in last_alerted:
                    q        = data.get("quote", {})
                    s7       = data.get("sma7_gap", {})
                    post_msg = format_post_open_briefing(
                        ticker        = TICKER,
                        open_price    = q.get("open", q.get("price", 0)),
                        current_price = q.get("price", 0),
                        sma7          = s7.get("sma7", 0),
                        orb           = data.get("orb", {}),
                        atr           = data.get("indicators", {}).get("atr", 30),
                        capital       = CAPITAL,
                        risk_rupees   = RISK_RUPEES,
                        prior_losses  = data.get("prior_losses", 0),
                        now           = now_t,
                    )
                    send_whatsapp_alert(
                        TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                        TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO,
                        post_msg,
                    )
                    last_alerted[post_key] = now_t

            # ── Dispatch intraday entry signal alert ──────────────────────────
            intra_sig = data.get("intra_signal", {})
            if intra_sig.get("action") in ("BUY", "STRONG_BUY"):
                sig_key  = f"intra_{intra_sig['action']}"
                last_t   = last_alerted.get(sig_key)
                too_soon = (
                    last_t and (datetime.now() - last_t).total_seconds() < 900
                )
                if not too_soon:
                    rupee_lvls = data.get("rupee_levels") or {}
                    sma7_data  = data.get("sma7_gap", {})
                    trend_7d   = data.get("trend_7d", "Unknown")
                    q          = data.get("quote", {})
                    pred         = data.get("trade_pred", {})
                    score        = pred.get("score", 0)
                    tier_emoji   = {"A — HIGH":"🟢","B — MODERATE":"🟡","C — LOW":"🟠","SKIP":"🔴"}.get(pred.get("tier",""),  "⚪")
                    _pb = lambda p, w=18: "█"*round(p/100*w) + "░"*(w-round(p/100*w))
                    action_label = intra_sig["action"].replace("_", " ")
                    action_emoji = "🔔🔔" if intra_sig["action"] == "STRONG_BUY" else "🔔"
                    trend_icon   = {"Upward":"📈","Downward":"📉","Choppy":"〰️"}.get(trend_7d, "📊")
                    change_pct   = q.get('change_pct', 0)
                    gap_val      = sma7_data.get('gap', 0)
                    r_t1 = pred.get('reach_t1', 0)
                    r_t2 = pred.get('reach_t2', 0)
                    r_t3 = pred.get('reach_t3', 0)
                    r_st = pred.get('p_stop', 0)
                    intra_msg = (
                        f"{action_emoji} *{TICKER}.NS — {action_label}*\n"
                        f"{intra_sig['reason']}\n"
                        f"\n"
                        f"Current  ₹{q.get('price',0):,.2f}  "
                        f"({'+' if change_pct>=0 else ''}{change_pct:.1f}%)\n"
                        f"SMA7     ₹{sma7_data.get('sma7',0):,.2f}  (gap ₹{gap_val:+.0f})\n"
                        f"Trend    {trend_icon} {trend_7d}\n"
                        f"\n"
                        f"{tier_emoji} *Confidence {score}/100 — {pred.get('tier','')}*\n"
                        f"```\n"
                        f"T1 +₹10  {_pb(r_t1)} {r_t1:.0f}%\n"
                        f"T2 +₹20  {_pb(r_t2)} {r_t2:.0f}%\n"
                        f"T3 +₹25  {_pb(r_t3)} {r_t3:.0f}%\n"
                        f"Stop     {_pb(r_st)} {r_st:.0f}%\n"
                        f"```\n"
                        f"Rec: {pred.get('target_rec','')}  ·  EV ₹{pred.get('ev',0):+,.0f}\n"
                    )
                    if rupee_lvls:
                        intra_msg += (
                            f"\n📋 *Trade Plan*  (₹{CAPITAL:,.0f})\n"
                            f"```\n"
                            f"Qty    {rupee_lvls['qty']} sh @ ₹{rupee_lvls['entry']:,.2f}\n"
                            f"SL     ₹{rupee_lvls['sl']:,.2f}  (-₹{rupee_lvls['sl_diff']:.0f})\n"
                            f"T1     ₹{rupee_lvls['t1']:,.2f}  (+₹10)\n"
                            f"T2     ₹{rupee_lvls['t2']:,.2f}  (+₹20) primary\n"
                            f"T3     ₹{rupee_lvls['t3']:,.2f}  (+₹25) stretch\n"
                            f"Max    ₹{rupee_lvls['max_risk']:,.0f} risk\n"
                            f"```\n"
                        )

                    if wa_ready:
                        send_whatsapp_alert(
                            TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                            TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO,
                            intra_msg,
                        )
                    last_alerted[sig_key] = datetime.now()

            # ── Dispatch time-based exit alert ────────────────────────────────
            time_act = data.get("time_action")
            if time_act and wa_ready:
                ta_key  = f"time_{time_act['action']}"
                last_t  = last_alerted.get(ta_key)
                too_soon = last_t and (datetime.now() - last_t).total_seconds() < 3600
                if not too_soon:
                    ta_msg = (
                        f"⏰ *{TICKER}.NS — {time_act['reason']}*\n"
                        f"Price: ₹{data.get('quote', {}).get('price', 0):,.2f}"
                    )
                    if time_act["action"] == "tighten_stop":
                        ta_msg += f"\nNew stop: ₹{time_act.get('new_sl', 0):,.2f}"
                    send_whatsapp_alert(
                        TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                        TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO,
                        ta_msg,
                    )
                    last_alerted[ta_key] = datetime.now()

            # ── Dispatch sentiment shift alert ────────────────────────────────
            shift_alert = (data.get("news_snapshot") or {}).get("shift_alert")
            if shift_alert and wa_ready:
                send_whatsapp_alert(
                    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                    TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO,
                    shift_alert,
                )

            # ── Dispatch strong score alerts (existing behaviour) ─────────────
            score_result = data.get("score_result")
            quote = data.get("quote")
            if score_result and quote and should_alert(score_result, last_alerted):
                alerted = False

                if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                    msg = format_alert(TICKER, score_result, quote)
                    if send_alert(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg):
                        alerted = True

                if wa_ready:
                    wa_msg = format_whatsapp_alert(
                        TICKER,
                        score_result,
                        quote,
                        target_probs=data.get("target_probs"),
                        intraday_probs=data.get("intraday_probs"),
                        buy_signal=data.get("buy_signal"),
                        strategy_state=data.get("strategy_state"),
                        pnl_unrealised=data.get("pnl_unrealised", 0.0),
                        halted_reason=data.get("halted_reason", ""),
                    )
                    if send_whatsapp_alert(
                        TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                        TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO,
                        wa_msg,
                    ):
                        alerted = True

                if alerted:
                    last_alerted[score_result["signal"]] = datetime.now()

            # ── Dispatch Supertrend strategy events (open / partial / close) ──
            for event_type, payload in data.get("strategy_events", []):
                if event_type == "open":
                    msg = format_position_open_alert(
                        TICKER, payload["sizing"], payload["buy_signal"], CAPITAL, RISK_PCT
                    )
                elif event_type == "partial":
                    msg = format_partial_alert(
                        TICKER, payload["position"], payload["price"], payload["pnl"]
                    )
                elif event_type == "close":
                    msg = format_position_close_alert(
                        TICKER, payload["trade"], payload["reason"]
                    )
                else:
                    continue

                if wa_ready:
                    send_whatsapp_alert(
                        TWILIO_ACCOUNT_SID,
                        TWILIO_AUTH_TOKEN,
                        TWILIO_WHATSAPP_FROM,
                        TWILIO_WHATSAPP_TO,
                        msg,
                    )
                if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                    send_alert(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)

            # Countdown loop — re-render every second so the timer is live
            for secs_left in range(REFRESH_SECONDS, 0, -1):
                layout = build_dashboard(
                    quote=data.get("quote"),
                    pivots=data.get("pivots"),
                    cam=data.get("cam"),
                    atr_lvls=data.get("atr_lvls"),
                    indicators=data.get("indicators"),
                    sentiment=data.get("sentiment"),
                    score_result=data.get("score_result"),
                    target_probs=data.get("target_probs", {}),
                    intraday_probs=data.get("intraday_probs", {}),
                    buy_signal=data.get("buy_signal"),
                    sizing=data.get("sizing"),
                    strategy_state=data.get("strategy_state"),
                    pnl_unrealised=data.get("pnl_unrealised", 0.0),
                    halted_reason=data.get("halted_reason", ""),
                    capital=CAPITAL,
                    last_updated=last_updated,
                    next_refresh_secs=secs_left,
                    ticker=TICKER,
                )
                live.update(layout)
                time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Dashboard closed. Happy trading!\n")
