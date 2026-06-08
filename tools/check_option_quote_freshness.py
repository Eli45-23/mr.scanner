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
            record = {"timestamp": scanner.now_utc().isoformat(), "symbol": symbol, "option_type": label, "stale_reason": "no_contract"}
        else:
            details = scanner.option_freshness_details(contract, config)
            record = {
                "timestamp": scanner.now_utc().isoformat(),
                **scanner.scanner_identity(config),
                "symbol": symbol,
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
                **details,
            }
        records.append(record)
        print(f"\n{label}: {record.get('selected_option_symbol', 'unavailable')}")
        for key in ("bid", "ask", "mid", "quote_timestamp_raw", "quote_timestamp_utc", "scanner_timestamp_utc", "quote_age_seconds", "max_allowed_quote_age_seconds", "status", "stale_reason", "option_quality_label"):
            print(f"  {key}: {record.get(key)}")
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
