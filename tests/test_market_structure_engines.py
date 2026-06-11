from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from unittest.mock import patch

import elite_momentum_scanner as scanner_app
from scanner.market_structure_models import combine_market_structure, resample_bars
from scanner.supply_demand_engine import detect_supply_demand
from scanner.support_resistance_engine import detect_support_resistance
from tools import preview_market_structure
from tools.preview_market_structure import build_market_structure, render_pretty


def bars_from_closes(closes: list[float], *, volumes: list[float] | None = None) -> list[dict]:
    start = datetime(2026, 6, 10, 13, 30, tzinfo=timezone.utc)
    volumes = volumes or [1000.0] * len(closes)
    return [
        {
            "t": start + timedelta(minutes=index),
            "o": close - 0.04 if index % 2 == 0 else close + 0.04,
            "h": close + 0.15,
            "l": close - 0.15,
            "c": close,
            "v": volumes[index],
        }
        for index, close in enumerate(closes)
    ]


class SupportResistanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.closes = [100.4, 100.1, 99.8, 100.2, 100.7, 101.0, 100.7, 100.3, 100.8, 101.15, 100.9, 100.5, 101.0]
        self.bars = bars_from_closes(self.closes)

    def test_detects_swing_high_and_low(self) -> None:
        result = detect_support_resistance("AAPL", "1m", self.bars)
        self.assertTrue(result["support_levels"])
        self.assertTrue(result["resistance_levels"])
        self.assertIn("swing_low", " ".join(item["source"] for item in result["support_levels"]))
        self.assertIn("swing_high", " ".join(item["source"] for item in result["resistance_levels"]))

    def test_repeated_bounce_and_rejection_rank_higher(self) -> None:
        result = detect_support_resistance(
            "AAPL",
            "5m",
            self.bars,
            known_levels={"vwap": 100.0, "ema9": 100.02, "pdh": 101.15},
        )
        scores = [item["score"] for item in result["support_levels"] + result["resistance_levels"]]
        self.assertTrue(scores)
        self.assertGreaterEqual(max(scores), 75)

    def test_role_reversal_sources_are_detected(self) -> None:
        crossing = bars_from_closes([99.7, 99.9, 100.1, 100.3, 100.05, 100.2, 99.95, 99.8, 100.0])
        result = detect_support_resistance("AAPL", "1m", crossing)
        sources = " ".join(item["source"] for item in result["support_levels"] + result["resistance_levels"])
        self.assertTrue("retest_old_resistance" in sources or "retest_old_support" in sources)

    def test_merges_duplicates_and_limits_output(self) -> None:
        result = detect_support_resistance(
            "AAPL",
            "15m",
            self.bars,
            known_levels={"vwap": 100.00, "ema9": 100.01, "ema20": 100.02, "pdl": 99.0, "lod": 98.0, "pdh": 101.2, "hod": 102.0},
            max_levels=3,
        )
        self.assertLessEqual(len(result["support_levels"]), 3)
        self.assertLessEqual(len(result["resistance_levels"]), 3)
        near_100 = [item for item in result["support_levels"] if abs(item["price"] - 100.01) < 0.08]
        self.assertLessEqual(len(near_100), 1)

    def test_missing_data_fails_safely(self) -> None:
        result = detect_support_resistance("AAPL", "1m", [])
        self.assertEqual(result["support_levels"], [])
        self.assertIn("Not enough", result["reason"])

    def test_accepts_resampled_timeframe_inputs(self) -> None:
        for minutes, name in ((1, "1m"), (5, "5m"), (15, "15m")):
            result = detect_support_resistance("AAPL", name, resample_bars(self.bars * 3, minutes))
            self.assertEqual(result["timeframe"], name)


class SupplyDemandTests(unittest.TestCase):
    def test_detects_bullish_demand_and_bearish_supply(self) -> None:
        closes = [100.0, 99.9, 99.85, 100.7, 100.9, 101.0, 101.1, 100.2, 100.0, 99.9]
        volumes = [800, 800, 900, 3000, 1200, 800, 900, 3200, 1200, 900]
        result = detect_supply_demand("AAPL", "5m", bars_from_closes(closes, volumes=volumes), current_price=100.5)
        self.assertTrue(result["demand_zones"])
        self.assertTrue(result["supply_zones"])

    def test_fresh_tested_and_weakened_zones(self) -> None:
        bars = bars_from_closes([100, 99.8, 100.8, 100.4, 100.0, 100.7, 100.1, 100.8, 100.5])
        result = detect_supply_demand("AAPL", "1m", bars, current_price=100.6)
        zones = result["demand_zones"] + result["supply_zones"]
        self.assertTrue(zones)
        self.assertTrue(any(item["fresh"] or item["times_tested"] > 0 for item in zones))
        self.assertTrue(all(item["strength"] in {"LOW", "MEDIUM", "HIGH"} for item in zones))

    def test_merges_ranks_limits_and_finds_nearest(self) -> None:
        closes = [100, 99.8, 100.8, 99.85, 100.9, 101.1, 100.2, 101.2, 100.5, 101.0]
        result = detect_supply_demand("AAPL", "15m", bars_from_closes(closes), current_price=100.6, max_zones=3)
        self.assertLessEqual(len(result["demand_zones"]), 3)
        self.assertLessEqual(len(result["supply_zones"]), 3)
        self.assertEqual(result["demand_zones"], sorted(result["demand_zones"], key=lambda item: (-item["score"], abs(100.6 - item["midpoint"]))))
        if result["demand_zones"]:
            self.assertTrue(result["nearest_demand_below"])
        if result["supply_zones"]:
            self.assertTrue(result["nearest_supply_above"])

    def test_missing_data_fails_safely(self) -> None:
        result = detect_supply_demand("AAPL", "1m", [])
        self.assertEqual(result["demand_zones"], [])
        self.assertIn("Not enough", result["reason"])


class MultiTimeframeAndPreviewTests(unittest.TestCase):
    def test_summary_detects_confluence_and_chop_without_approval(self) -> None:
        sr = {
            frame: {
                "current_price": 100.5,
                "support_levels": [{"price": 100.0 + offset, "score": 80}],
                "resistance_levels": [{"price": 101.0 + offset, "score": 80}],
            }
            for frame, offset in (("1m", 0.01), ("5m", 0.0), ("15m", -0.01))
        }
        sd = {
            frame: {
                "current_price": 100.5,
                "demand_zones": [{"zone_low": 99.9, "zone_high": 100.1, "midpoint": 100.0 + offset, "score": 80}],
                "supply_zones": [{"zone_low": 100.9, "zone_high": 101.1, "midpoint": 101.0 + offset, "score": 80}],
            }
            for frame, offset in (("1m", 0.01), ("5m", 0.0), ("15m", -0.01))
        }
        summary = combine_market_structure("AAPL", sr, sd)
        self.assertTrue(summary["support_confluence"])
        self.assertTrue(summary["resistance_confluence"])
        self.assertTrue(summary["demand_confluence"])
        self.assertTrue(summary["supply_confluence"])
        self.assertTrue(summary["chop_range_detected"])
        self.assertFalse(summary["can_approve_trades"])
        self.assertEqual(summary["market_structure_bias"], "MIXED")

    def test_market_structure_can_upgrade_defaults_false(self) -> None:
        config = scanner_app.load_config(None)
        self.assertFalse(config["market_structure_engines"]["can_upgrade"])
        self.assertTrue(config["market_structure_engines"]["enable_dashboard"])

    def test_preview_build_and_render_are_read_only_and_json_safe(self) -> None:
        bars = bars_from_closes([100 + ((index % 8) - 4) * 0.12 for index in range(60)])
        payload = build_market_structure("AAPL", bars, config=scanner_app.load_config(None))
        encoded = json.dumps(payload, default=str)
        text = render_pretty(payload)
        self.assertIn('"context_only": true', encoded)
        self.assertIn("No alerts, OpenAI calls, Telegram messages, or orders", text)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", text)
        self.assertNotIn("OPENAI_API_KEY", text)
        self.assertFalse(payload["can_approve_trades"])

    def test_preview_json_cli_uses_only_market_data_provider(self) -> None:
        bars = bars_from_closes([100 + ((index % 8) - 4) * 0.12 for index in range(60)])

        class Provider:
            def get_recent_bars(self, symbols, start, end):
                return {"AAPL": bars}

            def get_daily_bars(self, symbols, start, end):
                return {"AAPL": []}

        output = io.StringIO()
        with (
            patch.object(preview_market_structure.scanner, "make_provider", return_value=Provider()),
            patch.object(preview_market_structure.scanner, "load_dotenv"),
            patch.object(sys, "argv", ["preview_market_structure.py", "--symbol", "AAPL", "--json", "--no-log"]),
            redirect_stdout(output),
        ):
            self.assertEqual(preview_market_structure.main(), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["symbol"], "AAPL")
        self.assertTrue(payload["context_only"])
        self.assertFalse(payload["can_approve_trades"])

    def test_preview_writes_three_safe_jsonl_logs(self) -> None:
        bars = bars_from_closes([100 + ((index % 8) - 4) * 0.12 for index in range(60)])
        payload = build_market_structure("AAPL", bars, config=scanner_app.load_config(None))
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = {
                "support_resistance": preview_market_structure.Path(temp_dir) / "support.jsonl",
                "supply_demand": preview_market_structure.Path(temp_dir) / "zones.jsonl",
                "summary": preview_market_structure.Path(temp_dir) / "summary.jsonl",
            }
            with patch.object(preview_market_structure, "LOG_PATHS", paths):
                preview_market_structure.write_logs(payload, scanner_app.load_config(None))
            for path in paths.values():
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.strip())
                json.loads(text.splitlines()[0])
                self.assertNotIn("ALPACA_SECRET_KEY", text)
                self.assertNotIn("TELEGRAM_BOT_TOKEN", text)


if __name__ == "__main__":
    unittest.main()
