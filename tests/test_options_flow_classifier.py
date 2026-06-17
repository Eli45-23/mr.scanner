import unittest

from scanner.options_flow_classifier import classify_aggression, estimate_opening_flow


class OptionsFlowClassifierTests(unittest.TestCase):
    def test_call_near_ask_is_possible_bullish(self):
        result = classify_aggression({"option_type": "CALL", "bid": 1.0, "ask": 1.1, "last": 1.1, "midpoint": 1.05})
        self.assertEqual(result["aggression_side"], "near_ask")
        self.assertEqual(result["direction_label"], "Possible bullish call flow")

    def test_put_near_ask_is_possible_bearish(self):
        result = classify_aggression({"option_type": "PUT", "bid": 2.0, "ask": 2.1, "last": 2.1, "midpoint": 2.05})
        self.assertEqual(result["direction_label"], "Possible bearish put flow")

    def test_opening_estimate_handles_zero_oi(self):
        result = estimate_opening_flow({"volume": 1000, "open_interest": 0})
        self.assertEqual(result["opening_flow_estimate"], "possible opening flow")


if __name__ == "__main__":
    unittest.main()
