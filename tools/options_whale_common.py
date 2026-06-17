from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner_app
from scanner.options_data_client import OptionsDataClient
from scanner.options_whale_scanner import OptionsWhaleScanner
from scanner.options_whale_storage import OptionsWhaleStorage


def build_scanner(config_path: str | None = None) -> OptionsWhaleScanner:
    scanner_app.load_dotenv()
    config = scanner_app.load_config(Path(config_path).resolve() if config_path else None)
    client = OptionsDataClient(
        stock_feed=str(config.get("market_data", {}).get("stock_feed", "sip")),
        options_feed=str(config.get("options", {}).get("feed", "opra")),
        allow_indicative_fallback=bool(config.get("options", {}).get("allow_indicative_fallback", True)),
    )
    return OptionsWhaleScanner(config, client, OptionsWhaleStorage(ROOT), root=ROOT)


def print_json(payload: Dict[str, Any]) -> None:
    import json

    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
