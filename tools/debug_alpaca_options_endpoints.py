#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner_app
from scanner.options_data_client import OptionsDataClient, endpoint_hint


def short_body(response: Any, limit: int = 500) -> str:
    return str(getattr(response, "text", "") or "")[:limit]


def test_get(client: OptionsDataClient, label: str, base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        response = client._request("GET", base, path, params=params or {}, timeout=20)
        result = {
            "label": label,
            "base_url": base,
            "path": path,
            "status_code": response.status_code,
            "ok": response.status_code < 400,
            "response_body": short_body(response),
            "entitlement_hint": endpoint_hint(response.status_code, label),
        }
        if path == "/v2/options/contracts" and response.status_code < 400:
            try:
                contracts = response.json().get("option_contracts") or []
                if contracts:
                    result["sample_option_symbol"] = str(contracts[0].get("symbol") or "")
            except Exception:
                pass
        return result
    except Exception as exc:
        return {
            "label": label,
            "base_url": base,
            "path": path,
            "status_code": None,
            "ok": False,
            "response_body": str(exc)[:500],
            "entitlement_hint": "Request failed before an HTTP response was returned.",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Debug read-only Alpaca options endpoint access.")
    parser.add_argument("--symbol", default="AAPL", help="Underlying symbol used for contract lookup.")
    parser.add_argument("--option-symbol", default="", help="Optional OCC option symbol used for data endpoint tests.")
    args = parser.parse_args()

    scanner_app.load_dotenv()
    config = scanner_app.load_config(None)
    client = OptionsDataClient(
        stock_feed=str(config.get("market_data", {}).get("stock_feed", "sip")),
        options_feed=str(config.get("options", {}).get("feed", "opra")),
        allow_indicative_fallback=bool(config.get("options", {}).get("allow_indicative_fallback", True)),
    )

    today = datetime.now(timezone.utc).date()
    start = datetime.now(timezone.utc) - timedelta(minutes=15)
    rows: List[Dict[str, Any]] = []
    rows.append(test_get(client, "account_auth_check", client.options_contracts_base_url, "/v2/account"))
    rows.append(test_get(
        client,
        "contracts_paper_base",
        client.paper_base_url,
        "/v2/options/contracts",
        {
            "status": "active",
            "underlying_symbols": args.symbol.upper(),
            "expiration_date_gte": today.isoformat(),
            "expiration_date_lte": (today + timedelta(days=7)).isoformat(),
            "limit": 1,
        },
    ))
    rows.append(test_get(
        client,
        "contracts_live_base",
        client.live_base_url,
        "/v2/options/contracts",
        {
            "status": "active",
            "underlying_symbols": args.symbol.upper(),
            "expiration_date_gte": today.isoformat(),
            "expiration_date_lte": (today + timedelta(days=7)).isoformat(),
            "limit": 1,
        },
    ))

    option_symbol = args.option_symbol.strip()
    if not option_symbol:
        for row in rows:
            if row["label"] in {"contracts_paper_base", "contracts_live_base"} and row["ok"]:
                option_symbol = str(row.get("sample_option_symbol") or "")
                if option_symbol:
                    break

    if option_symbol:
        common = {"symbols": option_symbol, "feed": client.options_feed}
        rows.append(test_get(client, "option_snapshots_data_base", client.options_data_base_url, "/v1beta1/options/snapshots", common))
        rows.append(test_get(client, "option_quotes_latest_data_base", client.options_data_base_url, "/v1beta1/options/quotes/latest", common))
        rows.append(test_get(
            client,
            "option_trades_data_base",
            client.options_data_base_url,
            "/v1beta1/options/trades",
            {"symbols": option_symbol, "start": start.isoformat(), "limit": 1},
        ))
        rows.append(test_get(
            client,
            "option_bars_data_base",
            client.options_data_base_url,
            "/v1beta1/options/bars",
            {"symbols": option_symbol, "timeframe": "1Min", "start": start.isoformat(), "limit": 1},
        ))
    else:
        rows.append({
            "label": "option_data_endpoints",
            "base_url": client.options_data_base_url,
            "path": "/v1beta1/options/*",
            "status_code": None,
            "ok": False,
            "response_body": "Skipped because no sample option symbol was available from contracts endpoints.",
            "entitlement_hint": "Fix contract lookup first or pass --option-symbol.",
        })

    print(json.dumps({
        "paper_or_live_mode": client.paper_or_live_mode,
        "contracts_url_used": client.endpoint_url(client.options_contracts_base_url, "/v2/options/contracts"),
        "data_url_used": client.options_data_base_url,
        "keys_configured": bool(client.api_key and client.secret_key),
        "results": rows,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
