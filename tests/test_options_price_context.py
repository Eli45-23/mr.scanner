import unittest

from scanner.options_price_context import classify_price_context


class OptionsPriceContextTests(unittest.TestCase):
    def test_call_flow_aligns_with_breakout_context(self):
        bars = [
            {"o": 100, "h": 101, "l": 99, "c": 100, "v": 1000},
            {"o": 101, "h": 103, "l": 100, "c": 102.9, "v": 2000},
        ]
        result = classify_price_context("AAPL", "CALL", 103, bars)
        self.assertGreaterEqual(result["price_context_score"], 6)
        self.assertIn("flow aligns", " ".join(result["price_context"]["labels"]))

    def test_missing_price_is_safe(self):
        result = classify_price_context("AAPL", "CALL", None, [])
        self.assertEqual(result["price_context_score"], 0)


if __name__ == "__main__":
    unittest.main()
