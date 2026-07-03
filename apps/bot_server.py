#!/usr/bin/env python3
"""
WhatsApp + Telegram command bot — transport layer only.

Receives inbound WhatsApp messages via the Twilio webhook and Telegram
messages via the long-poller, and dispatches both into the shared command
registry in src/emcure/commands.py (BUY/SELL/STATUS/EXIT/HALT/RESUME/…).
Also serves /health, /dashboard, and the Zerodha Kite auth callbacks.

Run via:  python -m apps.bot_server
Or use:   ./start_bot.sh   (starts bot + ngrok tunnel together)
"""
from __future__ import annotations

import os

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, Response, request
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from werkzeug.middleware.proxy_fix import ProxyFix

from src.emcure import ledger
from src.emcure.commands import HANDLERS as _HANDLERS
from src.emcure.commands import live_price as _live_price
from src.emcure.trade_manager import current_pnl, get_trade
from src.notify import channels

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

TICKER            = os.getenv("TICKER", "EMCURE")
AUTHORIZED           = os.getenv("TWILIO_WHATSAPP_TO", "").replace("whatsapp:", "")
TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
# Emcure command bot: BUY/SELL/STATUS route to the Emcure Telegram feed
# (falls back to the shared TELEGRAM_TOKEN / TELEGRAM_CHAT_ID).
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID = channels.telegram_config("emcure")
HEALTH_API_KEY    = os.getenv("HEALTH_API_KEY", "")
KITE_API_KEY      = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET   = os.getenv("KITE_API_SECRET", "")


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
        from src.notify.alerts import send_whatsapp_alert
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
        from src.execution.broker import KiteBroker
        broker = KiteBroker(KITE_API_KEY, KITE_API_SECRET)
        if broker.complete_auth(request_token):
            wa_sid  = os.getenv("TWILIO_ACCOUNT_SID", "")
            wa_tok  = os.getenv("TWILIO_AUTH_TOKEN", "")
            wa_from = os.getenv("TWILIO_WHATSAPP_FROM", "")
            wa_to   = os.getenv("TWILIO_WHATSAPP_TO", "")
            if all([wa_sid, wa_tok, wa_from, wa_to]):
                from src.notify.alerts import send_whatsapp_alert
                send_whatsapp_alert(wa_sid, wa_tok, wa_from, wa_to,
                    "✅ Kite authenticated — auto-trading ACTIVE for today.")
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


def _authorized() -> bool:
    """Same key gate as /health — if HEALTH_API_KEY is unset, allow (local dev)."""
    if not HEALTH_API_KEY:
        return True
    provided = (
        request.args.get("key")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    return provided == HEALTH_API_KEY


def _dashboard_context() -> dict:
    """Assemble the read-only dashboard context from live sources."""
    from datetime import datetime, timedelta, timezone

    from src.emcure import schedule
    from src.emcure.managed_cycle import get_position as managed_get_position
    from src.shared import heartbeat

    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    market_open = schedule.is_market_open(now)

    price = _live_price()
    position = None
    trade = get_trade()
    if trade:
        pnl = current_pnl(price) if price > 0 else None
        position = {"source": "manual", "entry": trade.get("entry"),
                    "qty": trade.get("qty"), "price": price,
                    "pnl": pnl.get("pnl") if pnl else None}
    else:
        mp = managed_get_position()
        if mp:
            entry, qty = mp.get("entry", 0), mp.get("qty", 0)
            position = {"source": "managed", "entry": entry, "qty": qty, "price": price,
                        "pnl": round((price - entry) * qty, 0) if price > 0 else None}

    conn = ledger.connect()
    try:
        ctx = {
            "ticker": TICKER,
            "now": now.strftime("%Y-%m-%d %H:%M IST"),
            "market_open": market_open,
            "heartbeat_age": heartbeat.age_seconds(),
            "position": position,
            "summary": ledger.summary(conn),
            "by_strategy": ledger.by_strategy(conn),
            "recent_trades": ledger.recent_trades(conn, limit=10),
        }
    finally:
        conn.close()
    return ctx


@app.route("/dashboard")
def dashboard():
    if not _authorized():
        return Response("Unauthorized", status=401)
    from src.emcure.dashboard_web import render_dashboard
    return Response(render_dashboard(_dashboard_context()), mimetype="text/html")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _start_telegram_bot() -> None:
    """Start the Telegram command poller in a daemon thread, if configured."""
    if not TELEGRAM_TOKEN:
        return
    import threading

    from src.notify.telegram_bot import run_command_bot

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
    print(f"   Dashboard:  http://localhost:{port}/dashboard{'?key=…' if HEALTH_API_KEY else ''}")
    print(f"   WhatsApp authorized number: {AUTHORIZED or 'all'}")
    print(f"   Telegram: {'configured' if TELEGRAM_TOKEN else 'OFF'}")
    print(f"   Commands: BUY <price>, SELL [price], STATUS, EXIT, HALT, RESUME, HELP\n")
    _start_telegram_bot()
    # threaded=True: a slow handler (CRYPTO/STATUS do multi-second yfinance
    # fetches) must not stall a concurrent Twilio webhook past its ~15s timeout.
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
