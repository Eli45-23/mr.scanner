import unittest
from pathlib import Path

import scanner_dashboard


class OptionsWhaleDashboardTests(unittest.TestCase):
    def test_routes_exist_and_scan_has_no_symbol_parameter(self):
        text = Path(scanner_dashboard.__file__).read_text(encoding="utf-8")
        self.assertIn("/api/options-whales/status", text)
        self.assertIn("/api/options-whales/scan", text)
        self.assertNotIn("symbols=AAPL", text)
        self.assertIn("Options Whale Scanner", text)
        self.assertIn("OPTIONS_WHALE_FLOW", text)


if __name__ == "__main__":
    unittest.main()
