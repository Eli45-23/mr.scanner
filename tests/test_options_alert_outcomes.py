import unittest
from datetime import datetime, timedelta, timezone

from scanner.options_alert_outcomes import _first_bar_at_or_after, _parse_time, evaluate_alert_outcome, evaluate_option_price_outcome, infer_flow_bias, infer_flow_bias_details, summarize_outcomes


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
        self.assertTrue(outcome["windows"][0]["meaningful_0_20"])

    def test_option_price_outcome_reports_reference_and_executable_long_return(self):
        start = datetime(2026, 6, 17, 14, 30, tzinfo=timezone.utc)
        alert = {
            "timestamp": start.isoformat(),
            "aggression_side": "near_ask",
            "candidate": {"contract_price_paid": 1.05, "bid": 1.00, "ask": 1.10},
        }
        bars = [{"t": (start + timedelta(minutes=5)).isoformat(), "c": 1.30}]
        quotes = [{"t": (start + timedelta(minutes=5)).isoformat(), "bp": 1.25, "ap": 1.35}]
        result = evaluate_option_price_outcome(alert, bars, quotes, windows=(5,))
        self.assertEqual(result["option_position_side"], "LONG")
        self.assertGreater(result["option_windows"][0]["reference_return_pct"], 0)
        self.assertGreater(result["option_windows"][0]["estimated_executable_return_pct"], 0)

    def test_option_price_outcome_does_not_invent_executable_return_without_quotes(self):
        start = datetime(2026, 6, 17, 14, 30, tzinfo=timezone.utc)
        alert = {"timestamp": start.isoformat(), "aggression_side": "near_ask", "candidate": {"last": 1.0}}
        result = evaluate_option_price_outcome(alert, [{"t": (start + timedelta(minutes=5)).isoformat(), "c": 1.2}], [], windows=(5,))
        self.assertIsNone(result["option_windows"][0]["estimated_executable_return_pct"])
        self.assertEqual(result["option_windows"][0]["executable_status"], "historical_quote_unavailable")

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

    def test_alpaca_z_timestamps_parse_and_match_exact_target_bar(self):
        target = datetime(2026, 6, 18, 16, 38, tzinfo=timezone.utc)
        parsed = _parse_time("2026-06-18T16:38:00Z")
        self.assertEqual(parsed, target)
        bar = _first_bar_at_or_after([{"t": "2026-06-18T16:38:00Z", "c": 101.0}], target)
        self.assertIsNotNone(bar)
        self.assertEqual(bar["c"], 101.0)

    def test_first_bar_at_or_after_uses_later_bar_when_exact_missing(self):
        target = datetime(2026, 6, 18, 16, 38, tzinfo=timezone.utc)
        bars = [
            {"t": "2026-06-18T16:37:00Z", "c": 100.0},
            {"t": "2026-06-18T16:39:00Z", "c": 101.0},
        ]
        bar = _first_bar_at_or_after(bars, target)
        self.assertIsNotNone(bar)
        self.assertEqual(bar["t"], "2026-06-18T16:39:00Z")

    def test_alert_with_bars_through_later_time_completes_all_windows(self):
        detected = datetime(2026, 6, 18, 16, 33, tzinfo=timezone.utc)
        alert = {
            "timestamp": "2026-06-18T16:33:00Z",
            "candidate": {"option_type": "CALL", "underlying_price": 100.0},
        }
        bars = [
            {"t": (detected + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"), "c": 100.0 + minutes / 10}
            for minutes in range(0, 137)
        ]
        outcome = evaluate_alert_outcome(alert, bars, windows=(5, 15, 30, 60))
        self.assertEqual(outcome["outcome_status"], "ok")
        self.assertEqual(outcome["completed_window_count"], 4)
        self.assertEqual(outcome["pending_window_count"], 0)

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
            {"outcome_status": "ok", "max_favorable_move_pct": 1.2, "windows": [{"status": "ok", "move_pct": 1.2, "favorable": True}]},
            {"outcome_status": "partial", "max_favorable_move_pct": -0.3, "windows": [{"status": "ok", "move_pct": -0.3, "favorable": False}]},
            {"outcome_status": "partial", "max_favorable_move_pct": None, "windows": [{"status": "missing_bar", "favorable": None}]},
            {"outcome_status": "pending", "windows": []},
            {"outcome_status": "insufficient_future_session", "windows": []},
            {"outcome_status": "missing_start_context", "windows": []},
        ]
        summary = summarize_outcomes(rows)
        self.assertEqual(summary["count"], 6)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["insufficient_future_session"], 1)
        self.assertEqual(summary["dirty_completed_ignored"], 1)
        self.assertEqual(summary["favorable_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
