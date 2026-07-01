#!/usr/bin/env python3
"""
Send a WhatsApp alert with live market data.

Usage:
  python3 send_whatsapp_now.py
  TICKER=RELIANCE python3 send_whatsapp_now.py
"""

import os
import sys

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv

load_dotenv()

from src.market_intel.sentiment import load_sentiment_model
from src.notify.alerts import send_whatsapp_alert, format_whatsapp_alert
from apps.main import _refresh

TICKER = os.getenv("TICKER", "EMCURE")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
TWILIO_WHATSAPP_TO = os.getenv("TWILIO_WHATSAPP_TO", "")


def main() -> None:
    if not all(
        [TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]
    ):
        print("❌ Missing Twilio credentials in .env")
        print("   Required:")
        print("   - TWILIO_ACCOUNT_SID")
        print("   - TWILIO_AUTH_TOKEN")
        print("   - TWILIO_WHATSAPP_FROM")
        print("   - TWILIO_WHATSAPP_TO")
        sys.exit(1)

    print(f"📲 Fetching live data for {TICKER}…")
    load_sentiment_model()

    data = _refresh(TICKER)
    if not data:
        print("❌ No market data")
        sys.exit(1)

    quote = data.get("quote") or {}
    score_result = data.get("score_result") or {}
    target_probs = data.get("target_probs") or {}
    intraday_probs = data.get("intraday_probs") or {}

    if not quote or not score_result:
        print("❌ Incomplete data")
        sys.exit(1)

    msg = format_whatsapp_alert(
        TICKER,
        score_result,
        quote,
        target_probs=target_probs,
        intraday_probs=intraday_probs,
        buy_signal=data.get("buy_signal"),
        strategy_state=data.get("strategy_state"),
        pnl_unrealised=data.get("pnl_unrealised", 0.0),
        halted_reason=data.get("halted_reason", ""),
    )

    print("\n📨 Message preview:\n")
    print(msg)
    print("\n")

    if (
        send_whatsapp_alert(
            TWILIO_ACCOUNT_SID,
            TWILIO_AUTH_TOKEN,
            TWILIO_WHATSAPP_FROM,
            TWILIO_WHATSAPP_TO,
            msg,
        )
    ):
        print("✅ WhatsApp message sent successfully!")
    else:
        print("❌ Failed to send WhatsApp message")
        sys.exit(1)


if __name__ == "__main__":
    main()
