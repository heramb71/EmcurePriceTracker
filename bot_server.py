#!/usr/bin/env python3
"""
WhatsApp command bot for trade management.

Receives inbound WhatsApp messages via Twilio webhook and processes:
  BUY <price> [qty]  — record entry, set T1/T2/T3/SL
  SELL               — close trade, show final P&L
  STATUS             — live P&L + level progress
  HELP               — command list

Run via:  python bot_server.py
Or use:   ./start_bot.sh   (starts bot + ngrok tunnel together)
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from werkzeug.middleware.proxy_fix import ProxyFix

from src.trade_manager import set_trade, clear_trade, get_trade, current_pnl
from src.state import load_state

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

TICKER            = os.getenv("TICKER", "EMCURE")
CAPITAL           = float(os.getenv("CAPITAL", "100000"))
RISK_RUPEES       = float(os.getenv("RISK_RUPEES", "4500"))
AUTHORIZED           = os.getenv("TWILIO_WHATSAPP_TO", "").replace("whatsapp:", "")
TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
HEALTH_API_KEY    = os.getenv("HEALTH_API_KEY", "")
KITE_API_KEY      = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET   = os.getenv("KITE_API_SECRET", "")


def _live_price() -> float:
    try:
        import yfinance as yf
        return round(float(yf.Ticker(f"{TICKER}.NS").fast_info.last_price), 2)
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers — each returns a plain-text reply string
# ─────────────────────────────────────────────────────────────────────────────

def _handle_buy(parts: list[str]) -> str:
    if len(parts) < 2:
        return "Usage: BUY <price> [qty]\nExample: BUY 1693"
    try:
        entry = float(parts[1])
    except ValueError:
        return "❌ Invalid price.\nExample: BUY 1693"

    qty   = int(parts[2]) if len(parts) > 2 else int(CAPITAL / entry)
    state = set_trade(entry, qty, RISK_RUPEES)

    return (
        f"✅ Trade recorded — {TICKER}.NS\n"
        f"\n"
        f"Entry  ₹{state['entry']:,.2f} × {state['qty']} sh\n"
        f"SL     ₹{state['sl']:,.2f}  (−₹{round(entry - state['sl']):.0f})\n"
        f"T1     ₹{state['t1']:,.2f}  (+₹10)\n"
        f"T2     ₹{state['t2']:,.2f}  (+₹20)\n"
        f"T3     ₹{state['t3']:,.2f}  (+₹25)\n"
        f"\n"
        f"Alerts fire automatically as each level is crossed."
    )


def _handle_sell(parts: list[str]) -> str:
    trade = get_trade()
    if not trade:
        return "No active trade to close."

    price    = _live_price()
    pnl_data = current_pnl(price) if price > 0 else None
    clear_trade()

    if pnl_data and price > 0:
        sign = "+" if pnl_data["pnl"] >= 0 else ""
        return (
            f"✅ Trade closed — {TICKER}.NS\n"
            f"\n"
            f"Entry  ₹{pnl_data['entry']:,.2f}\n"
            f"Exit   ₹{price:,.2f}  ({pnl_data['pnl_per']:+.2f}/sh)\n"
            f"Qty    {pnl_data['qty']} sh\n"
            f"P&L    ₹{pnl_data['pnl']:+,.0f}"
        )
    return "✅ Trade closed."


def _handle_status(parts: list[str]) -> str:
    price = _live_price()
    if price <= 0:
        return "❌ Could not fetch live price right now."

    lines = []

    managed_active = os.getenv("MANAGED_CYCLE", "false").lower() == "true"

    if managed_active:
        # ── Managed-cycle position ────────────────────────────────────────
        # Under the managed cycle the Supertrend strategy is disabled and its
        # strategy_state.json is never updated — reading it would surface a
        # stale, already-sold position. Report the managed cycle's own state
        # (its file is cleared atomically on every exit) so STATUS reflects the
        # live session.
        from src.managed_cycle import get_position as managed_position
        pos = managed_position()
        if pos:
            entry   = float(pos["entry"])
            qty     = int(pos["qty"])
            sl      = float(pos["sl"])
            targets = pos.get("targets") or []
            pnl     = round((price - entry) * qty, 0)
            sign    = "+" if pnl >= 0 else ""
            lines += [
                f"🎯 *Managed Position*",
                f"Entry:   ₹{entry:,.2f} × {qty} shares",
                f"Current: ₹{price:,.2f}",
                f"P&L:     {sign}₹{pnl:,.0f}",
            ]
            for i, d in enumerate(targets):
                lvl = round(entry + float(d), 2)
                lines.append(f"T{i + 1}:      ₹{lvl:,.2f}  ({lvl - price:+.0f})")
            lines += [f"SL:      ₹{sl:,.2f}  ({sl - price:+.0f})", ""]
        else:
            lines += [
                f"🎯 *Managed cycle — flat*",
                f"No open position — watching to re-enter on an SMA7 dip.",
                "",
            ]
    else:
        # ── Auto-trade position (Supertrend strategy) ─────────────────────
        auto_state = load_state()
        auto_pos   = auto_state.get("position")
        if auto_pos:
            entry   = float(auto_pos["entry"])
            qty     = int(auto_pos["qty_remaining"])
            sl      = float(auto_pos["sl"])
            t1      = float(auto_pos["t1"])
            pnl     = round((price - entry) * qty, 0)
            sign    = "+" if pnl >= 0 else ""
            partial = " (partial booked ✅)" if auto_pos.get("partial_booked") else ""
            lines += [
                f"🤖 *Auto-Trade Position*{partial}",
                f"Entry:   ₹{entry:,.2f} × {qty} shares",
                f"Current: ₹{price:,.2f}",
                f"P&L:     {sign}₹{pnl:,.0f}",
                f"T1:      ₹{t1:,.2f}  ({t1 - price:+.0f})",
                f"SL:      ₹{sl:,.2f}  ({sl - price:+.0f})",
                "",
            ]

    # ── Manual trade position (BUY command) ───────────────────────────────
    trade = get_trade()
    if trade:
        p   = current_pnl(price)
        hit = p["levels_hit"]
        lines += [
            f"📱 *Manual Trade Position*",
            f"Entry:   ₹{p['entry']:,.2f} × {p['qty']} shares",
            f"Current: ₹{price:,.2f}  ({p['pnl_per']:+.2f}/sh)",
            f"P&L:     ₹{p['pnl']:+,.0f}",
            "",
        ]
        for label, level in [("T3", p["t3"]), ("T2", p["t2"]),
                              ("T1", p["t1"]), ("SL", p["sl"])]:
            tick = " ✅" if label in hit else ""
            dist = round(level - price, 2)
            lines.append(f"{label:<4} ₹{level:,.2f}  ({dist:+.2f}){tick}")

    if not lines:
        return "No active trades.\n\nSend BUY <price> to record a manual trade."

    return "\n".join(lines)


def _handle_help(parts: list[str]) -> str:
    auto = "✅ ON" if os.getenv("KITE_AUTO_TRADE") == "true" else "❌ OFF"
    return (
        f"📱 {TICKER} Trade Bot\n"
        f"\n"
        f"BUY <price>        record entry\n"
        f"BUY <price> <qty>  with custom qty\n"
        f"SELL               close trade\n"
        f"STATUS             live P&L\n"
        f"KITE               check auto-trading status\n"
        f"CRYPTO             BTC/ETH summary\n"
        f"TOKEN <token>      complete Kite daily auth\n"
        f"HELP               this message\n"
        f"\n"
        f"Auto-trading: {auto}\n"
        f"Example: BUY 1693"
    )


def _handle_kite(parts: list[str]) -> str:
    """Report whether Kite auto-trading will execute today."""
    from src.broker import kite_execution_status
    result = kite_execution_status()
    lines = [result["summary"], ""]
    tick = {True: "✅", False: "❌"}
    for c in result["checks"]:
        lines.append(f"{tick[c['ok']]} {c['name']}: {c['detail']}")
    return "\n".join(lines)


def _handle_crypto(parts: list[str]) -> str:
    """On-demand BTC/ETH summary (same read as the 8 AM / 8 PM briefings)."""
    try:
        from datetime import datetime, timezone, timedelta
        from crypto.data import fetch_crypto_daily, fetch_crypto_quote, fetch_usd_inr
        from crypto.signals import compute_crypto_signal
        from crypto.messages import format_evening_summary

        ist = timezone(timedelta(hours=5, minutes=30))
        usd = fetch_usd_inr()

        def _asset(sym: str):
            df = fetch_crypto_daily(sym, days=250)
            q  = fetch_crypto_quote(sym, usd)
            if df is None or len(df) < 30 or q is None:
                return None, None
            return q, compute_crypto_signal(df, q)

        bq, bs = _asset("BTC-USD")
        eq, es = _asset("ETH-USD")
        if not (bq and bs and eq and es):
            return "❌ Could not fetch crypto data right now. Try again shortly."
        return format_evening_summary(bq, bs, eq, es, datetime.now(ist))
    except Exception as e:
        return f"❌ Crypto error: {e}"


def _handle_token(parts: list[str]) -> str:
    """Complete Zerodha Kite daily auth by exchanging a request_token."""
    if not KITE_API_KEY or not KITE_API_SECRET:
        return "❌ KITE_API_KEY / KITE_API_SECRET not configured on server."
    if len(parts) < 2:
        return (
            "Usage: TOKEN <request_token>\n\n"
            "1. Open your Kite login URL (sent by the bot at 8:45 AM)\n"
            "2. Log in with your Zerodha credentials\n"
            "3. Copy the request_token from the redirect URL\n"
            "4. Send: TOKEN <that_token>"
        )
    try:
        from src.broker import KiteBroker
        broker = KiteBroker(KITE_API_KEY, KITE_API_SECRET)
        if broker.complete_auth(parts[1].strip()):
            return "✅ Kite authenticated — auto-trading active for today."
        return "❌ Auth failed. Check the request_token and try again."
    except Exception as e:
        return f"❌ Error: {e}"


_HANDLERS = {
    "BUY":    _handle_buy,
    "SELL":   _handle_sell,
    "STATUS": _handle_status,
    "CRYPTO": _handle_crypto,
    "HELP":   _handle_help,
    "TOKEN":  _handle_token,
    "KITE":   _handle_kite,
}


# ─────────────────────────────────────────────────────────────────────────────
# Webhook endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    # Validate Twilio request signature — rejects forged/replayed requests
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        signature = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(request.url, request.form, signature):
            return Response("Forbidden", status=403)

    from_num = request.form.get("From", "").replace("whatsapp:", "")

    if AUTHORIZED and from_num != AUTHORIZED:
        return Response(status=403)

    body  = request.form.get("Body", "").strip()
    parts = body.upper().split()
    cmd   = parts[0] if parts else ""

    handler = _HANDLERS.get(cmd)
    if handler:
        reply = handler(parts)
    else:
        reply = f"Unknown command: {body}\nSend HELP for commands."

    # Reply via the Twilio REST API — the same path as outbound alerts, which
    # deliver reliably. TwiML webhook responses were silently not delivered in
    # the sandbox, so we send the reply directly and ack the webhook with 204.
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM:
        from src.alerts import send_whatsapp_alert
        if send_whatsapp_alert(
            TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, from_num, reply
        ):
            return Response("", status=204)

    # Fallback: return TwiML with the correct XML content-type.
    resp = MessagingResponse()
    resp.message(reply)
    return Response(str(resp), mimetype="application/xml")


@app.route("/kite_callback")
def kite_callback():
    """
    Zerodha redirects here after the user logs in via the login URL.
    Exchanges request_token for access_token and notifies via WhatsApp.
    """
    status       = request.args.get("status", "")
    request_token = request.args.get("request_token", "")

    if status != "success" or not request_token:
        error = request.args.get("message", "Unknown error")
        return f"<h2>Kite login failed: {error}</h2>", 400

    if not KITE_API_KEY or not KITE_API_SECRET:
        return "<h2>KITE_API_KEY/SECRET not configured on server.</h2>", 500

    try:
        from src.broker import KiteBroker
        broker = KiteBroker(KITE_API_KEY, KITE_API_SECRET)
        if broker.complete_auth(request_token):
            wa_sid  = os.getenv("TWILIO_ACCOUNT_SID", "")
            wa_tok  = os.getenv("TWILIO_AUTH_TOKEN", "")
            wa_from = os.getenv("TWILIO_WHATSAPP_FROM", "")
            wa_to   = os.getenv("TWILIO_WHATSAPP_TO", "")
            if all([wa_sid, wa_tok, wa_from, wa_to]):
                from src.alerts import send_whatsapp_alert
                send_whatsapp_alert(wa_sid, wa_tok, wa_from, wa_to,
                    f"✅ Kite authenticated — auto-trading ACTIVE for today.")
            return "<h2>✅ Kite authenticated. Auto-trading is active. You can close this tab.</h2>"
        return "<h2>❌ Auth failed. Check server logs.</h2>", 500
    except Exception as e:
        return f"<h2>Error: {e}</h2>", 500


@app.route("/kite_postback", methods=["POST"])
def kite_postback():
    """Zerodha order postback — logs order status updates."""
    import logging
    logging.getLogger("kite_postback").info("postback: %s", request.json)
    return "", 200


@app.route("/health")
def health():
    if HEALTH_API_KEY:
        provided = (
            request.args.get("key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        if provided != HEALTH_API_KEY:
            return Response("Unauthorized", status=401)

    trade = get_trade()
    return {
        "status":       "ok",
        "ticker":       TICKER,
        "active_trade": bool(trade),
        "entry":        trade.get("entry") if trade else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _start_telegram_bot() -> None:
    """Start the Telegram command poller in a daemon thread, if configured."""
    if not TELEGRAM_TOKEN:
        return
    import threading
    from src.telegram_bot import run_command_bot

    t = threading.Thread(
        target=run_command_bot,
        args=(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, _HANDLERS),
        daemon=True,
    )
    t.start()
    print(f"   Telegram command bot: ON (chat_id={TELEGRAM_CHAT_ID or 'any'})")


if __name__ == "__main__":
    port = int(os.getenv("BOT_PORT", "5001"))
    print(f"\n🤖 {TICKER} Trade Bot")
    print(f"   Listening on http://localhost:{port}/whatsapp")
    print(f"   WhatsApp authorized number: {AUTHORIZED or 'all'}")
    print(f"   Telegram: {'configured' if TELEGRAM_TOKEN else 'OFF'}")
    print(f"   Commands: BUY <price>, SELL, STATUS, HELP\n")
    _start_telegram_bot()
    app.run(host="127.0.0.1", port=port, debug=False)
