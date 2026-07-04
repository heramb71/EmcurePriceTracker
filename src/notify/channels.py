"""Alert-channel resolution — one place that decides *where* alerts go.

Telegram is the primary alert channel. WhatsApp (Twilio) is an opt-in fan-out,
gated behind ``WHATSAPP_ENABLED`` (default off) because Twilio's trial caps at
50 msgs/day and silently drops anything over the limit.

Each service owns its own Telegram bot so the three feeds never mix:

    ┌────────────┬──────────────────────────────┬───────────────────────────┐
    │ service    │ token env                    │ chat-id env               │
    ├────────────┼──────────────────────────────┼───────────────────────────┤
    │ "emcure"   │ TELEGRAM_EMCURE_TOKEN        │ TELEGRAM_EMCURE_CHAT_ID   │
    │ "radar"    │ TELEGRAM_RADAR_TOKEN         │ TELEGRAM_RADAR_CHAT_ID    │
    │ "crypto"   │ TELEGRAM_CRYPTO_TOKEN        │ TELEGRAM_CRYPTO_CHAT_ID   │
    └────────────┴──────────────────────────────┴───────────────────────────┘

KittyBot (the intraday trader that supersedes the radar scanner) reuses the
``radar`` feed above — it inherits the bot you already watch, so retiring the
scanner just changes what that bot sends, not where.

Any per-service value left blank falls back to the shared ``TELEGRAM_TOKEN`` /
``TELEGRAM_CHAT_ID`` — so an existing single-bot deployment keeps working
unchanged, and you can split feeds out one bot at a time.
"""
from __future__ import annotations

import os

# The isolated alert feeds. Keep in sync with the env table above.
SERVICES = ("emcure", "radar", "crypto")


def whatsapp_enabled() -> bool:
    """True when WhatsApp fan-out is turned on.

    Defaults to *off*: Telegram is the primary channel. Set
    ``WHATSAPP_ENABLED=true`` to additionally fan alerts out to Twilio WhatsApp
    (subject to the 50/day trial cap).
    """
    return os.getenv("WHATSAPP_ENABLED", "false").strip().lower() == "true"


def telegram_config(service: str) -> tuple[str, str]:
    """Return ``(token, chat_id)`` for a service's dedicated Telegram bot.

    Falls back to the shared ``TELEGRAM_TOKEN`` / ``TELEGRAM_CHAT_ID`` for any
    value the service does not override, so partial configuration is valid.
    Returns empty strings when nothing is configured (callers treat that as
    "Telegram off").
    """
    if service not in SERVICES:
        raise ValueError(f"unknown alert service {service!r}; expected one of {SERVICES}")

    shared_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    shared_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    prefix = f"TELEGRAM_{service.upper()}"
    token = os.getenv(f"{prefix}_TOKEN", "").strip() or shared_token
    chat_id = os.getenv(f"{prefix}_CHAT_ID", "").strip() or shared_chat
    return token, chat_id
