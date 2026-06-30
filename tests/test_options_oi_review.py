import unittest
from datetime import date

from scanner.options_oi_review import classify_next_day_oi, fetch_next_day_oi_map, review_alerts_with_next_day_oi


class FakeContractClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_option_contracts(self, *, expiration_gte: date, expiration_lte: date, underlying_symbols=None, limit=10000, max_contracts=10000):
        self.calls.append({
            "expiration_gte": expiration_gte,
            "expiration_lte": expiration_lte,
            "underlying_symbols": underlying_symbols,
        })
        symbols = set(underlying_symbols or [])
        return [row for row in self.rows if not symbols or row.get("underlying_symbol") in symbols or row.get("root_symbol") in symbols]


class OptionsOiReviewTests(unittest.TestCase):
    def alert(self, volume=1000, oi=100, symbol="QQQ260618P00726000"):
        return {
            "timestamp": "2026-06-17T15:00:00+00:00",
            "candidate": {
                "underlying_symbol": "QQQ",
                "option_symbol": symbol,
                "option_type": "PUT",
                "expiration": "2026-06-18",
                "strike": 726,
                "volume": volume,
                "open_interest": oi,
            },
        }

    def test_confirms_opening_when_next_day_oi_rises_enough(self):
        result = classify_next_day_oi(self.alert(volume=1000, oi=100), 700)
        self.assertEqual(result["next_day_oi_status"], "confirmed_opening")
        self.assertEqual(result["open_close_estimate_after_oi"], "likely_opening")
        self.assertEqual(result["next_day_oi_change"], 600)
        self.assertTrue(result["likely_opening"])

    def test_not_confirmed_when_oi_barely_changes(self):
        result = classify_next_day_oi(self.alert(volume=1000, oi=100), 180)
        self.assertEqual(result["next_day_oi_status"], "not_confirmed")
        self.assertEqual(result["open_close_estimate_after_oi"], "mixed")
        self.assertFalse(result["likely_opening"])

    def test_pending_when_no_next_day_oi_available(self):
        result = classify_next_day_oi(self.alert(), None)
        self.assertEqual(result["next_day_oi_status"], "pending")
        self.assertIn("awaiting", result["next_day_oi_reason"])

    def test_large_oi_decrease_is_likely_closing(self):
        result = classify_next_day_oi(self.alert(volume=1000, oi=1000), 100)
        self.assertEqual(result["next_day_oi_status"], "likely_closing")

    def test_episode_with_offsetting_oi_changes_is_possible_roll_or_spread(self):
        alert = self.alert(symbol="QQQ260618P00726000")
        alert["flow_episode_id"] = "QQQ|BEARISH|bucket"
        alert["episode_member_contracts"] = [
            {"option_symbol": "QQQ260618P00726000", "open_interest": 100},
            {"option_symbol": "QQQ260619P00726000", "open_interest": 500},
        ]
        rows = review_alerts_with_next_day_oi([alert], {"QQQ260618P00726000": 700, "QQQ260619P00726000": 100})
        self.assertEqual(rows[0]["next_day_oi_status"], "roll_or_spread_possible")

    def test_review_alerts_outputs_contract_review(self):
        rows = review_alerts_with_next_day_oi([self.alert(volume=1000, oi=100)], {"QQQ260618P00726000": 700})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["option_symbol"], "QQQ260618P00726000")
        self.assertEqual(rows[0]["next_day_oi_status"], "confirmed_opening")
        self.assertEqual(rows[0]["same_day_volume"], 1000)

    def test_fetch_next_day_oi_map_fetches_matching_contracts(self):
        client = FakeContractClient([
            {"symbol": "QQQ260618P00726000", "underlying_symbol": "QQQ", "open_interest": 700},
            {"symbol": "QQQ260618P00727000", "underlying_symbol": "QQQ", "open_interest": 10},
        ])
        oi_map = fetch_next_day_oi_map(client, [self.alert()])
        self.assertEqual(oi_map["QQQ260618P00726000"], 700)
        self.assertEqual(client.calls[0]["underlying_symbols"], ["QQQ"])


if __name__ == "__main__":
    unittest.main()
