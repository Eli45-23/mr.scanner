#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner


def main() -> int:
    scanner.load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not configured.")
        return 1
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            timeout=8,
        )
        response.raise_for_status()
        updates = response.json().get("result") or []
    except Exception as exc:
        print(f"Could not read Telegram updates: {scanner.redact_notification_error(exc)}")
        return 1
    chats = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            chats[str(chat["id"])] = chat
    if not chats:
        print("Open Telegram, message your bot, then run again.")
        return 0
    print("Available Telegram chat IDs:")
    for chat_id, chat in chats.items():
        label = chat.get("title") or chat.get("username") or chat.get("first_name") or "unnamed chat"
        print(f"- {chat_id}: {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
