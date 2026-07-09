import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import review_options_alert_outcomes as review_tool


class FakeBarsClient:
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol
        self.requests = []

    def get_stock_bars(self, symbols, *, start, end, timeframe="1Min"):
        self.requests.append({"symbols": symbols, "start": start, "end": end, "timeframe": timeframe})
        return {symbol: self.bars_by_symbol.get(symbol, []) for symbol in symbols}

    def data_health(self):
        return {"request_diagnostics": {}}


class ReviewOptionsAlertOutcomesTests(unittest.TestCase):
    def latest_payload(self):
        return {
            "timestamp": "2026-06-18T16:33:00Z",
            "results": [
                {
                    "timestamp": "2026-06-18T16:33:00Z",
                    "whale_score": 90,
                    "classification": "EXTREME WHALE FLOW",
                    "alert_tier": "Tier 2",
                    "flow_bias": "BULLISH",
                    "reliability_bucket": "90+|0DTE|MEDIUM|TRENDING_UP|BULLISH",
                    "reliability_status": "penalty_only_insufficient_history",
                    "reliability_meaningful_rate": 0.44,
                    "reliability_executable_positive_rate": 0.50,
                    "reliability_effective_samples": 8.0,
                    "reliability_session_count": 1,
                    "reliability_qualified": False,
                    "reliability_dual_metric_passed": False,
                    "reliability_block_reasons": ["insufficient sessions or paired effective samples"],
                    "cohort_tier1_gate_reasons": ["Tier 1 reliability is not qualified"],
                    "candidate": {
                        "underlying_symbol": "ADBE",
                        "option_symbol": "ADBETESTC",
                        "option_type": "CALL",
                        "strike": 200,
                        "expiration": "2026-06-18",
                        "underlying_price": 100.0,
                        "time_detected": "2026-06-18T16:33:00Z",
                    },
                }
            ],
        }

    def bars(self, detected, minutes):
        return [
            {
                "t": (detected + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "c": 100.0 + minute / 10,
            }
            for minute in minutes
        ]

    def run_review(self, tmp_path, fake_client, *, force=False):
        latest_path = tmp_path / "options_whale_latest.json"
        outcomes_path = tmp_path / "options_whale_outcomes.jsonl"
        latest_path.write_text(json.dumps(self.latest_payload()), encoding="utf-8")
        with mock.patch.object(review_tool, "LATEST_PATH", latest_path), \
            mock.patch.object(review_tool, "OUTCOMES_PATH", outcomes_path), \
            mock.patch.object(review_tool.scanner_app, "load_dotenv", return_value=None), \
            mock.patch.object(review_tool.scanner_app, "load_config", return_value={}), \
            mock.patch.object(review_tool, "build_client", return_value=fake_client):
            return review_tool.review_alerts(limit=100, force=force)

    def test_review_diagnostics_include_bar_request_and_return_range(self):
        detected = datetime(2026, 6, 18, 16, 33, tzinfo=timezone.utc)
        fake_client = FakeBarsClient({"ADBE": self.bars(detected, [0, 5, 15, 30, 60])})
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.run_review(Path(temp_dir), fake_client)
        row = result["reviewed"][0]
        self.assertEqual(row["bars_returned"], 5)
        self.assertEqual(row["first_bar_time"], "2026-06-18T16:33:00+00:00")
        self.assertEqual(row["last_bar_time"], "2026-06-18T17:33:00+00:00")
        self.assertEqual(row["detected_at"], "2026-06-18T16:33:00+00:00")
        self.assertEqual(row["outcome_window_minutes_requested"], [5, 15, 30, 60])
        self.assertIn("bars_start_requested", row)
        self.assertIn("bars_end_requested", row)
        self.assertEqual(row["reliability_status"], "penalty_only_insufficient_history")
        self.assertEqual(row["reliability_meaningful_rate"], 0.44)
        self.assertEqual(row["reliability_block_reasons"], ["insufficient sessions or paired effective samples"])

    def test_option_bars_retry_and_record_attempt_diagnostics(self):
        detected = datetime(2026, 6, 18, 16, 33, tzinfo=timezone.utc)
        class RetryClient(FakeBarsClient):
            def __init__(self, bars_by_symbol):
                super().__init__(bars_by_symbol)
                self.option_calls = 0
            def get_option_bars(self, symbols, *, start, end):
                self.option_calls += 1
                return {} if self.option_calls == 1 else {symbols[0]: [{"t": detected.isoformat(), "o": 1, "h": 1, "l": 1, "c": 1}]}
        client = RetryClient({"ADBE": self.bars(detected, [0, 5, 15, 30, 60])})
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.run_review(Path(temp_dir), client)
        self.assertEqual(client.option_calls, 2)
        self.assertEqual([item["rows"] for item in result["reviewed"][0]["option_bar_fetch_attempts"]], [0, 1])

    def test_unavailable_option_bars_always_have_detailed_failure(self):
        detected = datetime(2026, 6, 18, 16, 33, tzinfo=timezone.utc)
        class EmptyOptionClient(FakeBarsClient):
            def get_option_bars(self, symbols, *, start, end): return {}
        client = EmptyOptionClient({"ADBE": self.bars(detected, [0, 5, 15, 30, 60])})
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.run_review(Path(temp_dir), client)
        failure = result["reviewed"][0]["option_bar_failure"]
        self.assertEqual(failure["option_symbol"], "ADBETESTC")
        self.assertEqual(failure["endpoint"], "historical_option_bars")
        self.assertEqual(failure["attempt_count"], 3)
        self.assertEqual(failure["final_disposition"], "retry_exhausted")

    def test_duplicate_pending_reviews_are_not_appended_repeatedly(self):
        detected = datetime(2026, 6, 18, 16, 33, tzinfo=timezone.utc)
        fake_client = FakeBarsClient({"ADBE": self.bars(detected, [0, 1])})
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            first = self.run_review(tmp_path, fake_client)
            second = self.run_review(tmp_path, fake_client)
            rows = (tmp_path / "options_whale_outcomes.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(first["appended_count"], 1)
        self.assertEqual(second["appended_count"], 0)
        self.assertEqual(second["unchanged_pending_count"], 1)
        self.assertEqual(len(rows), 1)

    def test_force_appends_unchanged_pending_review(self):
        detected = datetime(2026, 6, 18, 16, 33, tzinfo=timezone.utc)
        fake_client = FakeBarsClient({"ADBE": self.bars(detected, [0, 1])})
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            self.run_review(tmp_path, fake_client)
            forced = self.run_review(tmp_path, fake_client, force=True)
            rows = (tmp_path / "options_whale_outcomes.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(forced["appended_count"], 1)
        self.assertEqual(len(rows), 2)

    def test_pending_alert_later_appends_when_windows_complete(self):
        detected = datetime(2026, 6, 18, 16, 33, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            first_client = FakeBarsClient({"ADBE": self.bars(detected, [0, 1])})
            first = self.run_review(tmp_path, first_client)
            second_client = FakeBarsClient({"ADBE": self.bars(detected, range(0, 77))})
            second = self.run_review(tmp_path, second_client)
            rows = [
                json.loads(line)
                for line in (tmp_path / "options_whale_outcomes.jsonl").read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(first["pending_count"], 1)
        self.assertEqual(second["completed_count"], 1)
        self.assertEqual(second["appended_count"], 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["completed_window_count"], 4)


if __name__ == "__main__":
    unittest.main()
