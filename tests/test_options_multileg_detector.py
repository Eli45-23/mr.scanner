import unittest

from scanner.options_multileg_detector import default_multileg_result, detect_possible_multileg


class OptionsMultilegDetectorTests(unittest.TestCase):
    def test_detects_possible_call_spread(self):
        rows = [
            {"underlying_symbol": "AAPL", "expiration": "2026-06-19", "option_symbol": "AAPL1", "option_type": "CALL", "strike": 200, "volume": 1000},
            {"underlying_symbol": "AAPL", "expiration": "2026-06-19", "option_symbol": "AAPL2", "option_type": "CALL", "strike": 205, "volume": 950},
        ]
        result = detect_possible_multileg(rows)
        self.assertTrue(result["AAPL1"]["possible_multileg"])
        self.assertEqual(result["AAPL1"]["direction_clarity"], "mixed")

    def test_default_is_clear(self):
        self.assertFalse(default_multileg_result()["possible_multileg"])


if __name__ == "__main__":
    unittest.main()
