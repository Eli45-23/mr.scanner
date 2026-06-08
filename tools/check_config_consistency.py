#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner


def running(pattern: str) -> bool:
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    return result.returncode == 0


def build_report() -> dict:
    scanner.load_dotenv()
    config = scanner.load_config(None)
    identity = scanner.scanner_identity(config)
    market = {}
    path = ROOT / "logs" / "market_data_status.jsonl"
    if path.exists():
        try:
            market = json.loads(path.read_text(encoding="utf-8", errors="replace").splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            market = {}
    return {
        **identity,
        "telegram_enabled": bool(config.get("notifications", {}).get("telegram_enabled")),
        "stock_feed": str(config.get("market_data", {}).get("stock_feed", "unknown")).upper(),
        "options_feed": str(config.get("options", {}).get("feed", "unknown")).upper(),
        "opra_status": market.get("opra_status", "unknown"),
        "scanner_running": running("elite_momentum_scanner.py --mode live"),
        "dashboard_running": running("scanner_dashboard.py"),
    }


def main() -> int:
    report = build_report()
    print("Scanner Config Consistency")
    print("--------------------------")
    for key, value in report.items():
        if isinstance(value, list):
            value = ",".join(str(item) for item in value)
        print(f"{key}: {value}")
    print("\nNo secrets or full Telegram chat ID displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
