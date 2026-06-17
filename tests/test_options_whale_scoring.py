import tempfile
import unittest
from pathlib import Path

from scanner.options_unusualness_baseline import OptionsUnusualnessBaseline
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

    def test_unusualness_component_boosts_pro_score(self):
        base = {
            "estimated_premium": 600000,
            "volume_oi_ratio": 3,
            "aggression_score": 10,
            "spread_percent": 5,
            "dte": 0,
            "moneyness": "ATM",
            "price_context_score": 4,
        }
        normal = score_options_whale_flow(base)
        unusual = score_options_whale_flow({**base, "unusualness_score": 18, "unusualness_label": "HIGHLY_UNUSUAL"})
        self.assertGreater(unusual["whale_score"], normal["whale_score"])
        self.assertEqual(unusual["score_components"]["historical_unusualness"], 18)
        self.assertTrue(any("historically unusual" in reason for reason in unusual["detailed_reasons"]))

    def test_deep_itm_premium_gets_warning(self):
        result = score_options_whale_flow({
            "estimated_premium": 900000,
            "volume_oi_ratio": 2,
            "aggression_score": 8,
            "spread_percent": 4,
            "dte": 1,
            "moneyness": "ITM",
            "distance_percent": -18,
            "price_context_score": 3,
        })
        self.assertIn("moneyness_quality", result["score_components"])
        self.assertLessEqual(result["score_components"]["moneyness_quality"], 1)
        self.assertTrue(any("Deep ITM" in warning for warning in result["score_warnings"]))

    def test_far_otm_lotto_gets_warning(self):
        result = score_options_whale_flow({
            "estimated_premium": 150000,
            "volume_oi_ratio": 5,
            "aggression_score": 8,
            "spread_percent": 8,
            "dte": 0,
            "moneyness": "OTM",
            "distance_percent": 14,
            "price_context_score": 2,
        })
        self.assertLessEqual(result["score_components"]["moneyness_quality"], 1)
        self.assertTrue(any("Far OTM" in warning for warning in result["score_warnings"]))

    def test_baseline_flags_highly_unusual_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = OptionsUnusualnessBaseline(Path(temp_dir))
            normal_rows = [
                {
                    "underlying_symbol": "AAPL",
                    "option_symbol": f"AAPLTEST{i}",
                    "option_type": "CALL",
                    "dte": 0,
                    "moneyness": "ATM",
                    "volume": 100 + i,
                    "estimated_premium": 12000 + i * 100,
                }
                for i in range(30)
            ]
            baseline.append_observations(normal_rows)
            result = baseline.evaluate_candidate({
                "underlying_symbol": "AAPL",
                "option_symbol": "AAPLWHALE",
                "option_type": "CALL",
                "dte": 0,
                "moneyness": "ATM",
                "volume": 5000,
                "estimated_premium": 900000,
            })
            self.assertGreaterEqual(result["unusualness_score"], 13)
            self.assertIn(result["unusualness_label"], {"HIGHLY_UNUSUAL", "EXTREME_UNUSUAL"})
            self.assertGreater(result["volume_z_score"], 1)
            self.assertGreater(result["premium_z_score"], 1)
            self.assertEqual(result["baseline_scope"], "symbol_dte_moneyness")

    def test_baseline_reduces_confidence_with_small_sample(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = OptionsUnusualnessBaseline(Path(temp_dir))
            baseline.append_observations([
                {
                    "underlying_symbol": "NVDA",
                    "option_symbol": "NVDATEST1",
                    "option_type": "PUT",
                    "dte": 1,
                    "moneyness": "OTM",
                    "volume": 10,
                    "estimated_premium": 1000,
                }
            ])
            result = baseline.evaluate_candidate({
                "underlying_symbol": "NVDA",
                "option_symbol": "NVDAWHALE",
                "option_type": "PUT",
                "dte": 1,
                "moneyness": "OTM",
                "volume": 5000,
                "estimated_premium": 750000,
            })
            self.assertLessEqual(result["unusualness_score"], 8)
            self.assertTrue(any("limited historical baseline" in warning for warning in result["unusualness_warnings"]))

    def test_baseline_warns_on_index_0dte_noise(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            baseline = OptionsUnusualnessBaseline(Path(temp_dir))
            normal_spy = [
                {
                    "underlying_symbol": "SPY",
                    "option_symbol": f"SPYTEST{i}",
                    "option_type": "PUT",
                    "dte": 0,
                    "moneyness": "ATM",
                    "volume": 20000 + i,
                    "estimated_premium": 2000000 + i * 100,
                }
                for i in range(30)
            ]
            baseline.append_observations(normal_spy)
            result = baseline.evaluate_candidate({
                "underlying_symbol": "SPY",
                "option_symbol": "SPYNORMAL",
                "option_type": "PUT",
                "dte": 0,
                "moneyness": "ATM",
                "volume": 20010,
                "estimated_premium": 2001000,
            })
            self.assertLess(result["unusualness_score"], 10)
            self.assertTrue(any("index 0DTE flow" in warning for warning in result["unusualness_warnings"]))


if __name__ == "__main__":
    unittest.main()
