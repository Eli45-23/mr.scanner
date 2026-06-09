#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from elite_momentum_scanner import AlpacaProvider, load_dotenv, now_utc  # noqa: E402


def main() -> int:
    load_dotenv()
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        print("Alpaca news access: unavailable")
        print("Reason: ALPACA_API_KEY or ALPACA_SECRET_KEY is missing. Credentials remain redacted.")
        return 1
    provider = AlpacaProvider(api_key, secret_key, feed=os.getenv("ALPACA_STOCK_FEED", "sip"))
    try:
        response = provider.session.get(
            f"{provider.base_v1beta}/news",
            params={"symbols": "AAPL", "limit": 1, "sort": "desc"},
            timeout=15,
        )
        if response.status_code >= 400:
            print("Alpaca news access: unavailable")
            print(f"Reason: Alpaca returned HTTP {response.status_code}. No credentials were printed.")
            return 1
        items = provider.get_news(["AAPL"], limit=5)
    except Exception as exc:
        print("Alpaca news access: unavailable")
        print(f"Reason: {type(exc).__name__}. No credentials were printed.")
        return 1
    print("Alpaca news access: available")
    print(f"AAPL records returned: {len(items)}")
    print(f"Checked at: {now_utc().isoformat()}")
    if items:
        latest = items[0]
        print(f"Latest source: {latest.source or 'unavailable'}")
        print(f"Latest published: {latest.published_at.isoformat()}")
        print(f"Latest headline: {latest.headline}")
    else:
        print("No recent AAPL news records returned.")
    print("News is context only and cannot create or upgrade scanner alerts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
