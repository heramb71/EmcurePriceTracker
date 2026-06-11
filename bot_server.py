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

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

TICKER            = os.getenv("TICKER", "EMCURE")
CAPITAL           = float(os.getenv("CAPITAL", "100000"))
RISK_RUPEES       = float(os.getenv("RISK_RUPEES", "4500"))
AUTHORIZED        = os.getenv("TWILIO_WHATSAPP_TO", "").replace("whatsapp:", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
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
    trade = get_trade()
    if not trade:
        return "No active trade.\n\nSend BUY <price> to record one."

    price = _live_price()
    if price <= 0:
        return "❌ Could not fetch live price right now."

    p = current_pnl(price)
    hit = p["levels_hit"]

    lines = [
        f"📊 {TICKER}.NS — Live Position",
        "",
        f"Entry    ₹{p['entry']:,.2f} × {p['qty']} sh",
        f"Current  ₹{price:,.2f}  ({p['pnl_per']:+.2f}/sh)",
        f"P&L      ₹{p['pnl']:+,.0f}",
        "",
    ]
    for label, level in [("T3", p["t3"]), ("T2", p["t2"]),
                          ("T1", p["t1"]), ("SL", p["sl"])]:
        tick = " ✅" if label in hit else ""
        dist = round(level - price, 2)
        lines.append(f"{label:<4} ₹{level:,.2f}  ({dist:+.2f}){tick}")

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
        f"TOKEN <token>      complete Kite daily auth\n"
        f"HELP               this message\n"
        f"\n"
        f"Auto-trading: {auto}\n"
        f"Example: BUY 1693"
    )


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
    "HELP":   _handle_help,
    "TOKEN":  _handle_token,
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

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


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

if __name__ == "__main__":
    port = int(os.getenv("BOT_PORT", "5001"))
    print(f"\n🤖 {TICKER} WhatsApp Trade Bot")
    print(f"   Listening on http://localhost:{port}/whatsapp")
    print(f"   Authorized number: {AUTHORIZED or 'all'}")
    print(f"   Commands: BUY <price>, SELL, STATUS, HELP\n")
    app.run(host="127.0.0.1", port=port, debug=False)
