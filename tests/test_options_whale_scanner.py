import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scanner.options_whale_scanner import OptionsWhaleScanner, format_whale_alert
from scanner.options_whale_storage import OptionsWhaleStorage


class FakeWhaleClient:
    def check_access(self):
        return {
            "alpaca_connected": True,
            "options_contracts_available": True,
            "options_snapshots_available": True,
            "options_quotes_available": True,
            "options_trades_available": True,
            "options_bars_available": True,
            "official_options_feed_available": True,
            "last_error": "",
            "data_plan_warning": "",
        }

    def get_assets(self):
        return [{"symbol": "AAPL", "name": "Apple", "status": "active", "tradable": True}]

    def get_option_contracts(self, **kwargs):
        exp = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        return [{"symbol": "AAPL260619C00200000", "underlying_symbol": "AAPL", "expiration_date": exp, "strike_price": 200, "type": "call"}]

    def get_option_snapshots(self, symbols):
        return {
            symbols[0]: {
                "latestQuote": {"bp": 1.9, "ap": 2.0, "t": datetime.now(timezone.utc).isoformat()},
                "latestTrade": {"p": 2.0, "t": datetime.now(timezone.utc).isoformat()},
                "volume": 1000,
                "open_interest": 100,
                "trade_count": 3,
                "greeks": {"delta": 0.5},
            }
        }

    def get_stock_bars(self, symbols, **kwargs):
        return {"AAPL": [{"o": 198, "h": 201, "l": 197, "c": 200}, {"o": 200, "h": 202, "l": 199, "c": 201}]}

    def get_option_trades(self, symbols, **kwargs):
        now = datetime.now(timezone.utc)
        return {symbols[0]: [
            {"timestamp": now.isoformat(), "size": 200, "price": 2.0, "aggression_side": "near_ask"},
            {"timestamp": (now + timedelta(seconds=2)).isoformat(), "size": 200, "price": 2.0, "aggression_side": "near_ask"},
            {"timestamp": (now + timedelta(seconds=4)).isoformat(), "size": 200, "price": 2.0, "aggression_side": "near_ask"},
        ]}


class OptionsWhaleScannerTests(unittest.TestCase):
    def test_full_market_scan_requires_no_watchlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            scanner = OptionsWhaleScanner({
                "options_whale_scanner": {"enabled": True, "max_contracts_per_scan": 10, "min_score": 60, "min_premium": 100000, "min_volume": 500, "min_volume_oi_ratio": 2.0},
                "market_data": {"stock_feed": "sip"},
                "options": {"feed": "opra"},
            }, FakeWhaleClient(), OptionsWhaleStorage(Path(tmp)), root=Path(tmp))
            result = scanner.scan()
            self.assertGreaterEqual(result["results_count"], 1)
            self.assertIn("Possible whale flow", result["results"][0]["message_preview"])

    def test_alert_message_has_required_disclaimer(self):
        message = format_whale_alert({
            "candidate": {"underlying_symbol": "AAPL", "option_symbol": "AAPLX", "option_type": "CALL", "strike": 200, "expiration": "2026-06-19", "estimated_premium": 100000, "volume_oi_ratio": 3},
            "classification": "HIGH WHALE FLOW",
            "direction_label": "Possible bullish call flow",
            "whale_score": 85,
            "alert_tier": "Tier 2",
            "reason_summary": "Large premium.",
            "price_confirmation_label": "needs price confirmation",
        })
        self.assertIn("Possible whale flow — not a trade signal.", message)
        self.assertNotRegex(message.replace("buy/sell", ""), r"\bbuy\b|\bsell\b|\benter\b")


if __name__ == "__main__":
    unittest.main()
