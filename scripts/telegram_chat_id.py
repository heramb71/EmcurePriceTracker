#!/usr/bin/env python3
"""Print the Telegram chat ID for a bot token — with diagnostics.

Why this exists: the command bot long-polls getUpdates, and Telegram delivers
each update only once. If a poller (emcure-bot / bot_server.py) is running it
eats the update before a manual `curl getUpdates` can see it. This script
checks for that (and for a stray webhook) and tells you what's wrong.

Note: your PERSONAL chat ID equals your Telegram user ID — it is the SAME
number for every bot when the chat is your own DM. So read it from any one bot
(pick one nothing is polling, e.g. the radar/crypto bot) and reuse it for all
three *_CHAT_ID vars.

Usage:
    python scripts/telegram_chat_id.py                 # try every token in .env
    python scripts/telegram_chat_id.py <BOT_TOKEN>     # a specific token

Before running: open the bot in Telegram and send it any message (e.g. "hi").
"""
from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

_TOKEN_ENVS = [
    ("shared", "TELEGRAM_TOKEN"),
    ("emcure", "TELEGRAM_EMCURE_TOKEN"),
    ("radar", "TELEGRAM_RADAR_TOKEN"),
    ("crypto", "TELEGRAM_CRYPTO_TOKEN"),
]


def _api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _bot_identity(token: str) -> str:
    """Return the bot's @username via getMe, so a mislabeled token is obvious."""
    try:
        me = requests.get(_api(token, "getMe"), timeout=10).json()
        res = me.get("result") or {}
        if res.get("username"):
            return f"@{res['username']}"
        return "?? (getMe returned no username — token may be invalid)"
    except requests.RequestException:
        return "?? (getMe unreachable)"


def inspect(label: str, token: str) -> None:
    print(f"\n=== {label}  (token …{token[-6:]})  →  {_bot_identity(token)} ===")

    # A registered webhook silently disables getUpdates.
    try:
        wh = requests.get(_api(token, "getWebhookInfo"), timeout=10).json()
        url = (wh.get("result") or {}).get("url", "")
        if url:
            print(f"⚠️  A webhook is set ({url}). getUpdates won't return data.")
            print("   Clear it:  curl -s "
                  f'"https://api.telegram.org/bot{token}/deleteWebhook"')
            return
    except requests.RequestException as exc:
        print(f"❌ Cannot reach api.telegram.org ({exc.__class__.__name__}).")
        print("   Telegram is periodically govt-blocked in India — try a VPN/hotspot.")
        return

    try:
        resp = requests.get(_api(token, "getUpdates"), params={"timeout": 0}, timeout=15)
    except requests.RequestException as exc:
        print(f"❌ getUpdates failed ({exc.__class__.__name__}).")
        return

    if resp.status_code == 409:
        print("⚠️  409 Conflict — another process is polling this bot "
              "(the emcure-bot service or a local bot_server.py).")
        print("   Stop it first:  sudo systemctl stop emcure-bot   "
              "(or Ctrl-C the local bot), then re-run.")
        print("   Or just read the chat ID from a bot nothing polls "
              "(radar/crypto) — it's the same number.")
        return

    data = resp.json()
    updates = data.get("result", [])
    if not updates:
        print("ℹ️  No updates. Either you haven't messaged this bot yet, or a "
              "running poller already consumed them.")
        print("   → Send the bot a message, make sure no poller is running, re-run.")
        return

    chats: dict[int, str] = {}
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            who = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            chats[chat["id"]] = f"{chat.get('type', '?')}  {who}".strip()

    if not chats:
        print("ℹ️  Updates present but none carried a chat — send a plain text message.")
        return

    for cid, desc in chats.items():
        print(f"✅ CHAT_ID = {cid}    ({desc})")


def main() -> int:
    if len(sys.argv) > 1:
        inspect("cli-token", sys.argv[1])
        return 0

    found = [(lbl, os.getenv(env, "").strip()) for lbl, env in _TOKEN_ENVS]
    found = [(lbl, tok) for lbl, tok in found if tok]
    if not found:
        print("No TELEGRAM_*_TOKEN set in .env. Pass a token as an argument, or "
              "fill one in first.")
        return 1
    for lbl, tok in found:
        inspect(lbl, tok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
