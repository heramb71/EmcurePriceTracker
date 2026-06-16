#!/usr/bin/env python3
"""
Headless service entrypoint for unattended cloud deployment.

Run:
  python3 main_headless.py
  TICKER=RELIANCE REFRESH_SECONDS=120 python3 main_headless.py

For a systemd deployment, see deploy/emcure_price_tracker.service.
"""

import argparse
import os
import sys
import time
import logging
from argparse import ArgumentParser
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv

load_dotenv()


def _warn_if_env_world_readable() -> None:
    """The .env holds the Kite password + TOTP secret (a full 2FA bypass).
    Warn loudly if it is readable by group/others so it gets locked down."""
    env_path = Path(".env")
    try:
        if not env_path.exists():
            return
        mode = env_path.stat().st_mode
        if mode & 0o077:
            logging.getLogger(__name__).warning(
                ".env is group/world-readable (mode %o) — it holds Kite credentials. "
                "Run: chmod 600 .env", mode & 0o777,
            )
    except Exception:
        pass


from src.sentiment import load_sentiment_model
from src.holidays import is_market_holiday, format_holiday_alert
from src.broker import KiteBroker
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
from src.predictor import (
    format_pre_open_briefing,
    format_post_open_briefing,
    format_eod_summary,
)
from src.trade_manager import check_and_mark, format_target_alert
from src.state import load_state, save_state
from main import _refresh

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("main_headless")

_IST = timezone(timedelta(hours=5, minutes=30))
_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)
_WAKEUP_BEFORE_OPEN = timedelta(minutes=10)


def _now_ist() -> datetime:
    return datetime.now(_IST)


def _is_market_open(now: datetime | None = None) -> bool:
    """Return True if NSE is currently open (Mon–Fri, 9:15–15:30 IST, non-holiday)."""
    now = now or _now_ist()
    if now.weekday() >= 5:
        return False
    if is_market_holiday(now.date()):
        return False
    t = now.time()
    return _MARKET_OPEN <= t <= _MARKET_CLOSE


def _sleep_until_market_open() -> None:
    """Sleep until 10 minutes before the next NSE market open, then return."""
    now = _now_ist()
    candidate = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now.time() >= _MARKET_CLOSE or now.weekday() >= 5:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    wake_at = candidate - _WAKEUP_BEFORE_OPEN
    if wake_at <= now:
        return
    sleep_secs = (wake_at - now).total_seconds()
    next_open_str = candidate.strftime("%Y-%m-%d %H:%M IST")
    logger.info(
        "Market closed. Sleeping %.0f min until %s (waking 10 min early).",
        sleep_secs / 60,
        next_open_str,
    )
    time.sleep(sleep_secs)


def parse_args() -> argparse.Namespace:
    parser = ArgumentParser(
        description="Run EmcurePriceTracker in headless cloud mode."
    )
    parser.add_argument(
        "--ticker", default=os.getenv("TICKER", "EMCURE"), help="NSE ticker symbol"
    )
    parser.add_argument(
        "--refresh",
        type=int,
        default=int(os.getenv("REFRESH_SECONDS", "300")),
        help="Refresh interval in seconds",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce console output to warnings and errors",
    )
    return parser.parse_args()


def _dispatch_alerts(
    ticker: str,
    data: dict,
    now_t: datetime,
    last_alerted: dict,
    wa_sid: str,
    wa_token: str,
    wa_from: str,
    wa_to: str,
    capital: float,
    risk_rupees: float,
    risk_pct: float,
    tg_token: str,
    tg_chat_id: str,
    broker: "KiteBroker | None" = None,
) -> None:
    """Send all scheduled and event-driven alerts to every configured channel."""
    wa_ready     = bool(wa_sid and wa_token and wa_from and wa_to)
    tg_ready     = bool(tg_token and tg_chat_id)
    notify_ready = wa_ready or tg_ready

    def _tg(msg: str) -> None:
        if tg_ready:
            send_alert(tg_token, tg_chat_id, msg)

    def _notify(msg: str) -> None:
        """Fan out to every configured channel: WhatsApp (best-effort — Twilio
        trial caps at 50 msgs/day) and Telegram (no cap)."""
        if wa_ready:
            send_whatsapp_alert(wa_sid, wa_token, wa_from, wa_to, msg)
        if tg_ready:
            send_alert(tg_token, tg_chat_id, msg)

    # ── Holiday alert (9:00–9:14 AM, once per day) ───────────────────────────
    if notify_ready and now_t.hour == 9 and now_t.minute < 15:
        holiday_key = f"holiday_{now_t.date()}"
        if holiday_key not in last_alerted and is_market_holiday(now_t.date()):
            _notify(format_holiday_alert(ticker, now_t.date()))
            last_alerted[holiday_key]                   = now_t
            last_alerted[f"pre_open_{now_t.date()}"]    = now_t
            last_alerted[f"post_open_{now_t.date()}"]   = now_t
            last_alerted[f"eod_{now_t.date()}"]         = now_t
            logger.info("Holiday alert sent for %s", now_t.date())
            return  # no further alerts on holidays

    # ── Pre-open briefing (9:00–9:14 AM, once per day) ───────────────────────
    if notify_ready and now_t.hour == 9 and now_t.minute < 15:
        pre_key = f"pre_open_{now_t.date()}"
        if pre_key not in last_alerted:
            q   = data.get("quote", {})
            s7  = data.get("sma7_gap", {})
            msg = format_pre_open_briefing(
                ticker          = ticker,
                price           = data.get("daily_close") or q.get("price", 0),
                sma7            = s7.get("sma7", 0),
                trend_7d        = data.get("trend_7d", "Unknown"),
                atr             = data.get("indicators", {}).get("atr", 30),
                capital         = capital,
                risk_rupees     = risk_rupees,
                prior_losses    = data.get("prior_losses", 0),
                sentiment_label = data.get("news_sent_label", "Neutral"),
                sentiment_score = data.get("news_sent_score", 0.0),
                indicators      = data.get("indicators", {}),
                score_result    = data.get("score_result") or {},
                now             = now_t,
            )
            _notify(msg)
            last_alerted[pre_key] = now_t
            logger.info("Pre-open briefing sent")

    # ── Post-open update (9:20–9:59 AM, once per day) ────────────────────────
    if notify_ready and now_t.hour == 9 and now_t.minute >= 20:
        post_key = f"post_open_{now_t.date()}"
        if post_key not in last_alerted:
            q    = data.get("quote", {})
            s7   = data.get("sma7_gap", {})
            msg  = format_post_open_briefing(
                ticker        = ticker,
                open_price    = q.get("open", q.get("price", 0)),
                current_price = q.get("price", 0),
                sma7          = s7.get("sma7", 0),
                orb           = data.get("orb", {}),
                atr           = data.get("indicators", {}).get("atr", 30),
                capital       = capital,
                risk_rupees   = risk_rupees,
                prior_losses  = data.get("prior_losses", 0),
                indicators    = data.get("indicators", {}),
                score_result  = data.get("score_result") or {},
                now           = now_t,
            )
            _notify(msg)
            last_alerted[post_key] = now_t
            logger.info("Post-open update sent")

    # ── EOD summary (3:30–3:59 PM, once per day) ─────────────────────────────
    if notify_ready and now_t.hour == 15 and now_t.minute >= 30:
        eod_key = f"eod_{now_t.date()}"
        if eod_key not in last_alerted:
            q   = data.get("quote", {})
            s7  = data.get("sma7_gap", {})
            msg = format_eod_summary(
                ticker       = ticker,
                open_price   = float(q.get("open",  q.get("price", 0)) or 0),
                high         = float(q.get("high",  q.get("price", 0)) or 0),
                low          = float(q.get("low",   q.get("price", 0)) or 0),
                close        = data.get("daily_close") or float(q.get("price", 0)),
                change_pct   = float(q.get("change_pct", 0) or 0),
                sma7         = s7.get("sma7", 0),
                atr          = data.get("indicators", {}).get("atr", 30),
                capital      = capital,
                risk_rupees  = risk_rupees,
                prior_losses = data.get("prior_losses", 0),
                day_pnl      = 0.0,
                trades_today = 0,
                indicators   = data.get("indicators", {}),
                score_result = data.get("score_result") or {},
                now          = now_t,
            )
            _notify(msg)
            last_alerted[eod_key] = now_t
            logger.info("EOD summary sent")

    # ── Intraday entry signal (BUY / STRONG_BUY) ─────────────────────────────
    intra_sig = data.get("intra_signal", {})
    if intra_sig.get("action") in ("BUY", "STRONG_BUY"):
        # Key is date-scoped so a service restart never re-fires within the same day
        sig_key  = f"intra_{intra_sig['action']}_{now_t.date()}"
        last_t   = last_alerted.get(sig_key)
        too_soon = last_t and (datetime.now(_IST) - last_t).total_seconds() < 900
        if not too_soon:
            q          = data.get("quote", {})
            rupee_lvls = data.get("rupee_levels") or {}
            sma7_data  = data.get("sma7_gap", {})
            trend_7d   = data.get("trend_7d", "Unknown")
            pred       = data.get("trade_pred", {})
            score      = pred.get("score", 0)
            tier_emoji = {"A — HIGH": "🟢", "B — MODERATE": "🟡",
                          "C — LOW": "🟠", "SKIP": "🔴"}.get(pred.get("tier", ""), "⚪")
            _pb = lambda p, w=18: "█" * round(p / 100 * w) + "░" * (w - round(p / 100 * w))
            action_emoji = "🔔🔔" if intra_sig["action"] == "STRONG_BUY" else "🔔"
            trend_icon   = {"Upward": "📈", "Downward": "📉", "Choppy": "〰️"}.get(trend_7d, "📊")
            change_pct   = q.get("change_pct", 0)
            gap_val      = sma7_data.get("gap", 0)
            price_now    = q.get("price", 0)
            r_t1 = pred.get("reach_t1", 0)
            r_t2 = pred.get("reach_t2", 0)
            r_t3 = pred.get("reach_t3", 0)
            r_st = pred.get("p_stop", 0)

            confidence_label = (
                "High — strong setup 👍"  if score >= 75 else
                "Medium — decent chance"  if score >= 55 else
                "Low — be cautious"       if score >= 40 else
                "Very low — consider skipping"
            )

            intra_lines = [
                f"{action_emoji} *Buy Signal — {ticker}*",
                f"📉 Stock has dropped ₹{abs(gap_val):.0f} below its 7-day average",
                f"This is the entry zone we were waiting for.",
                "",
                f"Current price: ₹{price_now:,.2f}  ({'+' if change_pct >= 0 else ''}{change_pct:.1f}% today)",
                f"7-day average: ₹{sma7_data.get('sma7', 0):,.2f}",
                f"Trend: {trend_icon} {trend_7d}",
                "",
                f"{tier_emoji} *Confidence: {confidence_label}*",
                f"Chance of +₹10: {r_t1:.0f}%",
                f"Chance of +₹20: {r_t2:.0f}%",
                f"Chance of +₹25: {r_t3:.0f}%",
                f"Chance of stop: {r_st:.0f}%",
            ]

            if pred.get("ev", 0) != 0:
                ev_label = "expected profit" if pred["ev"] > 0 else "expected loss"
                intra_lines.append(f"Expected outcome: ₹{pred['ev']:+,.0f} ({ev_label})")

            if rupee_lvls:
                intra_lines += [
                    "",
                    f"📋 *Trade plan ({rupee_lvls['qty']} shares):*",
                    f"Buy at:       ₹{rupee_lvls['entry']:,.2f}",
                    f"Sell half at: ₹{rupee_lvls['t1']:,.2f}  (+₹10, profit ~₹{10 * rupee_lvls['qty']:,.0f})",
                    f"Next target:  ₹{rupee_lvls['t2']:,.2f}  (+₹20, profit ~₹{20 * rupee_lvls['qty']:,.0f})",
                    f"Stretch:      ₹{rupee_lvls['t3']:,.2f}  (+₹25, profit ~₹{25 * rupee_lvls['qty']:,.0f})",
                    f"Stop loss:    ₹{rupee_lvls['sl']:,.2f}  (max loss ₹{rupee_lvls['max_risk']:,.0f})",
                ]

            intra_msg = "\n".join(intra_lines)
            _notify(intra_msg)
            last_alerted[sig_key] = datetime.now(_IST)
            logger.info("Intraday signal alert sent: %s", intra_sig["action"])

    # ── Manual trade T1/T2/T3/SL alerts ──────────────────────────────────────
    q_now     = data.get("quote", {})
    day_high  = float(q_now.get("high", 0) or 0)
    day_low   = float(q_now.get("low",  0) or 0)
    cur_price = float(q_now.get("price", 0) or 0)
    if cur_price > 0 and day_high > 0:
        hits = check_and_mark(cur_price, day_high, day_low)
        for hit in hits:
            msg = format_target_alert(ticker, hit, cur_price)
            _notify(msg)
            logger.info("Target hit alert sent: %s", hit.get("label"))

    # ── Time-based exit alert ─────────────────────────────────────────────────
    time_act = data.get("time_action")
    if time_act and notify_ready:
        ta_key   = f"time_{time_act['action']}_{now_t.date()}"
        last_t   = last_alerted.get(ta_key)
        too_soon = last_t and (datetime.now(_IST) - last_t).total_seconds() < 3600
        if not too_soon:
            ta_msg = (
                f"⏰ *{ticker}.NS — {time_act['reason']}*\n"
                f"Price: ₹{q_now.get('price', 0):,.2f}"
            )
            if time_act["action"] == "tighten_stop":
                ta_msg += f"\nNew stop: ₹{time_act.get('new_sl', 0):,.2f}"
            _notify(ta_msg)
            last_alerted[ta_key] = datetime.now(_IST)
            logger.info("Time-based exit alert sent: %s", time_act["action"])

    # ── Sentiment shift alert (60-min cooldown to prevent repeat sends) ───────
    shift_alert = (data.get("news_snapshot") or {}).get("shift_alert")
    if shift_alert and notify_ready:
        shift_key = f"sentiment_shift_{now_t.date()}"
        last_t    = last_alerted.get(shift_key)
        too_soon  = last_t and (datetime.now(_IST) - last_t).total_seconds() < 3600
        if not too_soon:
            _notify(shift_alert)
            last_alerted[shift_key] = datetime.now(_IST)
            logger.info("Sentiment shift alert sent")

    # ── Strong score alert (existing behaviour) ───────────────────────────────
    score_result = data.get("score_result")
    quote        = data.get("quote")
    if score_result and quote and should_alert(score_result, last_alerted):
        alerted = False
        signal  = score_result.get("signal", "Hold")

        if tg_token and tg_chat_id:
            _tg(format_alert(ticker, score_result, quote))
            alerted = True

        if wa_ready:
            wa_msg = format_whatsapp_alert(
                ticker,
                score_result,
                quote,
                target_probs    = data.get("target_probs"),
                intraday_probs  = data.get("intraday_probs"),
                buy_signal      = data.get("buy_signal"),
                strategy_state  = data.get("strategy_state"),
                pnl_unrealised  = data.get("pnl_unrealised", 0.0),
                halted_reason   = data.get("halted_reason", ""),
            )
            if send_whatsapp_alert(wa_sid, wa_token, wa_from, wa_to, wa_msg):
                alerted = True

        if alerted:
            last_alerted[signal] = datetime.now(_IST)
            logger.info("Score-based alert sent: %s", signal)

    # ── Supertrend strategy events ────────────────────────────────────────────
    # Orders are now placed AND confirmed inside _refresh() before the state is
    # mutated, so these events already reflect real, filled trades. This loop
    # only notifies; it never places orders.
    for event_type, payload in data.get("strategy_events", []):
        if event_type == "open":
            msg = format_position_open_alert(
                ticker, payload["sizing"], payload["buy_signal"], capital, risk_pct
            )
            if broker:
                msg += "\n✅ Order filled and confirmed."

        elif event_type == "partial":
            reason = payload.get("reason", "t1_hit")
            msg = format_partial_alert(
                ticker, payload["position"], payload["price"], payload["pnl"], reason
            )
            if broker:
                msg += "\n✅ Sell order filled and confirmed."

        elif event_type == "close":
            msg = format_position_close_alert(ticker, payload["trade"], payload["reason"])
            if broker:
                msg += "\n✅ Sell order filled and confirmed."

        elif event_type == "open_failed":
            msg = (
                f"⚠️ *Auto-trade BUY not placed — {ticker}*\n\n"
                f"Tried to buy {payload['qty']} shares but the order did not fill "
                f"(rejected, cancelled, or timed out). No position was opened. "
                f"Check Zerodha and your funds."
            )
            logger.error("AUTO-TRADE BUY did not fill — qty=%d", payload["qty"])

        elif event_type == "exit_failed":
            msg = (
                f"🚨 *Auto-trade SELL FAILED — {ticker}*\n\n"
                f"Tried to sell {payload['qty']} shares ({payload['reason']}) but the "
                f"order did not fill. Your position is STILL OPEN — please exit "
                f"manually in Zerodha now."
            )
            logger.error(
                "AUTO-TRADE SELL did not fill — qty=%d reason=%s",
                payload["qty"], payload["reason"],
            )

        elif event_type == "insufficient_funds":
            msg = (
                f"💸 *Auto-trade BUY skipped — {ticker}*\n\n"
                f"Signal fired but you have ₹{payload['have']:,.0f} available and the "
                f"trade needs ₹{payload['need']:,.0f} ({payload['qty']} shares). "
                f"Add funds or lower CAPITAL."
            )
            logger.warning(
                "AUTO-TRADE BUY skipped — need ₹%.0f have ₹%.0f",
                payload["need"], payload["have"],
            )

        elif event_type == "reconcile_warn":
            msg = (
                f"⚠️ *Auto-trade BUY skipped — {ticker}*\n\n"
                f"A buy signal fired but Zerodha already shows {payload['held']} shares "
                f"held that the bot wasn't tracking. No new order placed. Check your "
                f"positions — fix the mismatch before the next signal."
            )
            logger.error("AUTO-TRADE BUY skipped — broker holds %d untracked", payload["held"])

        elif event_type == "event_blocked":
            msg = (
                f"📅 *Auto-trade BUY skipped — {ticker}*\n\n"
                f"A buy signal fired but earnings are within a couple of days. "
                f"Skipping new entries to avoid overnight gap risk around results."
            )
            logger.warning("AUTO-TRADE BUY skipped — near earnings event")
        else:
            continue

        _notify(msg)
        logger.info("Strategy event alert sent: %s", event_type)


def _broadcast(msg: str) -> None:
    """Send a system/auth message to every configured channel (WhatsApp +
    Telegram). Reads creds from env so it works anywhere without plumbing."""
    wa_sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
    wa_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    wa_from  = os.getenv("TWILIO_WHATSAPP_FROM", "")
    wa_to    = os.getenv("TWILIO_WHATSAPP_TO", "")
    tg_token   = os.getenv("TELEGRAM_TOKEN", "")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if wa_sid and wa_token and wa_from and wa_to:
        send_whatsapp_alert(wa_sid, wa_token, wa_from, wa_to, msg)
    if tg_token and tg_chat_id:
        send_alert(tg_token, tg_chat_id, msg)


def _reconcile_on_startup(
    broker, ticker, wa_sid, wa_token, wa_from, wa_to, wa_ready
) -> None:
    """Compare bot trade state against the broker's actual holdings on startup."""
    state    = load_state()
    pos      = state.get("position") or {}
    bot_qty  = int(pos.get("qty_remaining", 0)) if pos else 0
    held     = broker.held_qty(ticker)

    if held is None:
        logger.warning("Reconcile: could not query broker holdings — skipping check")
        return

    if bot_qty == held:
        logger.info("Reconcile OK: bot=%d  broker=%d shares", bot_qty, held)
        return

    logger.error("RECONCILE MISMATCH: bot=%d  broker=%d shares", bot_qty, held)
    _broadcast(
        f"⚠️ *Position mismatch — {ticker}*\n\n"
        f"Bot thinks it holds {bot_qty} shares, but Zerodha shows {held}.\n"
        f"The bot will keep using its own record. If that's wrong, fix it "
        f"before the next signal (e.g. send SELL or clear state)."
    )


def main() -> None:
    args = parse_args()
    ticker          = args.ticker
    refresh_seconds = args.refresh

    if args.quiet:
        logger.setLevel(logging.WARNING)

    logger.info("Starting EmcurePriceTracker headless service for %s", ticker)
    logger.info("Refresh interval: %ss", refresh_seconds)

    _warn_if_env_world_readable()
    load_sentiment_model()
    last_alerted: dict = {}

    wa_sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
    wa_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    wa_from  = os.getenv("TWILIO_WHATSAPP_FROM", "")
    wa_to    = os.getenv("TWILIO_WHATSAPP_TO", "")
    tg_token   = os.getenv("TELEGRAM_TOKEN", "")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    capital     = float(os.getenv("CAPITAL", "100000"))
    risk_rupees = float(os.getenv("RISK_RUPEES", "4500"))
    risk_pct    = float(os.getenv("RISK_PCT", "1.0"))

    wa_ready = bool(wa_sid and wa_token and wa_from and wa_to)
    logger.info("WhatsApp alerts: %s", "enabled" if wa_ready else "DISABLED — check .env")

    # ── Kite auto-trading ────────────────────────────────────────────────────
    # The "ACTIVE" announcement is persisted (not just kept in last_alerted)
    # so a process restart later the same day — crash, deploy, OOM — doesn't
    # re-send it. last_alerted is in-memory only and resets on every restart.
    today_str = _now_ist().date().isoformat()
    persisted_state = load_state()
    already_announced_today = persisted_state.get("kite_announced_date") == today_str

    def _announce_active_once() -> None:
        nonlocal already_announced_today
        logger.warning("Kite auto-trading ACTIVE")
        if already_announced_today:
            logger.info("Startup announcement already sent today — suppressing duplicate")
            return
        _broadcast(
            f"✅ {ticker} auto-trading ACTIVE\n"
            f"Capital: ₹{capital:,.0f}  Risk: {risk_pct}%/trade")
        persisted_state["kite_announced_date"] = today_str
        save_state(persisted_state)
        already_announced_today = True

    broker: KiteBroker | None = None
    if os.getenv("KITE_AUTO_TRADE", "false").lower() == "true":
        kite_key    = os.getenv("KITE_API_KEY", "")
        kite_secret = os.getenv("KITE_API_SECRET", "")
        if not kite_key or not kite_secret:
            logger.error("KITE_AUTO_TRADE=true but KITE_API_KEY/KITE_API_SECRET not set — auto-trading disabled")
        else:
            broker = KiteBroker(kite_key, kite_secret)
            _kite_user    = os.getenv("KITE_USER_ID", "")
            _kite_pass    = os.getenv("KITE_PASSWORD", "")
            _kite_totp    = os.getenv("KITE_TOTP_SECRET", "")
            if not broker.is_authenticated():
                if _kite_user and _kite_pass and _kite_totp:
                    logger.info("Kite not authenticated — attempting auto_login")
                    if broker.auto_login(_kite_user, _kite_pass, _kite_totp):
                        _announce_active_once()
                    else:
                        logger.error("Kite auto_login failed — sending login URL")
                        _broadcast(
                            f"🔐 Kite auth needed\n\n"
                            f"1. Open: {broker.login_url()}\n"
                            f"2. Log in with your Zerodha credentials\n"
                            f"3. Copy request_token from redirect URL\n"
                            f"4. Reply: TOKEN <request_token>")
                        broker = None
                else:
                    logger.warning("Kite credentials not in .env — sending login URL")
                    _broadcast(
                        f"🔐 Kite auth needed for auto-trading\n\n"
                        f"Open this link and log in:\n{broker.login_url()}\n\n"
                        f"Then reply: TOKEN <request_token>")
                    broker = None
            else:
                _announce_active_once()

    # ── Startup reconciliation ─────────────────────────────────────────────────
    # Detect divergence between what the bot believes it holds (strategy_state)
    # and what Zerodha actually shows, before the loop starts trading on it.
    if broker:
        _reconcile_on_startup(broker, ticker, wa_sid, wa_token, wa_from, wa_to, wa_ready)

    while True:
        # The whole iteration is guarded: a transient data/network error here
        # must not crash the process. An unhandled exception would propagate
        # out of main(), and since systemd has Restart=on-failure, the service
        # would restart from scratch every 30s — re-running the startup block
        # above and re-sending the "auto-trading ACTIVE" announcement on loop.
        try:
            now = _now_ist()

            # Pre-open briefing window is before market open — run outside market hours
            pre_open_window = now.hour == 9 and now.minute < 15
            if pre_open_window and not is_market_holiday(now.date()):
                # Re-authenticate Kite at start of each trading day, with a once-per-day
                # heartbeat so a silent auth failure can't leave trading dead unnoticed.
                auth_key = f"auth_{now.date()}"
                if broker and auth_key not in last_alerted:
                    _kite_user = os.getenv("KITE_USER_ID", "")
                    _kite_pass = os.getenv("KITE_PASSWORD", "")
                    _kite_totp = os.getenv("KITE_TOTP_SECRET", "")
                    authed = broker.is_authenticated()
                    if not authed and _kite_user and _kite_pass and _kite_totp:
                        authed = broker.auto_login(_kite_user, _kite_pass, _kite_totp)

                    if authed:
                        logger.warning("Kite auth OK for %s — auto-trading ACTIVE", now.date())
                        _broadcast(
                            f"✅ {ticker} auto-trading ACTIVE today\n"
                            f"Capital: ₹{capital:,.0f}  Risk: {risk_pct}%/trade")
                    else:
                        logger.error("Kite daily re-auth FAILED — auto-trading suspended")
                        _broadcast(
                            f"🚨 {ticker} auto-trading is DOWN today\n\n"
                            f"Kite login failed — no orders will be placed. "
                            f"Reply TOKEN <request_token> after logging in:\n"
                            f"{broker.login_url()}")
                        broker = None
                    last_alerted[auth_key] = now

                pre_key = f"pre_open_{now.date()}"
                if pre_key not in last_alerted:
                    data = _refresh(ticker)
                    if data:
                        _dispatch_alerts(
                            ticker, data, now, last_alerted,
                            wa_sid, wa_token, wa_from, wa_to,
                            capital, risk_rupees, risk_pct, tg_token, tg_chat_id,
                            broker=broker,
                        )
                    time.sleep(60)
                    continue

            if not _is_market_open():
                _sleep_until_market_open()
                continue

            started_at = _now_ist()
            data = _refresh(ticker, broker=broker)

            if not data:
                logger.warning("No market data returned, retrying in %ss...", refresh_seconds)
            else:
                quote        = data.get("quote") or {}
                score_result = data.get("score_result") or {}
                price        = quote.get("price") or 0.0
                signal       = score_result.get("signal", "Hold")
                score        = score_result.get("score", 0.0)

                logger.info(
                    "%s @ ₹%.2f | signal=%s | score=%.2f | change=%+.2f%%",
                    ticker, price, signal, score, quote.get("change_pct", 0.0),
                )

                _dispatch_alerts(
                    ticker, data, _now_ist(), last_alerted,
                    wa_sid, wa_token, wa_from, wa_to,
                    capital, risk_rupees, risk_pct, tg_token, tg_chat_id,
                    broker=broker,
                )

            elapsed   = (_now_ist() - started_at).total_seconds()
            sleep_for = max(1, refresh_seconds - int(elapsed))
            logger.debug("Sleeping for %s seconds", sleep_for)
            time.sleep(sleep_for)
        except Exception:
            logger.exception("Unhandled error in main loop — retrying in %ss", refresh_seconds)
            time.sleep(refresh_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down headless service")
