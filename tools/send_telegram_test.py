#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner


def main() -> int:
    scanner.load_dotenv()
    config = scanner.load_config(None)
    if scanner.send_telegram_test_message(config):
        print("Telegram test alert sent.")
        return 0
    print("Telegram test alert was not sent. Check TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and logs/notification_status.jsonl.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
