import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from post_alert_performance import PostAlertPerformanceTracker, alert_tracking_record, update_performance_record


class PostAlertPerformanceV2Tests(unittest.TestCase):
    def alert(self, **updates):
        row = {
            "timestamp": datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc),
            "symbol": "AAPL", "direction": "BULLISH", "price": 100,
            "setup_name": "Pullback Holding", "entry_quality_label": "GOOD_POSITION",
            "phase3_delivery_requested": True, "phase3_delivery_attempted": False,
            "phase3_delivery_succeeded": False, "phase3_heads_up_final_decision": "ELIGIBLE_PENDING_DELIVERY",
            "orchestrator_final_alert_type": "TREND_CONTEXT", "orchestrator_decision_reason": "aligned trend",
            "option_quality_reasons": ["wide_spread", "stale_quote"],
        }
        row.update(updates)
        return row

    def bars(self, closes):
        start = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
        return [{"t": start + timedelta(minutes=i), "h": close + .02, "l": close - .02, "c": close} for i, close in enumerate(closes)]

    def test_meaningful_move_definitions_are_explicit(self):
        record = alert_tracking_record(self.alert())
        result = update_performance_record(record, self.bars([100, 100.02, 100.04, 100.08, 100.11, 100.15]), now=datetime(2026, 7, 6, 14, 6, tzinfo=timezone.utc), intervals=(1, 5))
        self.assertEqual(result["outcome_definition_version"], "v2_meaningful_move_0.10")
        self.assertTrue(result["useful_alert"])
        self.assertTrue(result["alert_was_early"])
        self.assertFalse(result["should_be_blocked_next_time"])

    def test_adverse_meaningful_move_is_block_next(self):
        record = alert_tracking_record(self.alert(entry_quality_label="LATE"))
        result = update_performance_record(record, self.bars([100, 99.95, 99.85, 99.80, 99.75, 99.70]), now=datetime(2026, 7, 6, 14, 6, tzinfo=timezone.utc), intervals=(1, 5))
        self.assertTrue(result["alert_was_late"])
        self.assertTrue(result["should_be_blocked_next_time"])
        self.assertFalse(result["useful_alert"])

    def test_canonical_episode_contains_decision_delivery_option_and_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracker = PostAlertPerformanceTracker(root/"performance.jsonl", root/"pending.json", intervals=(1,), episode_path=root/"episodes.jsonl")
            initial = tracker.register(self.alert())
            tracker.update({"AAPL": type("Snapshot", (), {"recent_bars": self.bars([100, 100.2])})()}, now=datetime(2026, 7, 6, 14, 2, tzinfo=timezone.utc))
            rows = [json.loads(line) for line in (root/"episodes.jsonl").read_text().splitlines()]
        self.assertEqual(rows[0]["canonical_episode_id"], initial["alert_id"])
        self.assertEqual(rows[-1]["orchestrator_final_alert_type"], "TREND_CONTEXT")
        self.assertFalse(rows[-1]["delivery_attempted"])
        self.assertIn("stale_quote", rows[-1]["option_quality_reasons"])
        self.assertTrue(rows[-1]["useful_alert"])


if __name__ == "__main__":
    unittest.main()
