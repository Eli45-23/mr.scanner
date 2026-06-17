import unittest
from datetime import datetime, timedelta, timezone

from scanner.options_alert_outcomes import evaluate_alert_outcome, infer_flow_bias, summarize_outcomes


class OptionsAlertOutcomeTests(unittest.TestCase):
    def bars(self, start, closes):
        return [
            {"t": (start + timedelta(minutes=i)).isoformat(), "c": close}
            for i, close in enumerate(closes)
        ]

    def test_infers_bias_from_option_type(self):
        self.assertEqual(infer_flow_bias({"candidate": {"option_type": "CALL"}}), "BULLISH")
        self.assertEqual(infer_flow_bias({"candidate": {"option_type": "PUT"}}), "BEARISH")

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

    def test_missing_context_is_safe(self):
        outcome = evaluate_alert_outcome({"candidate": {"option_type": "CALL"}}, [], windows=(5,))
        self.assertEqual(outcome["outcome_status"], "missing_start_context")
        self.assertEqual(outcome["windows"], [])

    def test_summarizes_outcomes(self):
        rows = [
            {"outcome_status": "ok", "max_favorable_move_pct": 1.2, "windows": [{"favorable": True}]},
            {"outcome_status": "ok", "max_favorable_move_pct": -0.3, "windows": [{"favorable": False}]},
            {"outcome_status": "missing_start_context", "windows": []},
        ]
        summary = summarize_outcomes(rows)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["favorable_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
