import json
import tempfile
import unittest
from pathlib import Path

from tools.options_outcome_dashboard import outcome_payload


class OptionsOutcomeDashboardTests(unittest.TestCase):
    def test_outcome_payload_includes_report_and_selected_group(self):
        row = {
            "alert_key": "a",
            "underlying_symbol": "QQQ",
            "flow_bias": "BULLISH",
            "flow_bias_source": "direction_label",
            "option_type": "CALL",
            "alert_tier": "Tier 2",
            "whale_score": 88,
            "outcome_status": "ok",
            "completed_window_count": 1,
            "pending_window_count": 0,
            "max_favorable_move_pct": 0.25,
            "max_adverse_move_pct": -0.05,
            "score_components": {"historical_unusualness": 12},
            "windows": [{"status": "ok", "move_pct": 0.25, "favorable": True}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "outcomes.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            payload = outcome_payload("symbol_flow_bias", path)
        self.assertEqual(payload["selected_group"], "symbol_flow_bias")
        self.assertEqual(payload["overall"]["completed"], 1)
        self.assertEqual(payload["overall"]["favorable_rate"], 1.0)
        keys = [item["key"] for item in payload["groups"]["symbol_flow_bias"]]
        self.assertIn("QQQ|BULLISH", keys)


if __name__ == "__main__":
    unittest.main()
