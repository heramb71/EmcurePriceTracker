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

from src.alerts import send_whatsapp_alert
from crypto.data import fetch_crypto_daily, fetch_crypto_quote, fetch_usd_inr
from crypto.messages import (
    format_evening_summary,
    format_morning_briefing,
    format_signal_alert,
)
from crypto.signals import compute_crypto_signal, is_alert_worthy

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


def _now_ist() -> datetime:
    return datetime.now(_IST)


def _in_window(now: datetime, hour: int) -> bool:
    return now.hour == hour and now.minute <= _WINDOW_MINUTES


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

    wa_ready = bool(wa_sid and wa_token and wa_from and wa_to)
    if not wa_ready:
        logger.error("WhatsApp credentials missing — check TWILIO_* in .env")

    def _wa(msg: str) -> None:
        if wa_ready:
            send_whatsapp_alert(wa_sid, wa_token, wa_from, wa_to, msg)

    last_alerted: dict = {}
    logger.info("Crypto tracker started. Refresh every %ds.", _REFRESH_SECONDS)
    logger.info("WhatsApp alerts: %s", "enabled" if wa_ready else "DISABLED")

    while True:
        now = _now_ist()
        started_at = now

        usd_inr = fetch_usd_inr()
        logger.info("USD/INR: %.2f", usd_inr)

        btc_quote, btc_sig = _fetch_asset("BTC-USD", usd_inr)
        eth_quote, eth_sig = _fetch_asset("ETH-USD", usd_inr)

        if btc_quote and btc_sig and eth_quote and eth_sig:
            _log_state(btc_quote, btc_sig, eth_quote, eth_sig)

            # ── Morning briefing ──────────────────────────────────────────────
            if _in_window(now, _MORNING_HOUR):
                key = f"morning_{now.date()}"
                if key not in last_alerted:
                    msg = format_morning_briefing(btc_quote, btc_sig, eth_quote, eth_sig, now)
                    _wa(msg)
                    last_alerted[key] = now
                    logger.info("Morning briefing sent")

            # ── Evening summary ───────────────────────────────────────────────
            if _in_window(now, _EVENING_HOUR):
                key = f"evening_{now.date()}"
                if key not in last_alerted:
                    msg = format_evening_summary(btc_quote, btc_sig, eth_quote, eth_sig, now)
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
                        last_alerted[alert_key] = now
                        logger.info(
                            "Signal alert: %s | %s | RSI %.0f | score %.2f",
                            sym, sig["signal"], sig["rsi"], sig["score"],
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
