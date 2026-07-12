#!/usr/bin/env python3
"""
Headless crypto trend tracker — BTC and ETH, 7 days a week.

Sends WhatsApp alerts via the same Twilio credentials as the Emcure bot.
Crypto markets never close so there is no market-open gate; the loop runs
continuously with a configurable refresh interval.

Schedule (IST, every day including weekends):
  08:00 – 08:14 AM  Morning briefing (BTC + ETH prices, trend, RSI, signal)
  08:00 – 08:14 PM  Evening summary (full technical read with EMA/ATR)
  Intraday (any time) Signal alert when RSI < 35 or > 68, per asset,
                      with a 4-hour cooldown to prevent spam

Environment variables (all optional — fall back to .env defaults):
  CRYPTO_REFRESH_SECONDS   Polling interval in seconds (default: 600)

All other credentials (TWILIO_*) are shared with the Emcure bot via .env.

Run locally:
  python3 crypto_headless.py

Deploy on Oracle Cloud (as a separate systemd service):
  sudo cp deploy/crypto.service /etc/systemd/system/
  sudo systemctl enable --now crypto-tracker
  journalctl -u crypto-tracker -f
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv

load_dotenv()

from src.crypto import outcomes
from src.crypto import portfolio as pf
from src.crypto.data import fetch_crypto_daily, fetch_crypto_quote, fetch_usd_inr
from src.crypto.messages import (
    format_evening_summary,
    format_morning_briefing,
    format_signal_alert,
)
from src.crypto.portfolio_messages import (
    format_book_profit_alert,
    format_dip_buy_alert,
    format_portfolio_block,
)
from src.crypto.signals import compute_crypto_signal, is_alert_worthy
from src.notify import channels
from src.notify.alerts import send_alert, send_whatsapp_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("crypto_headless")

_IST = timezone(timedelta(hours=5, minutes=30))

_ASSETS = [
    ("Bitcoin",  "BTC", "BTC-USD"),
    ("Ethereum", "ETH", "ETH-USD"),
]

_MORNING_HOUR = 8
_EVENING_HOUR = 20
_WINDOW_MINUTES = 14          # briefing window: HH:00 – HH:14
_SIGNAL_COOLDOWN_H = 4        # hours between intraday alerts per asset
_REFRESH_SECONDS = int(os.getenv("CRYPTO_REFRESH_SECONDS", "600"))
_SEND_RETRY_DELAY_S = 2


def _now_ist() -> datetime:
    return datetime.now(_IST)


def _retry_send(send_fn, *args) -> bool:
    """Call a send function, retrying once after a short delay. Returns True if
    either attempt succeeds — so a single transient network blip never silently
    drops an alert. Mirrors main_headless._retry_send."""
    if send_fn(*args):
        return True
    time.sleep(_SEND_RETRY_DELAY_S)
    return send_fn(*args)


def _in_window(now: datetime, hour: int) -> bool:
    return now.hour == hour and now.minute <= _WINDOW_MINUTES


def _portfolio_block(
    port: dict | None,
    btc_quote: dict, btc_sig: dict,
    eth_quote: dict, eth_sig: dict,
    usd_inr: float,
) -> str | None:
    """Build the briefing portfolio section, fetching quotes for any extra
    held coins (DOGE, TUSD, …). Called only inside briefing windows so the
    extra fetches happen twice a day, not every cycle."""
    if not port:
        return None
    prices = {"BTC": btc_quote["price_inr"], "ETH": eth_quote["price_inr"]}
    for sym in port["holdings"]:
        yf_sym = pf.YF_SYMBOLS.get(sym)
        if sym not in prices and yf_sym:
            q = fetch_crypto_quote(yf_sym, usd_inr)
            if q:
                prices[sym] = q["price_inr"]
    summary = pf.portfolio_summary(port, prices)
    if summary is None:
        return None
    return format_portfolio_block(port, summary, {"BTC": btc_sig, "ETH": eth_sig}, usd_inr)


def _check_portfolio_alerts(
    port: dict | None,
    assets: list[tuple[str, str, dict, dict]],
    now: datetime,
    last_alerted: dict,
    send,
) -> None:
    """Position-relative intraday alerts: book-profit band + SMA7 dip zone.
    Each fires at most once per symbol per day."""
    if not port:
        return
    plan = port["plan"]
    for name, sym, quote, sig in assets:
        holding = port["holdings"].get(sym)
        stats = pf.holding_stats(sym, holding, quote["price_inr"]) if holding else None

        if stats:
            level = pf.should_book_profit(stats, sig, plan)
            key = f"book_{sym}_{now.date()}"
            if level and key not in last_alerted:
                send(format_book_profit_alert(name, sym, quote, stats, sig, plan, level, now))
                last_alerted[key] = now
                logger.info("Book-profit alert: %s %s (%+.1f%%)", sym, level, stats["pnl_pct"])

        dip = pf.dip_level(sig, plan)
        key = f"dip_{sym}_{now.date()}"
        if dip and key not in last_alerted:
            send(format_dip_buy_alert(name, sym, quote, sig, stats, plan, dip, now))
            last_alerted[key] = now
            logger.info("Dip-buy alert: %s %s (gap %+.1f%%)", sym, dip, sig["sma7_gap_pct"])


def _fetch_asset(yf_symbol: str, usd_inr: float) -> tuple[dict | None, dict | None]:
    """Return (quote, signal) for one asset, or (None, None) on failure."""
    df = fetch_crypto_daily(yf_symbol, days=250)
    if df is None or len(df) < 30:
        logger.warning("Insufficient daily data for %s", yf_symbol)
        return None, None

    quote = fetch_crypto_quote(yf_symbol, usd_inr)
    if quote is None:
        return None, None

    sig = compute_crypto_signal(df, quote)
    return quote, sig


def main() -> None:
    wa_sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
    wa_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    wa_from  = os.getenv("TWILIO_WHATSAPP_FROM", "")
    wa_to    = os.getenv("TWILIO_WHATSAPP_TO", "")
    tg_token, tg_chat_id = channels.telegram_config("crypto")
    wa_ready = channels.whatsapp_enabled() and bool(wa_sid and wa_token and wa_from and wa_to)
    tg_ready = bool(tg_token and tg_chat_id)
    if not (wa_ready or tg_ready):
        logger.error("No alert channel configured — set TWILIO_* and/or TELEGRAM_* in .env")

    def _wa(msg: str) -> None:
        """Fan out to WhatsApp (50/day cap) and Telegram, retrying each leg once
        on a transient failure so a single network blip never drops an alert."""
        if wa_ready and not _retry_send(send_whatsapp_alert, wa_sid, wa_token, wa_from, wa_to, msg):
            logger.error("WhatsApp alert dropped after retry (%d chars)", len(msg))
        if tg_ready and not _retry_send(send_alert, tg_token, tg_chat_id, msg):
            logger.error("Telegram alert dropped after retry (%d chars)", len(msg))

    last_alerted: dict = {}
    # Forward-outcome tracking (crypto.db): every fired alert is recorded and
    # scored at 1d/3d/7d so the alerts build an evidence base — the gate any
    # future crypto trading must pass. Read the report:
    # python -m apps.crypto_outcomes
    outcomes_conn = outcomes.connect()
    logger.info("Crypto tracker started. Refresh every %ds.", _REFRESH_SECONDS)
    logger.info("WhatsApp: %s | Telegram: %s",
                "on" if wa_ready else "off",
                "on" if tg_ready else "off")

    while True:
        now = _now_ist()
        started_at = now

        usd_inr = fetch_usd_inr()
        logger.info("USD/INR: %.2f", usd_inr)

        btc_quote, btc_sig = _fetch_asset("BTC-USD", usd_inr)
        eth_quote, eth_sig = _fetch_asset("ETH-USD", usd_inr)

        if btc_quote and btc_sig and eth_quote and eth_sig:
            _log_state(btc_quote, btc_sig, eth_quote, eth_sig)

            # Holdings file is re-read each cycle so edits (new tranches,
            # changed plan) take effect without a restart. None → no portfolio
            # features, tracker behaves exactly as before.
            port = pf.load_portfolio()

            # Book matured forward outcomes for previously recorded alerts.
            written = outcomes.evaluate_due(
                outcomes_conn,
                {"BTC": btc_quote["price_usd"], "ETH": eth_quote["price_usd"]},
                now,
            )
            if written:
                logger.info("Crypto outcomes recorded this cycle: %d", written)

            # ── Morning briefing ──────────────────────────────────────────────
            if _in_window(now, _MORNING_HOUR):
                key = f"morning_{now.date()}"
                if key not in last_alerted:
                    pblock = _portfolio_block(port, btc_quote, btc_sig, eth_quote, eth_sig, usd_inr)
                    msg = format_morning_briefing(btc_quote, btc_sig, eth_quote, eth_sig, now, pblock)
                    _wa(msg)
                    last_alerted[key] = now
                    logger.info("Morning briefing sent")

            # ── Evening summary ───────────────────────────────────────────────
            if _in_window(now, _EVENING_HOUR):
                key = f"evening_{now.date()}"
                if key not in last_alerted:
                    pblock = _portfolio_block(port, btc_quote, btc_sig, eth_quote, eth_sig, usd_inr)
                    msg = format_evening_summary(btc_quote, btc_sig, eth_quote, eth_sig, now, pblock)
                    _wa(msg)
                    last_alerted[key] = now
                    logger.info("Evening summary sent")

            # ── Intraday signal alerts (per asset, 4h cooldown) ───────────────
            # Skip during scheduled windows to avoid duplicate messages.
            in_scheduled_window = (
                _in_window(now, _MORNING_HOUR) or
                _in_window(now, _EVENING_HOUR)
            )
            if not in_scheduled_window:
                for name, sym, _ in _ASSETS:
                    quote = btc_quote if sym == "BTC" else eth_quote
                    sig   = btc_sig   if sym == "BTC" else eth_sig

                    alert_key = f"signal_{sym}"
                    last_t = last_alerted.get(alert_key)
                    cooldown_ok = (
                        last_t is None
                        or (now - last_t).total_seconds() >= _SIGNAL_COOLDOWN_H * 3600
                    )

                    if cooldown_ok and is_alert_worthy(sig):
                        msg = format_signal_alert(name, sym, quote, sig, now)
                        _wa(msg)
                        outcomes.record_alert(outcomes_conn, sym, sig, quote, now)
                        last_alerted[alert_key] = now
                        logger.info(
                            "Signal alert: %s | %s | RSI %.0f | score %.2f",
                            sym, sig["signal"], sig["rsi"], sig["score"],
                        )

                # Position-relative alerts (book-profit / dip-buy tranche).
                _check_portfolio_alerts(
                    port,
                    [("Bitcoin", "BTC", btc_quote, btc_sig),
                     ("Ethereum", "ETH", eth_quote, eth_sig)],
                    now, last_alerted, _wa,
                )
        else:
            logger.warning("Data fetch failed for one or both assets — will retry")

        elapsed = (_now_ist() - started_at).total_seconds()
        sleep_for = max(60, _REFRESH_SECONDS - int(elapsed))
        logger.debug("Sleeping %ds", sleep_for)
        time.sleep(sleep_for)


def _log_state(
    btc_quote: dict, btc_sig: dict, eth_quote: dict, eth_sig: dict
) -> None:
    logger.info(
        "BTC ₹%s | %s | RSI %.0f | score %.2f",
        f"{btc_quote['price_inr']:,.0f}", btc_sig["signal"], btc_sig["rsi"], btc_sig["score"],
    )
    logger.info(
        "ETH ₹%s | %s | RSI %.0f | score %.2f",
        f"{eth_quote['price_inr']:,.0f}", eth_sig["signal"], eth_sig["rsi"], eth_sig["score"],
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Crypto tracker stopped")
