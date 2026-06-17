import json
import tempfile
import unittest
from pathlib import Path

from tools.summarize_options_outcomes import (
    latest_by_alert_key,
    score_bucket,
    summarize_group,
    summarize_outcome_file,
    unusualness_bucket,
)


class OptionsOutcomeSummaryTests(unittest.TestCase):
    def row(self, key, symbol="QQQ", bias="BEARISH", status="ok", favorable=True, score=85, option_type="PUT"):
        return {
            "alert_key": key,
            "underlying_symbol": symbol,
            "flow_bias": bias,
            "flow_bias_source": "direction_label",
            "option_type": option_type,
            "alert_tier": "Tier 2",
            "whale_score": score,
            "outcome_status": status,
            "max_favorable_move_pct": 0.42 if favorable else -0.15,
            "max_adverse_move_pct": -0.1,
            "score_components": {"historical_unusualness": 12},
            "windows": [{"favorable": favorable}],
        }

    def test_score_bucket(self):
        self.assertEqual(score_bucket(95), "90-100")
        self.assertEqual(score_bucket(84), "80-89")
        self.assertEqual(score_bucket(None), "unknown")

    def test_unusualness_bucket(self):
        self.assertEqual(unusualness_bucket({"score_components": {"historical_unusualness": 12}}), "12+ extreme")
        self.assertEqual(unusualness_bucket({"score_components": {"historical_unusualness": 5}}), "4-7 moderate")
        self.assertEqual(unusualness_bucket({}), "unknown")

    def test_latest_by_alert_key_keeps_latest_record(self):
        rows = [
            self.row("a", status="pending"),
            self.row("a", status="ok", favorable=True),
            self.row("b", status="pending"),
        ]
        latest = latest_by_alert_key(rows)
        self.assertEqual(len(latest), 2)
        self.assertEqual(next(row for row in latest if row["alert_key"] == "a")["outcome_status"], "ok")

    def test_summarize_group_counts_completed_pending_and_rate(self):
        summary = summarize_group([
            self.row("a", favorable=True),
            self.row("b", favorable=False),
            self.row("c", status="pending"),
        ])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["favorable_rate"], 0.5)

    def test_summarize_outcome_file_groups_results(self):
        rows = [
            self.row("a", symbol="QQQ", bias="BEARISH", favorable=True),
            self.row("b", symbol="QQQ", bias="BEARISH", favorable=False),
            self.row("c", symbol="NVDA", bias="BULLISH", status="pending", favorable=False, option_type="CALL"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "outcomes.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            report = summarize_outcome_file(path, min_completed=0)
        self.assertEqual(report["raw_record_count"], 3)
        self.assertEqual(report["unique_alert_count"], 3)
        self.assertEqual(report["overall"]["completed"], 2)
        symbol_flow = {row["key"]: row for row in report["groups"]["symbol_flow_bias"]}
        self.assertIn("QQQ|BEARISH", symbol_flow)
        self.assertEqual(symbol_flow["QQQ|BEARISH"]["favorable_rate"], 0.5)
        self.assertIn("NVDA|BULLISH", symbol_flow)
        self.assertEqual(symbol_flow["NVDA|BULLISH"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()
