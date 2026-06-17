import unittest

from scanner.options_whale_scoring import estimated_premium, score_options_whale_flow, spread_percent, volume_oi_ratio


class OptionsWhaleScoringTests(unittest.TestCase):
    def test_calculations(self):
        self.assertEqual(estimated_premium(1000, 1.5), 150000.0)
        self.assertEqual(spread_percent(1.0, 1.1), 9.52)
        self.assertEqual(volume_oi_ratio(1000, 250), 4.0)

    def test_score_components_explain_result(self):
        result = score_options_whale_flow({
            "estimated_premium": 500000,
            "volume_oi_ratio": 4,
            "aggression_score": 20,
            "is_possible_sweep": True,
            "spread_percent": 4,
            "dte": 0,
            "moneyness": "ATM",
            "price_context_score": 8,
        })
        self.assertGreaterEqual(result["whale_score"], 75)
        self.assertIn("score_components", result)
        self.assertTrue(result["detailed_reasons"])


if __name__ == "__main__":
    unittest.main()
