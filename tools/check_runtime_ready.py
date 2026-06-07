#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner


def yes_no(value: Any) -> str:
    return "yes" if value else "no"


def port_status(host: str = "127.0.0.1", port: int = 8765) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return "dashboard already running" if sock.connect_ex((host, port)) == 0 else "available"


def main() -> int:
    scanner.load_dotenv()
    config = scanner.load_config(None)
    failures: list[str] = []
    warnings: list[str] = []

    print("AAPL Scanner Runtime Readiness")
    print("------------------------------")
    print(f"Project root: {ROOT}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Virtual environment: {yes_no((ROOT / '.venv' / 'bin' / 'python').exists())}")

    for package in ("requests", "elite_momentum_scanner", "scanner_dashboard", "tools.dashboard_snapshot_exporter"):
        try:
            importlib.import_module(package)
            print(f"Import {package}: ok")
        except Exception as exc:
            print(f"Import {package}: failed ({type(exc).__name__})")
            failures.append(f"required import failed: {package}")

    env_path = ROOT / ".env"
    print(f"Private .env present: {yes_no(env_path.exists())}")
    if not env_path.exists():
        failures.append(".env is missing")

    alpaca_key = bool(os.getenv("ALPACA_API_KEY", "").strip())
    alpaca_secret = bool(os.getenv("ALPACA_SECRET_KEY", "").strip())
    telegram_token = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    telegram_chat = bool(os.getenv("TELEGRAM_CHAT_ID", "").strip())
    print(f"Alpaca API key configured: {yes_no(alpaca_key)} (redacted)")
    print(f"Alpaca secret configured: {yes_no(alpaca_secret)} (redacted)")
    print(f"Telegram token configured: {yes_no(telegram_token)} (redacted)")
    print(f"Telegram chat ID configured: {yes_no(telegram_chat)} (redacted)")
    if not (alpaca_key and alpaca_secret):
        failures.append("Alpaca credentials are incomplete")
    if config.get("notifications", {}).get("telegram_enabled") and not (telegram_token and telegram_chat):
        warnings.append("Telegram is enabled but not fully configured")

    stock_feed = str(config.get("market_data", {}).get("stock_feed", "unknown")).upper()
    options_feed = str(config.get("options", {}).get("feed", "unknown")).upper()
    print(f"Stock feed requested: {stock_feed}")
    print(f"Options feed requested: {options_feed}")
    if stock_feed != "SIP":
        failures.append("stock feed is not SIP")
    if options_feed != "OPRA":
        failures.append("options feed is not OPRA")

    if alpaca_key and alpaca_secret:
        try:
            provider = scanner.make_provider("live", ["AAPL"], config)
            status = provider.check_market_data_status(config, symbol="AAPL")
            print(f"SIP runtime status: {status.get('stock_feed_status', 'unknown')}")
            print(f"OPRA runtime status: {status.get('opra_status', 'unknown')}")
            warning = status.get("feed_warning")
            if warning:
                warnings.append(str(warning))
        except Exception as exc:
            warnings.append(f"market-data runtime check unavailable ({type(exc).__name__})")
    else:
        print("SIP/OPRA runtime check: skipped until Alpaca credentials are configured")

    print(f"Dashboard port 8765: {port_status()}")
    print(f"screen available: {yes_no(shutil.which('screen'))}")
    if not shutil.which("screen"):
        failures.append("screen is not installed")

    for folder in ("logs", "exports"):
        exists = (ROOT / folder).is_dir()
        print(f"{folder}/ exists: {yes_no(exists)}")
        if not exists:
            failures.append(f"{folder}/ is missing")

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"- {warning}")
    if failures:
        print("\nNot ready:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("\nRuntime ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
