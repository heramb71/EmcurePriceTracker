"""
Command handlers for the trading bots — one registry, two transports.

Both the Twilio WhatsApp webhook and the Telegram long-poller
(apps/bot_server.py) dispatch into ``HANDLERS`` below. Extracted from
bot_server so the command logic is unit-testable without importing
Flask/Twilio, and reusable by any future transport. Each handler takes the
upper-cased, whitespace-split message parts and returns a plain-text reply.

Config (TICKER/CAPITAL/RISK_RUPEES/…) is read from the environment per call —
cheap, and it lets tests monkeypatch the environment without reload tricks.
"""
from __future__ import annotations

import logging
import os

from src.emcure import ledger
from src.emcure.managed_cycle import get_position as managed_position
from src.emcure.managed_cycle import request_exit, set_halted
from src.emcure.state import load_state
from src.emcure.trade_manager import clear_trade, current_pnl, get_trade, set_trade
from src.shared.costs import round_trip_charges

logger = logging.getLogger(__name__)


def _ticker() -> str:
    return os.getenv("TICKER", "EMCURE")


def _capital() -> float:
    return float(os.getenv("CAPITAL", "100000"))


def _risk_rupees() -> float:
    return float(os.getenv("RISK_RUPEES", "4500"))


def _managed_enabled() -> bool:
    return os.getenv("MANAGED_CYCLE", "false").lower() == "true"


# ─────────────────────────────────────────────────────────────────────────────
# Live price — the engine's price, not a delayed lookalike
# ─────────────────────────────────────────────────────────────────────────────

_broker_cache: list = []   # lazy singleton — construction loads the token file


def _kite_broker():
    """An authenticated KiteBroker, or None. Cached across calls."""
    key    = os.getenv("KITE_API_KEY", "")
    secret = os.getenv("KITE_API_SECRET", "")
    if not key or not secret:
        return None
    try:
        from src.execution.broker import KiteBroker
        if not _broker_cache:
            _broker_cache.append(KiteBroker(key, secret))
        broker = _broker_cache[0]
        return broker if broker.is_authenticated() else None
    except Exception:
        logger.warning("commands: Kite broker unavailable", exc_info=True)
        return None


def live_price() -> float:
    """Real-time Kite LTP when authenticated — the same price the trading
    engine acts on — falling back to the ~15-min-delayed yfinance quote.
    Returns 0.0 when neither source responds."""
    broker = _kite_broker()
    if broker is not None:
        try:
            ltp = broker.get_ltp(_ticker())
            if ltp and ltp > 0:
                return round(float(ltp), 2)
        except Exception:
            logger.warning("commands: Kite LTP failed; using yfinance", exc_info=True)
    try:
        import yfinance as yf
        return round(float(yf.Ticker(f"{_ticker()}.NS").fast_info.last_price), 2)
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Handlers — each returns a plain-text reply string
# ─────────────────────────────────────────────────────────────────────────────

def handle_buy(parts: list[str]) -> str:
    if len(parts) < 2:
        return "Usage: BUY <price> [qty]\nExample: BUY 1693"
    try:
        entry = float(parts[1])
    except ValueError:
        return "❌ Invalid price.\nExample: BUY 1693"
    if entry <= 0:
        return "❌ Price must be positive.\nExample: BUY 1693"

    if len(parts) > 2:
        try:
            qty = int(parts[2])
        except ValueError:
            return "❌ Invalid qty.\nExample: BUY 1693 60"
    else:
        qty = int(_capital() / entry)
    if qty <= 0:
        return (
            f"❌ Qty works out to 0 — ₹{entry:,.0f} exceeds CAPITAL ₹{_capital():,.0f}.\n"
            f"Pass it explicitly: BUY {parts[1]} <qty>"
        )
    state = set_trade(entry, qty, _risk_rupees())

    return (
        f"✅ Trade recorded — {_ticker()}.NS\n"
        f"\n"
        f"Entry  ₹{state['entry']:,.2f} × {state['qty']} sh\n"
        f"SL     ₹{state['sl']:,.2f}  (−₹{round(entry - state['sl']):.0f})\n"
        f"T1     ₹{state['t1']:,.2f}  (+₹10)\n"
        f"T2     ₹{state['t2']:,.2f}  (+₹20)\n"
        f"T3     ₹{state['t3']:,.2f}  (+₹25)\n"
        f"\n"
        f"Alerts fire automatically as each level is crossed."
    )


def handle_sell(parts: list[str]) -> str:
    trade = get_trade()
    if not trade:
        return "No active trade to close."

    # SELL [price] — an explicit price closes at that price; otherwise use the
    # live quote. If neither is available the trade is NOT closed: silently
    # clearing it would drop the round-trip from the P&L ledger.
    if len(parts) > 1:
        try:
            price = float(parts[1])
        except ValueError:
            return "❌ Invalid price.\nUsage: SELL [price]"
    else:
        price = live_price()
    if price <= 0:
        return (
            "❌ Couldn't fetch a live price — trade NOT closed.\n"
            "Try again in a minute, or close at a known price: SELL <price>"
        )

    pnl_data = current_pnl(price)
    clear_trade()
    charges = round_trip_charges(pnl_data["entry"], price, pnl_data["qty"])
    net = round(pnl_data["pnl"] - charges, 2)
    ledger.log_trade(
        strategy="manual", ticker=_ticker(), qty=pnl_data["qty"],
        entry_price=pnl_data["entry"], exit_price=price,
        pnl=pnl_data["pnl"], charges=charges, exit_reason="manual",
        opened_at=trade.get("opened_at"),
    )
    return (
        f"✅ Trade closed — {_ticker()}.NS\n"
        f"\n"
        f"Entry    ₹{pnl_data['entry']:,.2f}\n"
        f"Exit     ₹{price:,.2f}  ({pnl_data['pnl_per']:+.2f}/sh)\n"
        f"Qty      {pnl_data['qty']} sh\n"
        f"Gross    ₹{pnl_data['pnl']:+,.0f}\n"
        f"Charges  ₹{charges:,.0f}\n"
        f"Net P&L  ₹{net:+,.0f}"
    )


def handle_status(parts: list[str]) -> str:
    price = live_price()
    if price <= 0:
        return "❌ Could not fetch live price right now."

    lines = []

    if _managed_enabled():
        # ── Managed-cycle position ────────────────────────────────────────
        # Under the managed cycle the Supertrend strategy is disabled and its
        # strategy_state.json is never updated — reading it would surface a
        # stale, already-sold position. Report the managed cycle's own state
        # (its file is cleared atomically on every exit) so STATUS reflects the
        # live session.
        pos = managed_position()
        if pos:
            entry   = float(pos["entry"])
            qty     = int(pos["qty"])
            sl      = float(pos["sl"])
            targets = pos.get("targets") or []
            pnl     = round((price - entry) * qty, 0)
            sign    = "+" if pnl >= 0 else ""
            lines += [
                "🎯 *Managed Position*",
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
                "🎯 *Managed cycle — flat*",
                "No open position — watching to re-enter on an SMA7 dip.",
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
            "📱 *Manual Trade Position*",
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


def handle_exit(parts: list[str]) -> str:
    """Queue a managed-cycle exit — the tracker sells on its next step. Before
    this, the only way to close the managed position from the phone was to sell
    in Zerodha and wait for the external-close reconcile."""
    if not _managed_enabled():
        return "Managed cycle is not enabled (MANAGED_CYCLE=false) — nothing to exit."
    pos = request_exit()
    if not pos:
        return "Managed cycle is flat — no position to exit."
    return (
        f"🛑 EXIT queued — {_ticker()}\n"
        f"The tracker will sell {pos['qty']} shares (entry ₹{pos['entry']:,.2f}) "
        f"at the current price on its next cycle (within ~5 min). "
        f"You'll get the usual Sold alert when it's done."
    )


def handle_halt(parts: list[str]) -> str:
    if not _managed_enabled():
        return "Managed cycle is not enabled (MANAGED_CYCLE=false) — nothing to halt."
    set_halted(True)
    note = (
        "The open position stays managed — targets and stop still act; only new buys stop."
        if managed_position() else "No position is open."
    )
    return f"⏸️ Managed cycle HALTED — no new buys until you send RESUME.\n{note}"


def handle_resume(parts: list[str]) -> str:
    if not _managed_enabled():
        return "Managed cycle is not enabled (MANAGED_CYCLE=false)."
    set_halted(False)
    return "▶️ Managed cycle resumed — re-entries are allowed again."


def handle_help(parts: list[str]) -> str:
    auto = "✅ ON" if os.getenv("KITE_AUTO_TRADE") == "true" else "❌ OFF"
    return (
        f"📱 {_ticker()} Trade Bot\n"
        f"\n"
        f"BUY <price>        record entry\n"
        f"BUY <price> <qty>  with custom qty\n"
        f"SELL [price]       close manual trade\n"
        f"STATUS             live P&L\n"
        f"EXIT               sell the managed position\n"
        f"HALT               pause managed re-entries\n"
        f"RESUME             re-enable managed re-entries\n"
        f"KITE               check auto-trading status\n"
        f"CRYPTO             BTC/ETH summary\n"
        f"TOKEN <token>      complete Kite daily auth\n"
        f"HELP               this message\n"
        f"\n"
        f"Auto-trading: {auto}\n"
        f"Example: BUY 1693"
    )


def handle_kite(parts: list[str]) -> str:
    """Report whether Kite auto-trading will execute today."""
    from src.execution.broker import kite_execution_status
    result = kite_execution_status()
    lines = [result["summary"], ""]
    tick = {True: "✅", False: "❌"}
    for c in result["checks"]:
        lines.append(f"{tick[c['ok']]} {c['name']}: {c['detail']}")
    return "\n".join(lines)


def handle_crypto(parts: list[str]) -> str:
    """On-demand BTC/ETH summary (same read as the 8 AM / 8 PM briefings)."""
    try:
        from datetime import datetime, timedelta, timezone

        from src.crypto.data import fetch_crypto_daily, fetch_crypto_quote, fetch_usd_inr
        from src.crypto.messages import format_evening_summary
        from src.crypto.signals import compute_crypto_signal

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


def handle_token(parts: list[str]) -> str:
    """Complete Zerodha Kite daily auth by exchanging a request_token."""
    key    = os.getenv("KITE_API_KEY", "")
    secret = os.getenv("KITE_API_SECRET", "")
    if not key or not secret:
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
        from src.execution.broker import KiteBroker
        broker = KiteBroker(key, secret)
        if broker.complete_auth(parts[1].strip()):
            return "✅ Kite authenticated — auto-trading active for today."
        return "❌ Auth failed. Check the request_token and try again."
    except Exception as e:
        return f"❌ Error: {e}"


HANDLERS = {
    "BUY":    handle_buy,
    "SELL":   handle_sell,
    "STATUS": handle_status,
    "EXIT":   handle_exit,
    "HALT":   handle_halt,
    "RESUME": handle_resume,
    "CRYPTO": handle_crypto,
    "HELP":   handle_help,
    "TOKEN":  handle_token,
    "KITE":   handle_kite,
}
