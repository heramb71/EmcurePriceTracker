from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

# Telegram circuit breaker. When api.telegram.org is unreachable — e.g. the
# periodic government block in India — pause Telegram sends for this long instead
# of paying two connect-timeouts (~6s) on every single alert dispatch and dumping
# a full traceback each time. The breaker auto-closes after the cooldown, so
# delivery resumes on its own once the block lifts — no restart or config change.
_TG_COOLDOWN_S = 300.0
_tg_paused_until = 0.0

# Mirrors _HORIZON in scoring.py — days allowed per swing target
_SWING_HORIZONS: dict[float, int] = {2.0: 3, 5.0: 5, 7.0: 10, 10.0: 15}


def _interpolate_prob(target_pct: float, probs: dict) -> int:
    """Linear interpolation between available intraday probability buckets."""
    keys = sorted(k for k in probs if k != "stop_hit" and isinstance(k, float))
    if not keys:
        return 0
    if target_pct <= keys[0]:
        return int(probs[keys[0]])
    if target_pct >= keys[-1]:
        return int(probs[keys[-1]])
    for i in range(len(keys) - 1):
        lo, hi = keys[i], keys[i + 1]
        if lo <= target_pct <= hi:
            frac = (target_pct - lo) / (hi - lo)
            return round(probs[lo] + frac * (probs[hi] - probs[lo]))
    return 0


def send_alert(token: str, chat_id: str, message: str) -> bool:
    """Send a Telegram message. Falls back to plain text if Markdown fails to
    parse (stray * / _ in dynamic content), so a message is never dropped.

    A connection/route failure (DNS or network blocked) opens a short circuit
    breaker so a dead endpoint doesn't stall every later dispatch — see
    _TG_COOLDOWN_S. The breaker auto-closes, so sending resumes on its own."""
    global _tg_paused_until

    if time.monotonic() < _tg_paused_until:
        return False  # circuit open — Telegram known-unreachable, skip fast

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=(3.05, 7),
        )
        if resp.status_code == 200:
            return True
        # Markdown parse errors return 400 — retry as plain text.
        logger.warning("Telegram Markdown send failed (%s); retrying plain", resp.status_code)
        resp = requests.post(
            url, json={"chat_id": chat_id, "text": message}, timeout=(3.05, 7)
        )
        return resp.status_code == 200
    except requests.exceptions.RequestException as exc:
        # Network-level failure (e.g. ENETUNREACH during the India block). Open
        # the breaker so we stop hammering a dead endpoint every dispatch.
        _tg_paused_until = time.monotonic() + _TG_COOLDOWN_S
        logger.warning(
            "Telegram unreachable (%s) — pausing Telegram sends for %.0fs",
            exc.__class__.__name__, _TG_COOLDOWN_S,
        )
        return False
    except Exception:
        logger.exception("send_alert failed")
        return False


def send_whatsapp_alert(
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    message: str,
) -> bool:
    """Send a WhatsApp message via Twilio.

    from_number / to_number must be in E.164 format e.g. +919876543210.
    Twilio wraps them as whatsapp:<number> internally.
    """
    try:
        from twilio.rest import Client

        client = Client(account_sid, auth_token)
        msg = client.messages.create(
            from_=f"whatsapp:{from_number}",
            to=f"whatsapp:{to_number}",
            body=message,
        )
        logger.warning("WhatsApp sent OK  sid=%s  to=%s  chars=%d", msg.sid, to_number, len(message))
        return True
    except Exception:
        logger.exception("send_whatsapp_alert failed  to=%s", to_number)
        return False


def format_alert(ticker: str, score_result: dict, quote: dict) -> str:
    """Markdown-formatted alert for Telegram (*bold* syntax)."""
    signal = score_result["signal"]
    price = quote["price"]
    change_pct = quote.get("change_pct", 0.0)
    entry = score_result["entry"]
    sl = score_result["sl"]
    t1 = score_result["t1"]
    t2 = score_result["t2"]
    score = score_result["score"]
    regime = score_result["regime"]

    emoji = "🚨" if "Strong" in signal else "📊"
    sign = "+" if change_pct >= 0 else ""

    return (
        f"{emoji} *{ticker}.NS — {signal}*\n"
        f"Price: ₹{price:,.2f} ({sign}{change_pct:.1f}%)\n"
        f"Entry: ₹{entry:,.2f} | SL: ₹{sl:,.2f} | T1: ₹{t1:,.2f} | T2: ₹{t2:,.2f}\n"
        f"Score: {score:.2f} | Regime: {regime}"
    )


def _supertrend_lines(
    buy_signal: dict | None,
    strategy_state: dict | None,
    pnl_unrealised: float,
    halted_reason: str,
    price: float = 0.0,
    intraday_probs: dict | None = None,
) -> list[str]:
    lines = ["", "*🚀 Supertrend Strategy*"]

    if halted_reason:
        lines.append(f"⛔ *HALTED* — {halted_reason}")
        return lines

    if buy_signal:
        conds = buy_signal.get("conditions", {})
        details = buy_signal.get("details", {})

        rsi_val = details.get("rsi", 0)
        vol_ratio = details.get("vol_ratio", 0)
        candle = details.get("candle", {})

        t = "✅ Trend" if conds.get("trend") else "❌ Trend"
        m = f"✅ RSI {rsi_val:.0f}" if conds.get("momentum") else f"❌ RSI {rsi_val:.0f}"
        v = "✅ Volume" if conds.get("volume") else f"❌ Vol {vol_ratio:.1f}x avg"

        if conds.get("candle"):
            c = "✅ Candle"
        elif not candle.get("is_bullish"):
            c = "❌ Candle (bearish)"
        elif candle.get("is_doji"):
            c = "❌ Candle (doji)"
        else:
            c = "❌ Candle (weak)"

        lines += [f"{t}    {m}", f"{v}    {c}"]

    position = (strategy_state or {}).get("position")
    session = (strategy_state or {}).get("session", {})

    lines.append("")
    if position:
        entry = position.get("entry", 0)
        sl = position.get("sl", 0)
        t1 = position.get("t1", 0)
        qty = position.get("qty_remaining", position.get("qty", 0))
        partial = position.get("partial_booked", False)
        pnl_sign = "+" if pnl_unrealised >= 0 else ""
        lines += [
            "*Position: OPEN*" + (" (50% booked)" if partial else ""),
            f"Entry  ₹{entry:,.2f}    Qty {qty}",
            f"Stop   ₹{sl:,.2f}    T1  ₹{t1:,.2f}",
            f"Unrealised P&L: {pnl_sign}₹{pnl_unrealised:,.0f}",
        ]
    else:
        triggered = (buy_signal or {}).get("triggered", False)
        if triggered:
            lines.append("*Gate: 🔔 BUY TRIGGERED*")
        else:
            conds = (buy_signal or {}).get("conditions", {})
            failed = [k for k, v in conds.items() if not v and k != "regime_ok"]
            failed_str = " + ".join(failed) if failed else "conditions"
            lines.append(f"Gate: FLAT  ({failed_str} failed)")

    if price > 0 and not position:
        stop = round(price - 13, 2)
        t1_20 = round(price + 20, 2)
        t2_25 = round(price + 25, 2)

        t1_pct = (t1_20 - price) / price * 100
        t2_pct = (t2_25 - price) / price * 100
        stop_pct_val = 13 / price * 100

        if intraday_probs:
            t1_prob = _interpolate_prob(t1_pct, intraday_probs)
            t2_prob = _interpolate_prob(t2_pct, intraday_probs)
            stop_prob = intraday_probs.get("stop_hit", 0)
            prob_t1 = f"  →  {t1_prob}%"
            prob_t2 = f"  →  {t2_prob}%"
            prob_stop = f"  →  {stop_prob}%"
        else:
            prob_t1 = prob_t2 = prob_stop = ""

        lines += [
            "",
            f"*📌 Daily Levels*  (CMP ₹{price:,.2f})",
            f"Entry   ₹{price:,.2f}",
            f"T1 +₹20  ₹{t1_20:,.2f}{prob_t1}",
            f"T2 +₹25  ₹{t2_25:,.2f}{prob_t2}",
            f"Stop    ₹{stop:,.2f}  (−₹{stop_pct_val:.1f}%){prob_stop}",
        ]

    session_pnl = session.get("session_pnl", 0)
    cons_losses = session.get("consecutive_losses", 0)
    pnl_sign = "+" if session_pnl >= 0 else ""
    lines.append(f"Session P&L: {pnl_sign}₹{session_pnl:,.0f}    Loss streak: {cons_losses}")

    return lines


def format_whatsapp_alert(
    ticker: str,
    score_result: dict,
    quote: dict,
    target_probs: dict | None = None,
    intraday_probs: dict | None = None,
    buy_signal: dict | None = None,
    strategy_state: dict | None = None,
    pnl_unrealised: float = 0.0,
    halted_reason: str = "",
) -> str:
    """Plain-text alert for WhatsApp (no Markdown parse_mode).

    Entry/SL/T1/T2 are anchored to today's open price (or prev_close if open
    is unavailable), keeping levels consistent with what a trader would see at
    the start of the session rather than the live mid-day price.
    """
    signal = score_result["signal"]
    price = quote["price"]
    change_pct = quote.get("change_pct", 0.0)
    score = score_result["score"]
    regime = score_result["regime"]

    # Anchor levels to open (preferred) or prev_close, not the live price
    ref_price = quote.get("open") or quote.get("prev_close") or score_result["entry"]

    # Derive ATR from the existing score levels: t1 = entry + 1.5 × ATR
    live_entry = score_result["entry"]
    atr = (score_result["t1"] - live_entry) / 1.5 if live_entry else 0
    sl = round(ref_price - 1.5 * atr, 2)
    t1 = round(ref_price + 1.5 * atr, 2)
    t2 = round(ref_price + 3.0 * atr, 2)

    direction = "▼" if change_pct < 0 else "▲"
    change_sign = "+" if change_pct >= 0 else ""
    ref_label = "Open" if quote.get("open") else "Prev Close"
    signal_emoji = "🔴" if "Sell" in signal else "🟢" if "Buy" in signal else "🟡"

    sl_diff = round(ref_price - sl, 2)
    t1_diff = round(t1 - ref_price, 2)
    t2_diff = round(t2 - ref_price, 2)

    lines = [
        f"{signal_emoji} *{ticker}.NS — {signal}*",
        f"Price: ₹{price:,.2f}  {direction} {change_sign}{change_pct:.1f}%",
        f"Score: {score:.2f}    Regime: {regime}",
        "",
        f"*📍 Levels*  ({ref_label}: ₹{ref_price:,.2f})",
        f"Entry  ₹{ref_price:,.2f}",
        f"Stop   ₹{sl:,.2f}  (−₹{sl_diff:,.2f})",
        f"T1     ₹{t1:,.2f}  (+₹{t1_diff:,.2f})",
        f"T2     ₹{t2:,.2f}  (+₹{t2_diff:,.2f})",
    ]

    if target_probs:
        swing_stop_pct = 100 - target_probs.get(2.0, 0)
        swing_stop_price = round(price * 0.98, 2)
        lines += ["", f"*📈 Swing Targets*  (from ₹{price:,.2f}, stop −2%)"]
        for pct, hit in target_probs.items():
            target_price = round(price * (1 + pct / 100), 2)
            days = _SWING_HORIZONS.get(float(pct), 5)
            lines.append(f"  +{pct:.0f}%  ₹{target_price:,.2f}  {days}d  →  {hit}%")
        lines.append(f"  Stop  ₹{swing_stop_price:,.2f}  →  {swing_stop_pct}%")

    if intraday_probs:
        stop_hit = intraday_probs.get("stop_hit", 0)
        intraday_stop_price = round(price * 0.995, 2)
        lines += ["", f"*⚡ Intraday Targets*  (from ₹{price:,.2f}, stop −0.5%)"]
        for pct, hit in intraday_probs.items():
            if pct != "stop_hit":
                target_price = round(price * (1 + pct / 100), 2)
                lines.append(f"  +{pct:.1f}%  ₹{target_price:,.2f}  →  {hit}%")
        lines.append(f"  Stop  ₹{intraday_stop_price:,.2f}  →  {stop_hit}%")

    lines += _supertrend_lines(
        buy_signal, strategy_state, pnl_unrealised, halted_reason, price, intraday_probs
    )

    return "\n".join(lines)


def format_position_open_alert(
    ticker: str, sizing: dict, buy_signal: dict, capital: float, risk_pct: float
) -> str:
    """WhatsApp message for a new Supertrend strategy entry."""
    entry = sizing["entry"]
    sl    = sizing["sl"]
    t1    = sizing["t1"]
    qty   = sizing["qty"]
    risk  = sizing["risk_amount"]

    return (
        f"🚀 *Trade Entered — {ticker}*\n\n"
        f"Bought {qty} shares at ₹{entry:,.2f}\n\n"
        f"🎯 First target: ₹{t1:,.2f}  (+₹{t1 - entry:.0f} per share)\n"
        f"🛑 Stop loss:    ₹{sl:,.2f}  (max loss ₹{risk:,.0f})\n\n"
        f"I'll sell half when target is hit and hold rest for more profit.\n"
        f"If price falls to ₹{sl:,.2f}, I'll exit to protect capital."
    )


def format_partial_alert(
    ticker: str, position: dict, exit_price: float, pnl: float, reason: str = "t1_hit"
) -> str:
    """WhatsApp message when a target is hit and 1/3 is booked."""
    qty_booked    = position["qty"] - position["qty_remaining"]
    qty_remaining = position["qty_remaining"]
    sign          = "+" if pnl >= 0 else ""

    label_map = {"t1_hit": "First", "t2_hit": "Second", "t3_hit": "Final"}
    label     = label_map.get(reason, "Target")
    emoji_map = {"t1_hit": "🎯", "t2_hit": "🎯🎯", "t3_hit": "🏆"}
    emoji     = emoji_map.get(reason, "💰")

    lines = [
        f"{emoji} *{label} Target Hit — {ticker}*",
        "",
        f"Sold {qty_booked} shares at ₹{exit_price:,.2f}",
        f"Profit booked: {sign}₹{pnl:,.0f} ✅",
        "",
    ]

    if reason == "t1_hit":
        lines += [
            f"Still holding {qty_remaining} shares.",
            f"Stop loss moved to ₹{position['sl']:,.2f} (breakeven — no loss possible now).",
            f"Next targets: ₹{position.get('t2', exit_price + 10):,.2f} (+₹20)  ·  ₹{position.get('t3', exit_price + 15):,.2f} (+₹25)",
        ]
    elif reason == "t2_hit":
        lines += [
            f"Still holding {qty_remaining} shares.",
            f"Final target: ₹{position.get('t3', exit_price + 5):,.2f} (+₹25)",
        ]
    else:
        lines.append(f"All targets hit! Consider exiting remaining {qty_remaining} shares.")

    return "\n".join(lines)


def format_position_close_alert(
    ticker: str, trade: dict, reason: str
) -> str:
    """WhatsApp message when position fully closes (stop or trailing exit)."""
    pnl   = trade["total_pnl"]
    sign  = "+" if pnl >= 0 else ""
    won   = pnl >= 0
    entry = trade["entry"]
    exit_ = trade["exit"]
    qty   = trade["qty_closed_at_exit"]
    had_partial = trade.get("partial_booked", False)

    if reason == "stop_hit":
        header = "🛑 *Stop Loss Hit — {ticker}*".format(ticker=ticker)
        reason_line = "Price fell to our stop loss level. Exited to protect capital."
    elif reason == "supertrend_exit":
        header = "📉 *Trend Reversed — {ticker}*".format(ticker=ticker)
        reason_line = "Market trend turned down. Exited remaining position."
    else:
        header = f"🔔 *Position Closed — {ticker}*"
        reason_line = ""

    result_emoji = "✅" if won else "❌"
    partial_note = "Had already booked partial profit at first target." if had_partial else ""

    return (
        f"{header}\n\n"
        f"Sold {qty} shares at ₹{exit_:,.2f}\n"
        f"Entry was ₹{entry:,.2f}\n\n"
        f"{result_emoji} Total P&L: {sign}₹{pnl:,.0f}\n"
        + (f"{partial_note}\n" if partial_note else "")
        + (f"\n{reason_line}" if reason_line else "")
    )


def should_alert(score_result: dict, last_alerted: dict) -> bool:
    signal = score_result.get("signal", "Hold")
    if signal not in ("Strong Buy", "Strong Sell"):
        return False
    last_time = last_alerted.get(signal)
    if last_time is None:
        return True
    return datetime.now() - last_time > timedelta(minutes=30)
