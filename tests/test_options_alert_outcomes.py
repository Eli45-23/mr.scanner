import unittest
from datetime import datetime, timedelta, timezone

from scanner.options_alert_outcomes import evaluate_alert_outcome, infer_flow_bias, infer_flow_bias_details, summarize_outcomes


class OptionsAlertOutcomeTests(unittest.TestCase):
    def bars(self, start, closes):
        return [
            {"t": (start + timedelta(minutes=i)).isoformat(), "c": close}
            for i, close in enumerate(closes)
        ]

    def test_infers_bias_from_option_type(self):
        self.assertEqual(infer_flow_bias({"candidate": {"option_type": "CALL"}}), "BULLISH")
        self.assertEqual(infer_flow_bias({"candidate": {"option_type": "PUT"}}), "BEARISH")
        details = infer_flow_bias_details({"candidate": {"option_type": "PUT"}})
        self.assertEqual(details["flow_bias_source"], "option_type_fallback")
        self.assertIn("PUT flow defaults bearish", details["flow_bias_reason"])

    def test_direction_label_overrides_option_type_bias(self):
        details = infer_flow_bias_details({
            "direction_label": "Possible bullish sold-put flow",
            "candidate": {"option_type": "PUT"},
        })
        self.assertEqual(details["flow_bias"], "BULLISH")
        self.assertEqual(details["flow_bias_source"], "direction_label")
        self.assertIn("bullish", details["flow_bias_reason"].lower())

    def test_call_flow_favorable_when_price_rises(self):
        start = datetime(2026, 6, 17, 14, 30, tzinfo=timezone.utc)
        alert = {
            "timestamp": start.isoformat(),
            "candidate": {
                "underlying_symbol": "NVDA",
                "option_symbol": "NVDATESTC",
                "option_type": "CALL",
                "underlying_price": 100.0,
            },
        }
        outcome = evaluate_alert_outcome(alert, self.bars(start, [100, 101, 102, 103, 104, 105, 106]), windows=(5,))
        self.assertEqual(outcome["outcome_status"], "ok")
        self.assertEqual(outcome["flow_bias"], "BULLISH")
        self.assertEqual(outcome["flow_bias_source"], "option_type_fallback")
        self.assertTrue(outcome["windows"][0]["favorable"])
        self.assertGreater(outcome["max_favorable_move_pct"], 0)

    def test_put_flow_favorable_when_price_falls(self):
        start = datetime(2026, 6, 17, 14, 30, tzinfo=timezone.utc)
        alert = {
            "timestamp": start.isoformat(),
            "candidate": {
                "underlying_symbol": "QQQ",
                "option_symbol": "QQQTESTP",
                "option_type": "PUT",
                "underlying_price": 100.0,
            },
        }
        outcome = evaluate_alert_outcome(alert, self.bars(start, [100, 99, 98, 97, 96, 95, 94]), windows=(5,))
        self.assertEqual(outcome["flow_bias"], "BEARISH")
        self.assertTrue(outcome["windows"][0]["favorable"])
        self.assertGreater(outcome["max_favorable_move_pct"], 0)

    def test_pending_when_no_future_window_bars_exist_yet(self):
        start = datetime(2026, 6, 17, 14, 30, tzinfo=timezone.utc)
        alert = {
            "timestamp": start.isoformat(),
            "candidate": {"option_type": "CALL", "underlying_price": 100.0},
        }
        outcome = evaluate_alert_outcome(alert, self.bars(start, [100, 100.2]), windows=(5, 15))
        self.assertEqual(outcome["outcome_status"], "pending")
        self.assertEqual(outcome["completed_window_count"], 0)
        self.assertEqual(outcome["pending_window_count"], 2)
        self.assertEqual(outcome["insufficient_window_count"], 0)

    def test_partial_when_some_windows_are_complete(self):
        start = datetime(2026, 6, 17, 14, 30, tzinfo=timezone.utc)
        alert = {
            "timestamp": start.isoformat(),
            "candidate": {"option_type": "CALL", "underlying_price": 100.0},
        }
        outcome = evaluate_alert_outcome(alert, self.bars(start, [100, 101, 102, 103, 104, 105, 106]), windows=(5, 15))
        self.assertEqual(outcome["outcome_status"], "partial")
        self.assertEqual(outcome["completed_window_count"], 1)
        self.assertEqual(outcome["pending_window_count"], 1)

    def test_market_close_marks_unavailable_windows_insufficient(self):
        start = datetime(2026, 6, 17, 19, 58, tzinfo=timezone.utc)  # 3:58 PM New York time.
        alert = {
            "timestamp": start.isoformat(),
            "candidate": {"option_type": "CALL", "underlying_price": 100.0},
        }
        outcome = evaluate_alert_outcome(alert, self.bars(start, [100, 100.2]), windows=(5, 15, 30, 60))
        self.assertEqual(outcome["outcome_status"], "insufficient_future_session")
        self.assertEqual(outcome["completed_window_count"], 0)
        self.assertEqual(outcome["pending_window_count"], 0)
        self.assertEqual(outcome["insufficient_window_count"], 4)
        self.assertTrue(all(item["status"] == "insufficient_future_session" for item in outcome["windows"]))

    def test_market_close_can_still_return_partial_for_available_window(self):
        start = datetime(2026, 6, 17, 19, 50, tzinfo=timezone.utc)  # 3:50 PM New York time.
        alert = {
            "timestamp": start.isoformat(),
            "candidate": {"option_type": "CALL", "underlying_price": 100.0},
        }
        outcome = evaluate_alert_outcome(alert, self.bars(start, [100, 101, 102, 103, 104, 105, 106]), windows=(5, 15))
        self.assertEqual(outcome["outcome_status"], "partial")
        self.assertEqual(outcome["completed_window_count"], 1)
        self.assertEqual(outcome["insufficient_window_count"], 1)

    def test_missing_context_is_safe(self):
        outcome = evaluate_alert_outcome({"candidate": {"option_type": "CALL"}}, [], windows=(5,))
        self.assertEqual(outcome["outcome_status"], "missing_start_context")
        self.assertEqual(outcome["windows"], [])
        self.assertIn("flow_bias_source", outcome)

    def test_summarizes_outcomes(self):
        rows = [
            {"outcome_status": "ok", "max_favorable_move_pct": 1.2, "windows": [{"favorable": True}]},
            {"outcome_status": "partial", "max_favorable_move_pct": -0.3, "windows": [{"favorable": False}]},
            {"outcome_status": "pending", "windows": []},
            {"outcome_status": "insufficient_future_session", "windows": []},
            {"outcome_status": "missing_start_context", "windows": []},
        ]
        summary = summarize_outcomes(rows)
        self.assertEqual(summary["count"], 5)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["insufficient_future_session"], 1)
        self.assertEqual(summary["favorable_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
