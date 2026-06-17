import tempfile
import unittest
from datetime import date
from pathlib import Path

from scanner.options_universe import build_optionable_universe, load_universe_cache, universe_status


class FakeUniverseClient:
    def get_assets(self):
        return [{"symbol": "AAPL", "name": "Apple", "status": "active", "tradable": True}]

    def get_option_contracts(self, **kwargs):
        return [
            {"symbol": "AAPL260619C00200000", "underlying_symbol": "AAPL", "expiration_date": "2026-06-19"},
            {"symbol": "AAPL260619P00200000", "underlying_symbol": "AAPL", "expiration_date": "2026-06-19"},
        ]


class OptionsUniverseTests(unittest.TestCase):
    def test_builds_and_caches_universe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "options_universe.json"
            payload = build_optionable_universe(FakeUniverseClient(), {"options_whale_scanner": {"max_dte": 7}}, today=date(2026, 6, 17), cache_path=path)
            self.assertEqual(payload["entry_count"], 1)
            self.assertTrue(path.exists())
            self.assertEqual(universe_status(path)["entry_count"], 1)

    def test_missing_cache_safe(self):
        self.assertEqual(load_universe_cache(Path("/tmp/does-not-exist-options-universe.json"))["status"], "missing")


if __name__ == "__main__":
    unittest.main()
