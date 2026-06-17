import unittest

from scanner.options_oi_review import classify_next_day_oi, review_alerts_with_next_day_oi


class OptionsOiReviewTests(unittest.TestCase):
    def test_classifies_likely_opening(self):
        result = classify_next_day_oi({"open_interest": 100, "volume": 500}, 500)
        self.assertTrue(result["likely_opening"])

    def test_reviews_alerts(self):
        reviews = review_alerts_with_next_day_oi([
            {"candidate": {"option_symbol": "AAPLX", "volume": 500, "open_interest": 100}}
        ], {"AAPLX": 500})
        self.assertEqual(len(reviews), 1)


if __name__ == "__main__":
    unittest.main()
