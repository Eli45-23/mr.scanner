import unittest

from scanner.options_flow_classifier import classify_aggression, estimate_opening_flow


class OptionsFlowClassifierTests(unittest.TestCase):
    def test_call_near_ask_is_possible_bullish(self):
        result = classify_aggression({"option_type": "CALL", "bid": 1.0, "ask": 1.1, "last": 1.1, "midpoint": 1.05})
        self.assertEqual(result["aggression_side"], "near_ask")
        self.assertEqual(result["aggression_confidence"], "MEDIUM")
        self.assertIn("ask", result["bid_ask_reason"])
        self.assertEqual(result["direction_label"], "Possible bullish call flow")

    def test_bid_side_midpoint_missing_and_stale_quotes(self):
        bid = classify_aggression({"option_type": "CALL", "bid": 1.0, "ask": 1.1, "last": 1.0, "midpoint": 1.05})
        self.assertEqual(bid["aggression_side"], "near_bid")
        midpoint = classify_aggression({"option_type": "CALL", "bid": 1.0, "ask": 1.1, "last": 1.05, "midpoint": 1.05})
        self.assertEqual(midpoint["aggression_side"], "midpoint")
        missing = classify_aggression({"option_type": "CALL"})
        self.assertEqual(missing["aggression_side"], "unknown")
        stale = classify_aggression({"option_type": "CALL", "bid": 1.0, "ask": 1.1, "last": 1.1, "midpoint": 1.05, "quote_freshness_seconds": 300})
        self.assertEqual(stale["aggression_confidence"], "LOW")
        self.assertIn("stale", stale["quote_stale_warning"].lower())

    def test_put_near_ask_is_possible_bearish(self):
        result = classify_aggression({"option_type": "PUT", "bid": 2.0, "ask": 2.1, "last": 2.1, "midpoint": 2.05})
        self.assertEqual(result["direction_label"], "Possible bearish put flow")

    def test_opening_estimate_handles_zero_oi(self):
        result = estimate_opening_flow({"volume": 1000, "open_interest": 0})
        self.assertEqual(result["opening_flow_estimate"], "possible opening flow")
        self.assertEqual(result["open_close_estimate"], "likely_opening")
        self.assertTrue(result["awaiting_next_day_oi_confirmation"])

    def test_open_close_estimates_closing_mixed_and_unknown(self):
        closing = estimate_opening_flow({"volume": 100, "open_interest": 1000})
        self.assertEqual(closing["open_close_estimate"], "likely_closing")
        mixed = estimate_opening_flow({"volume": 100, "open_interest": 100})
        self.assertEqual(mixed["open_close_estimate"], "mixed")
        unknown = estimate_opening_flow({"volume": 0, "open_interest": 0})
        self.assertEqual(unknown["open_close_estimate"], "unknown")


if __name__ == "__main__":
    unittest.main()
