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
import time
import logging
from argparse import ArgumentParser
from datetime import datetime, time as dtime, timedelta, timezone

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv

load_dotenv()

from src.sentiment import load_sentiment_model
from src.alerts import (
    send_alert,
    send_whatsapp_alert,
    format_alert,
    format_whatsapp_alert,
    should_alert,
)
from main import _refresh

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("main_headless")

_IST = timezone(timedelta(hours=5, minutes=30))
_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)
_WAKEUP_BEFORE_OPEN = timedelta(minutes=10)


def _now_ist() -> datetime:
    return datetime.now(_IST)


def _is_market_open(now: datetime | None = None) -> bool:
    """Return True if NSE is currently open (Mon–Fri, 9:15–15:30 IST)."""
    now = now or _now_ist()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now.time()
    return _MARKET_OPEN <= t <= _MARKET_CLOSE


def _sleep_until_market_open() -> None:
    """Sleep until 10 minutes before the next NSE market open, then return."""
    now = _now_ist()
    # Find next weekday 9:15 AM IST
    candidate = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now.time() >= _MARKET_CLOSE or now.weekday() >= 5:
        # Move to next day
        candidate += timedelta(days=1)
    # Skip weekends
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    wake_at = candidate - _WAKEUP_BEFORE_OPEN
    if wake_at <= now:
        return  # Already past wakeup; open is imminent
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


def main() -> None:
    args = parse_args()
    ticker = args.ticker
    refresh_seconds = args.refresh

    if args.quiet:
        logger.setLevel(logging.WARNING)

    logger.info("Starting EmcurePriceTracker headless service for %s", ticker)
    logger.info("Refresh interval: %ss", refresh_seconds)

    load_sentiment_model()
    last_alerted: dict = {}

    while True:
        if not _is_market_open():
            _sleep_until_market_open()
            continue

        started_at = datetime.now()
        data = _refresh(ticker)

        if not data:
            logger.warning(
                "No market data returned, retrying in %ss...", refresh_seconds
            )
        else:
            quote = data.get("quote") or {}
            score_result = data.get("score_result") or {}
            price = quote.get("price") or 0.0
            signal = score_result.get("signal", "Hold")
            score = score_result.get("score", 0.0)

            logger.info(
                "%s @ ₹%.2f | signal=%s | score=%.2f | change=%+.2f%%",
                ticker,
                price,
                signal,
                score,
                quote.get("change_pct", 0.0),
            )

            if score_result and quote and should_alert(score_result, last_alerted):
                alerted = False

                if os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
                    msg = format_alert(ticker, score_result, quote)
                    if send_alert(
                        os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"), msg
                    ):
                        logger.info("Telegram alert sent: %s", signal)
                        alerted = True
                    else:
                        logger.warning("Telegram alert failed")

                if (
                    os.getenv("TWILIO_ACCOUNT_SID")
                    and os.getenv("TWILIO_AUTH_TOKEN")
                    and os.getenv("TWILIO_WHATSAPP_FROM")
                    and os.getenv("TWILIO_WHATSAPP_TO")
                ):
                    wa_msg = format_whatsapp_alert(
                        ticker,
                        score_result,
                        quote,
                        target_probs=data.get("target_probs"),
                        intraday_probs=data.get("intraday_probs"),
                    )
                    if send_whatsapp_alert(
                        os.getenv("TWILIO_ACCOUNT_SID"),
                        os.getenv("TWILIO_AUTH_TOKEN"),
                        os.getenv("TWILIO_WHATSAPP_FROM"),
                        os.getenv("TWILIO_WHATSAPP_TO"),
                        wa_msg,
                    ):
                        logger.info("WhatsApp alert sent: %s", signal)
                        alerted = True
                    else:
                        logger.warning("WhatsApp alert failed")

                if alerted:
                    last_alerted[signal] = datetime.now()

        elapsed = (datetime.now() - started_at).total_seconds()
        sleep_for = max(1, refresh_seconds - int(elapsed))
        logger.debug("Sleeping for %s seconds", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down headless service")
