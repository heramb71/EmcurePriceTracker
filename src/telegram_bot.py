"""
Telegram command bot — long-polls getUpdates and dispatches commands.

Reuses the same command handlers as the Twilio webhook (BUY/SELL/STATUS/HELP/
TOKEN). Telegram has no daily message limit and needs no sandbox join, so this
is the primary command channel. Runs as a blocking loop, typically in a daemon
thread alongside the Flask app in bot_server.py.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import requests

from src.alerts import send_alert

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT_S = 30


def _get_updates(token: str, offset: int) -> list[dict]:
    try:
        resp = requests.get(
            _API.format(token=token, method="getUpdates"),
            params={"offset": offset, "timeout": _POLL_TIMEOUT_S},
            timeout=_POLL_TIMEOUT_S + 10,
        )
        if resp.status_code != 200:
            logger.warning("getUpdates HTTP %s", resp.status_code)
            return []
        return resp.json().get("result", [])
    except Exception:
        logger.exception("getUpdates failed")
        return []


def run_command_bot(
    token: str,
    chat_id: str,
    handlers: dict[str, Callable[[list[str]], str]],
) -> None:
    """
    Blocking long-poll loop. Dispatches commands from the authorized chat only
    and replies via Telegram. Accepts both `STATUS` and `/status` styles.
    """
    if not token:
        logger.warning("Telegram command bot not started — no token")
        return

    logger.warning("Telegram command bot started (chat_id=%s)", chat_id or "any")
    offset = 0
    while True:
        try:
            updates = _get_updates(token, offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue

                from_chat = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                # Only the configured chat may issue commands.
                if chat_id and from_chat != str(chat_id):
                    logger.warning("Ignoring Telegram msg from unauthorized chat %s", from_chat)
                    continue

                parts = text.upper().split()
                parts[0] = parts[0].lstrip("/")   # allow /status or STATUS
                cmd = parts[0]
                handler = handlers.get(cmd)
                reply = (
                    handler(parts) if handler
                    else f"Unknown command: {text}\nSend HELP for commands."
                )
                send_alert(token, from_chat, reply)
                logger.info("Telegram command handled: %s", cmd)

            if not updates:
                time.sleep(1)
        except Exception:
            logger.exception("Telegram command loop error; backing off")
            time.sleep(5)
