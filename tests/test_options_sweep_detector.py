import unittest

from scanner.options_sweep_detector import approximate_sweep_from_snapshot, detect_sweep_activity


class OptionsSweepDetectorTests(unittest.TestCase):
    def test_detects_sweep_cluster(self):
        trades = [
            {"timestamp": "2026-06-17T14:00:00Z", "size": 200, "price": 2.0, "aggression_side": "near_ask"},
            {"timestamp": "2026-06-17T14:00:02Z", "size": 180, "price": 2.0, "aggression_side": "near_ask"},
            {"timestamp": "2026-06-17T14:00:04Z", "size": 170, "price": 2.0, "aggression_side": "near_ask"},
        ]
        result = detect_sweep_activity(trades)
        self.assertTrue(result["is_possible_sweep"])

    def test_limited_without_prints(self):
        result = approximate_sweep_from_snapshot({"trade_count": 0, "estimated_premium": 0})
        self.assertFalse(result["is_possible_sweep"])
        self.assertIn("limited", result["sweep_reason"])


if __name__ == "__main__":
    unittest.main()
