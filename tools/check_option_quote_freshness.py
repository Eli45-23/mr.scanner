#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import elite_momentum_scanner as scanner


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose Alpaca OPRA option quote freshness without placing orders.")
    parser.add_argument("--symbol", default="AAPL")
    args = parser.parse_args()
    symbol = args.symbol.upper()
    scanner.load_dotenv()
    config = scanner.load_config(None)
    provider = scanner.make_provider("live", [symbol], config)
    latest = provider.get_latest_bars([symbol]).get(symbol)
    if latest is None:
        print(f"No latest SIP bar available for {symbol}.")
        return 1
    chain = provider.get_option_chain(symbol, config)
    call, put = scanner.select_option_contracts(chain, latest.c, config)
    market = scanner.latest_market_data_status(config)
    records = []
    identity = scanner.scanner_identity(config)
    print(f"Scanner instance: {identity['scanner_instance_name']}")
    print(f"Hostname: {identity['hostname']}")
    print(f"Git commit: {identity['git_commit']}")
    print(f"Stock feed requested: {scanner.stock_feed_from_config(config).upper()}")
    print(f"Options feed requested: {scanner.options_feed_from_config(config).upper()}")
    print(f"OPRA status: {market.get('opra_status', 'unknown')}")
    print(f"Underlying: {symbol} ${latest.c:.2f}")
    if isinstance(provider, scanner.AlpacaProvider):
        stock_response = provider.session.get(
            f"{provider.base_v2}/stocks/{symbol}/snapshot",
            params={"feed": scanner.stock_feed_from_config(config)},
            timeout=15,
        )
        if stock_response.status_code < 400:
            stock_snapshot = stock_response.json()
            print(f"Latest stock quote: {stock_snapshot.get('latestQuote') or stock_snapshot.get('latest_quote') or 'unavailable'}")
            print(f"Latest stock trade: {stock_snapshot.get('latestTrade') or stock_snapshot.get('latest_trade') or 'unavailable'}")
        else:
            print(f"Latest stock quote/trade check unavailable: HTTP {stock_response.status_code}")
    for label, selection in (("CALL", call), ("PUT", put)):
        contract = selection.contract
        if contract is None:
            record = {
                "timestamp": scanner.now_utc().isoformat(),
                **identity,
                "symbol": symbol,
                "underlying_symbol": symbol,
                "option_type": label,
                "status": "invalid",
                "invalid_reason": "no_contract",
                "stale_reason": "",
            }
        else:
            details = scanner.option_freshness_details(contract, config)
            record = {
                "timestamp": scanner.now_utc().isoformat(),
                **identity,
                "symbol": symbol,
                "underlying_symbol": symbol,
                "selected_option_symbol": contract.symbol,
                "option_type": label,
                "underlying_price": latest.c,
                "strike": contract.strike,
                "expiration": contract.expiration_date.isoformat(),
                "bid": contract.bid,
                "ask": contract.ask,
                "mid": contract.mid,
                "spread_pct": contract.spread_pct,
                "data_source": contract.feed,
                "option_quality_label": selection.quality,
                "option_quality_score": selection.score,
                "quote_object_type": contract.quote_raw_type,
                "quote_repr": repr(contract.quote_raw_data)[:2000],
                "quote_dict": contract.quote_raw_data,
                "quote___dict__": contract.quote_raw_data if contract.quote_raw_type != "dict" else None,
                "quote_model_dump": contract.quote_raw_data if contract.quote_raw_type != "dict" else None,
                "quote_raw_data": contract.quote_raw_data,
                "quote_top_level_keys": sorted(contract.quote_raw_data.keys()),
                "opra_feed_requested": scanner.options_feed_from_config(config).upper(),
                "opra_status": market.get("opra_status", "unknown"),
                "fallback_used": contract.feed == "indicative",
                **details,
            }
        records.append(record)
        print(f"\n{label}: {record.get('selected_option_symbol', 'unavailable')}")
        for key in ("bid", "ask", "mid", "spread_pct", "quote_object_type", "quote_top_level_keys", "timestamp_source_field", "timestamp_available_fields", "timestamp_extraction_failed", "quote_timestamp_raw", "quote_timestamp_utc", "scanner_timestamp_utc", "quote_age_seconds", "max_allowed_quote_age_seconds", "fallback_used", "fallback_type", "fallback_timestamp_utc", "market_session_status", "status", "stale_reason", "invalid_reason", "option_quality_label"):
            print(f"  {key}: {record.get(key)}")
        print(f"  quote_repr: {record.get('quote_repr')}")
        print(f"  quote_dict: {record.get('quote_dict')}")
        print(f"  quote_raw_data: {record.get('quote_raw_data')}")
        final_decision = {
            "recent": "TRADABLE",
            "stale": "STALE",
            "invalid": "INVALID",
            "poor_quality": "POOR_QUALITY",
        }.get(str(record.get("status", "")).lower(), "UNKNOWN")
        print(f"  final_decision: {final_decision}")
    path = ROOT / "logs" / "option_freshness_diagnostic.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    print(f"\nDiagnostic log: {path}")
    print("Read-only diagnostic. No orders were placed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
