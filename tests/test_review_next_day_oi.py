import unittest
from datetime import date

from tools.review_next_day_oi import prior_session_episodes


class ReviewNextDayOiTests(unittest.TestCase):
    def test_prior_session_selects_all_rows_from_latest_prior_date(self):
        rows = [
            {"episode_id": "old", "scanner_detected_time": "2026-06-30T15:00:00Z"},
            {"episode_id": "july-1-a", "scanner_detected_time": "2026-07-01T14:00:00Z"},
            {"episode_id": "july-1-b", "scanner_detected_time": "2026-07-01T19:30:00Z"},
            {"episode_id": "today", "scanner_detected_time": "2026-07-02T14:00:00Z"},
        ]
        source_day, selected = prior_session_episodes(rows, as_of=date(2026, 7, 2))
        self.assertEqual(source_day, "2026-07-01")
        self.assertEqual({row["episode_id"] for row in selected}, {"july-1-a", "july-1-b"})

    def test_prior_session_returns_empty_when_no_prior_rows_exist(self):
        source_day, selected = prior_session_episodes([], as_of=date(2026, 7, 2))
        self.assertIsNone(source_day)
        self.assertEqual(selected, [])


if __name__ == "__main__":
    unittest.main()
