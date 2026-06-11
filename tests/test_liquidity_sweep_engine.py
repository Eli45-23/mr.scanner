from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import elite_momentum_scanner as scanner
from scanner.liquidity_sweep_engine import evaluate_liquidity_sweeps
from scanner.liquidity_sweep_telegram import (
    DISCLAIMER,
    claim_sweep_delivery,
    format_liquidity_sweep_message,
    select_liquidity_sweep_message,
    sweep_telegram_eligibility,
    validate_liquidity_sweep_message,
)
from tools import preview_liquidity_sweeps
from tools import export_review_package
from strategies.base import StrategyContext
from strategies.liquidity_sweep import evaluate as evaluate_sweep_strategy
from strategies.scoring import evaluate_strategy_suite


START = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)


def candles(latest: dict) -> list[dict]:
    prior = [
        {"t": START + timedelta(minutes=index), "o": 100.5, "h": 100.65, "l": 100.35, "c": 100.5, "v": 1000}
        for index in range(20)
    ]
    return prior + [{"t": START + timedelta(minutes=20), "v": 1600, **latest}]


def structure(*, chop: bool = False) -> dict:
    return {
        "support_resistance": {
            "1m": {"support_levels": [], "resistance_levels": []},
            "5m": {
                "support_levels": [{"price": 100.0, "score": 80}],
                "resistance_levels": [{"price": 101.0, "score": 80}],
            },
            "15m": {"support_levels": [], "resistance_levels": []},
        },
        "supply_demand": {
            "1m": {"demand_zones": [], "supply_zones": []},
            "5m": {
                "demand_zones": [{"zone_low": 100.0, "zone_high": 100.2, "midpoint": 100.1, "score": 85}],
                "supply_zones": [{"zone_low": 100.8, "zone_high": 101.0, "midpoint": 100.9, "score": 85}],
            },
            "15m": {"demand_zones": [], "supply_zones": []},
        },
        "summary": {"chop_range_detected": chop, "structure_warning": "inside chop range" if chop else "no clean edge"},
    }


class LiquiditySweepEngineTests(unittest.TestCase):
    def evaluate(self, latest: dict, *, closed: bool = True, chop: bool = False) -> dict:
        return evaluate_liquidity_sweeps(
            "AAPL", candles(latest), market_structure=structure(chop=chop),
            current_candle_closed=closed, watch_distance_bps=8,
        )

    def test_sweep_watch_near_supply(self) -> None:
        result = self.evaluate({"o": 100.92, "h": 100.98, "l": 100.9, "c": 100.95})
        self.assertEqual(result["sweep_status"], "SWEEP_WATCH")
        self.assertEqual(result["sweep_direction"], "ABOVE_LEVEL")

    def test_sweep_forming_above_supply_before_close(self) -> None:
        result = self.evaluate({"o": 100.95, "h": 101.08, "l": 100.9, "c": 101.04}, closed=False)
        self.assertEqual(result["sweep_status"], "SWEEP_FORMING")
        self.assertNotEqual(result["sweep_status"], "SWEEP_CONFIRMED")

    def test_confirmed_upside_sweep_after_close_below_supply(self) -> None:
        result = self.evaluate({"o": 100.95, "h": 101.12, "l": 100.65, "c": 100.72}, chop=True)
        self.assertEqual(result["sweep_status"], "SWEEP_CONFIRMED")
        self.assertEqual(result["trap_bias"], "BEARISH")
        self.assertEqual(result["level_source"], "5m_supply")
        self.assertIn("closed back below", result["reason"])

    def test_failed_sweep_when_price_holds_above_supply(self) -> None:
        result = self.evaluate({"o": 100.95, "h": 101.16, "l": 100.9, "c": 101.1})
        self.assertEqual(result["sweep_status"], "SWEEP_FAILED_HELD")
        self.assertEqual(result["trap_bias"], "NEUTRAL")

    def test_sweep_watch_near_demand(self) -> None:
        result = self.evaluate({"o": 100.07, "h": 100.1, "l": 100.02, "c": 100.05})
        self.assertEqual(result["sweep_status"], "SWEEP_WATCH")
        self.assertEqual(result["sweep_direction"], "BELOW_LEVEL")

    def test_sweep_forming_below_demand_before_close(self) -> None:
        result = self.evaluate({"o": 100.05, "h": 100.08, "l": 99.9, "c": 99.96}, closed=False)
        self.assertEqual(result["sweep_status"], "SWEEP_FORMING")

    def test_confirmed_downside_sweep_after_reclaim(self) -> None:
        result = self.evaluate({"o": 100.05, "h": 100.38, "l": 99.86, "c": 100.34}, chop=True)
        self.assertEqual(result["sweep_status"], "SWEEP_CONFIRMED")
        self.assertEqual(result["trap_bias"], "BULLISH")
        self.assertEqual(result["level_source"], "5m_demand")

    def test_failed_sweep_when_price_holds_below_demand(self) -> None:
        result = self.evaluate({"o": 100.05, "h": 100.08, "l": 99.82, "c": 99.9})
        self.assertEqual(result["sweep_status"], "SWEEP_FAILED_HELD")

    def test_confidence_scoring_and_context_only(self) -> None:
        result = self.evaluate({"o": 100.95, "h": 101.15, "l": 100.65, "c": 100.7}, chop=True)
        self.assertGreaterEqual(result["score"], 75)
        self.assertEqual(result["confidence"], "HIGH")
        self.assertFalse(result["can_approve_trades"])
        self.assertTrue(result["context_only"])

    def test_can_upgrade_defaults_false(self) -> None:
        self.assertFalse(scanner.load_config(None)["liquidity_sweep_engine"]["can_upgrade"])

    def test_missing_data_fails_safely(self) -> None:
        result = evaluate_liquidity_sweeps("AAPL", [], market_structure=structure())
        self.assertEqual(result["sweep_status"], "NO_ACTIVE_SWEEP")
        self.assertFalse(result["can_approve_trades"])


class LiquiditySweepStrategyAdapterTests(unittest.TestCase):
    def context(self, engine_context: dict | None, *, legacy_fallback: bool = True) -> StrategyContext:
        bar = scanner.Bar(t=START, o=100.0, h=101.0, l=99.0, c=100.5, v=1000)
        config = scanner.load_config(None)
        config["strategy_engine"]["liquidity_sweep_strategy_legacy_fallback"] = legacy_fallback
        return StrategyContext(
            symbol="AAPL",
            bars=[bar] * 6,
            latest=bar,
            config=config,
            levels={},
            relative_volume=1.0,
            market_alignment="ALIGNED",
            liquidity_sweep_context=engine_context,
        )

    def payload(self, status: str, direction: str = "BELOW_LEVEL", bias: str = "BULLISH", score: int = 82) -> dict:
        return {
            "sweep_status": status,
            "sweep_direction": direction,
            "trap_bias": bias,
            "score": score,
            "confidence": "HIGH",
            "level_source": "5m_demand" if direction == "BELOW_LEVEL" else "5m_supply",
            "sweep_level": 100.0,
            "sweep_zone_low": 100.0,
            "sweep_zone_high": 100.2,
            "reason": "Price swept the engine level and closed back through it.",
            "meaning": "Trapped participants may be present.",
            "context_only": True,
            "can_approve_trades": False,
        }

    def test_confirmed_downside_engine_sweep_adapts_to_bullish_reclaim(self) -> None:
        result = evaluate_sweep_strategy(self.context(self.payload("SWEEP_CONFIRMED")))
        self.assertTrue(result.active)
        self.assertEqual(result.label, "Bullish Liquidity Sweep Reclaim")
        self.assertEqual(result.direction, "bullish")
        self.assertLessEqual(result.score, 75)
        self.assertEqual(result.levels["source_of_truth"], "scanner_liquidity_sweep_engine")
        self.assertFalse(result.levels["can_approve_trades"])

    def test_confirmed_upside_engine_sweep_adapts_to_bearish_rejection(self) -> None:
        result = evaluate_sweep_strategy(
            self.context(self.payload("SWEEP_CONFIRMED", direction="ABOVE_LEVEL", bias="BEARISH"))
        )
        self.assertTrue(result.active)
        self.assertEqual(result.label, "Bearish Liquidity Sweep Rejection")
        self.assertEqual(result.direction, "bearish")

    def test_watch_and_forming_are_not_active_or_trade_ready(self) -> None:
        watch = evaluate_sweep_strategy(self.context(self.payload("SWEEP_WATCH", score=90)))
        forming = evaluate_sweep_strategy(self.context(self.payload("SWEEP_FORMING", score=90)))
        self.assertFalse(watch.active)
        self.assertLessEqual(watch.score, 45)
        self.assertFalse(forming.active)
        self.assertLessEqual(forming.score, 55)
        self.assertIn("candle has not closed", " ".join(forming.warnings).lower())

    def test_failed_held_does_not_label_trap(self) -> None:
        result = evaluate_sweep_strategy(self.context(self.payload("SWEEP_FAILED_HELD", score=90)))
        self.assertFalse(result.active)
        self.assertEqual(result.direction, "neutral")
        self.assertIn("Break Held", result.label)
        self.assertLessEqual(result.score, 35)

    def test_engine_context_prevents_legacy_conflicting_opinion(self) -> None:
        result = evaluate_sweep_strategy(self.context(self.payload("NO_ACTIVE_SWEEP")))
        self.assertFalse(result.active)
        self.assertEqual(result.label, "No Liquidity Sweep")
        self.assertEqual(result.levels["liquidity_sweep_source"], "engine")

    def test_legacy_fallback_can_be_disabled(self) -> None:
        result = evaluate_sweep_strategy(self.context(None, legacy_fallback=False))
        self.assertFalse(result.active)
        self.assertEqual(result.levels["liquidity_sweep_source"], "none")

    def test_strategy_suite_accepts_engine_context_and_reports_source(self) -> None:
        bars = [
            scanner.Bar(t=START + timedelta(minutes=index), o=100.0, h=100.8, l=99.8, c=100.2, v=1000)
            for index in range(20)
        ]
        summary = evaluate_strategy_suite(
            "AAPL",
            bars,
            bars[-1],
            scanner.load_config(None),
            {},
            1.0,
            "ALIGNED",
            liquidity_sweep_context=self.payload("SWEEP_CONFIRMED"),
        )
        sweep = next(item for item in summary["strategy_results"] if item["strategy"] == "liquidity_sweep")
        self.assertEqual(summary["liquidity_sweep_source"], "engine")
        self.assertFalse(summary["liquidity_sweep_can_approve_trades"])
        self.assertEqual(sweep["levels"]["source_of_truth"], "scanner_liquidity_sweep_engine")

    def test_scanner_passes_cached_engine_result_into_strategy_suite(self) -> None:
        instance = object.__new__(scanner.EliteScanner)
        instance.config = scanner.load_config(None)
        engine_payload = self.payload("SWEEP_CONFIRMED")
        instance.latest_liquidity_sweep_context = {"AAPL": engine_payload}
        bar = scanner.Bar(t=START, o=100.0, h=101.0, l=99.0, c=100.5, v=1000)
        snap = scanner.SymbolSnapshot(symbol="AAPL", latest_bar=bar, recent_bars=[bar])
        alert = scanner.Alert(symbol="AAPL", timestamp=START, category="WATCH", price=100.5, direction="BULLISH")
        with (
            patch.object(instance, "market_alignment_for", return_value="ALIGNED"),
            patch.object(instance, "strategy_levels_for_snapshot", return_value={}),
            patch.object(instance, "apply_mixed_signal_and_news_context"),
            patch.object(instance, "apply_aapl_bearish_continuation_label"),
            patch.object(scanner, "evaluate_strategy_suite", return_value={
                "liquidity_sweep_source": "engine",
                "liquidity_sweep_status": "SWEEP_CONFIRMED",
                "strategy_results": [],
            }) as suite,
        ):
            result = instance.attach_strategy_context(alert, snap, {})
        self.assertIs(suite.call_args.kwargs["liquidity_sweep_context"], engine_payload)
        self.assertEqual(result.liquidity_sweep_source, "engine")
        self.assertFalse(result.liquidity_sweep_can_approve_trades)


class LiquiditySweepPreviewTests(unittest.TestCase):
    def test_pretty_preview_is_copy_friendly(self) -> None:
        payload = evaluate_liquidity_sweeps(
            "AAPL", candles({"o": 100.95, "h": 101.12, "l": 100.65, "c": 100.72}),
            market_structure=structure(),
        )
        text = preview_liquidity_sweeps.render_pretty(payload)
        self.assertIn("Current price:", text)
        self.assertIn("Nearest upside sweep zone:", text)
        self.assertIn("Current sweep status:", text)
        self.assertIn("No alerts, Telegram messages, or orders were generated.", text)

    def test_log_output_is_valid_and_contains_no_secrets(self) -> None:
        payload = evaluate_liquidity_sweeps(
            "AAPL", candles({"o": 100.95, "h": 101.12, "l": 100.65, "c": 100.72}),
            market_structure=structure(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "liquidity_sweeps.jsonl"
            preview_liquidity_sweeps.write_log(payload, scanner.load_config(None), path)
            text = path.read_text(encoding="utf-8")
        record = json.loads(text)
        self.assertEqual(record["symbol"], "AAPL")
        self.assertFalse(record["can_approve_trades"])
        self.assertNotIn("ALPACA_SECRET_KEY", text)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", text)

    def test_preview_json_uses_market_data_only(self) -> None:
        bars = candles({"o": 100.95, "h": 101.12, "l": 100.65, "c": 100.72})

        class Provider:
            def get_recent_bars(self, symbols, start, end):
                return {"AAPL": bars}

            def get_daily_bars(self, symbols, start, end):
                return {"AAPL": []}

        output = io.StringIO()
        with (
            patch.object(preview_liquidity_sweeps.scanner, "make_provider", return_value=Provider()),
            patch.object(preview_liquidity_sweeps.scanner, "load_dotenv"),
            patch.object(sys, "argv", ["preview_liquidity_sweeps.py", "--symbol", "AAPL", "--json", "--no-log"]),
            redirect_stdout(output),
        ):
            self.assertEqual(preview_liquidity_sweeps.main(), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["context_only"])
        self.assertFalse(payload["can_approve_trades"])


class LiquiditySweepTelegramTests(unittest.TestCase):
    def payload(self, status: str, direction: str = "ABOVE_LEVEL") -> dict:
        above = direction == "ABOVE_LEVEL"
        return {
            "symbol": "AAPL",
            "sweep_status": status,
            "sweep_direction": direction,
            "trap_bias": "BEARISH" if above else "BULLISH",
            "sweep_level": 101.0 if above else 100.0,
            "sweep_zone_low": 100.8 if above else 100.0,
            "sweep_zone_high": 101.0 if above else 100.2,
            "level_source": "5m_supply" if above else "5m_demand",
            "timeframe": "5m",
            "score": 85,
            "confidence": "HIGH",
            "meaning": "Buyers may be trapped above the level." if above else "Sellers may be trapped below the level.",
            "wait_for": "Failed reclaim or lower high." if above else "Reclaim hold or higher low.",
            "invalidation": "Clean hold above the zone." if above else "Clean loss below the zone.",
            "inside_chop_range": False,
            "context_only": True,
            "can_approve_trades": False,
        }

    def test_watch_and_forming_messages_are_concise_context_only(self) -> None:
        watch_payload = self.payload("SWEEP_WATCH")
        watch_payload["market_structure_summary"] = "AAPL is between 5m demand near 100.20 and 5m supply near 101.00."
        watch = format_liquidity_sweep_message(watch_payload)
        forming = format_liquidity_sweep_message(self.payload("SWEEP_FORMING", "BELOW_LEVEL"))
        self.assertIn("AAPL SWEEP WATCH", watch)
        self.assertIn("Watch:", watch)
        self.assertIn("Structure:", watch)
        self.assertIn("AAPL SWEEP FORMING", forming)
        self.assertIn("Wait for:", forming)
        for message in (watch, forming):
            actionable = message.replace(DISCLAIMER, "").lower()
            self.assertNotRegex(actionable, r"\bbuy\b|\bsell\b|\benter\b|\btake trade\b")
            self.assertIn(DISCLAIMER, message)

    def test_confirmed_upside_and_downside_messages(self) -> None:
        upside = format_liquidity_sweep_message(self.payload("SWEEP_CONFIRMED"))
        downside = format_liquidity_sweep_message(self.payload("SWEEP_CONFIRMED", "BELOW_LEVEL"))
        self.assertIn("LIQUIDITY SWEEP ABOVE SUPPLY", upside)
        self.assertIn("Buyer trap risk", upside)
        self.assertIn("100.80-101.00", upside)
        self.assertIn("LIQUIDITY SWEEP BELOW DEMAND", downside)
        self.assertIn("Seller trap risk", downside)
        self.assertIn("100.00-100.20", downside)

    def test_duplicate_is_blocked_and_confirmed_bypasses_watch_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sweep-dedupe.json"
            now = datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc)
            watch = self.payload("SWEEP_WATCH")
            confirmed = self.payload("SWEEP_CONFIRMED")
            self.assertTrue(claim_sweep_delivery(watch, 10, path, now=now)[0])
            self.assertFalse(claim_sweep_delivery(watch, 10, path, now=now + timedelta(minutes=1))[0])
            self.assertTrue(claim_sweep_delivery(confirmed, 10, path, now=now + timedelta(minutes=1))[0])

    def test_openai_style_change_to_numeric_zone_is_rejected_and_rule_fallback_validates(self) -> None:
        payload = self.payload("SWEEP_CONFIRMED")
        rule = format_liquidity_sweep_message(payload)
        changed = rule.replace("100.80-101.00", "100.70-101.10")
        self.assertFalse(validate_liquidity_sweep_message(payload, changed, rule_message=rule)[0])
        self.assertTrue(validate_liquidity_sweep_message(payload, rule, rule_message=rule)[0])
        selected, metadata = select_liquidity_sweep_message(payload, formatted_message=changed)
        self.assertEqual(selected, rule)
        self.assertTrue(metadata["fallback_used"])
        self.assertFalse(metadata["openai_validation_passed"])

    def test_telegram_flags_and_context_only_safety(self) -> None:
        config = scanner.load_config(None)
        payload = self.payload("SWEEP_CONFIRMED")
        allowed, _, alert_type = sweep_telegram_eligibility(payload, config)
        self.assertTrue(allowed)
        self.assertEqual(alert_type, "LIQUIDITY_SWEEP_CONFIRMED")
        config["liquidity_sweep_engine"]["telegram_enabled"] = False
        self.assertFalse(sweep_telegram_eligibility(payload, config)[0])
        self.assertFalse(payload["can_approve_trades"])
        self.assertFalse(config["liquidity_sweep_engine"]["can_upgrade"])

    def test_scanner_routes_eligible_sweep_to_existing_telegram_destination_only(self) -> None:
        config = scanner.load_config(None)
        config["notifications"]["telegram_enabled"] = True
        instance = object.__new__(scanner.EliteScanner)
        instance.config = config
        bar = scanner.Bar(t=START, o=100.0, h=101.1, l=99.9, c=100.7, v=1600)
        snap = scanner.SymbolSnapshot(symbol="AAPL", latest_bar=bar, recent_bars=[bar])
        with (
            patch("tools.preview_liquidity_sweeps.build_liquidity_sweep_preview", return_value=self.payload("SWEEP_CONFIRMED")),
            patch.object(scanner, "claim_sweep_delivery", return_value=(True, "first sweep alert", None)),
            patch.object(scanner, "send_telegram_message", return_value=(True, "")) as send,
            patch.object(scanner, "append_sweep_telegram_log") as log,
        ):
            self.assertTrue(instance.process_liquidity_sweep_telegram(snap))
        self.assertEqual(instance.latest_liquidity_sweep_context["AAPL"]["sweep_status"], "SWEEP_CONFIRMED")
        self.assertEqual(send.call_args.kwargs["alert_type"], "LIQUIDITY_SWEEP_CONFIRMED")
        self.assertEqual(send.call_args.kwargs["alert_source"], "LIQUIDITY_SWEEP_ENGINE")
        self.assertFalse(send.call_args.kwargs["sms_sent"])
        self.assertTrue(log.call_args.kwargs["telegram_sent"])

    def test_review_summary_includes_liquidity_sweep_counts(self) -> None:
        summary = export_review_package.build_review_summary(
            day_text="2026-06-10",
            start_text="09:30",
            end_text="16:00",
            alert_window=[{
                "timestamp": START.isoformat(),
                "symbol": "AAPL",
                "direction": "BULLISH",
                "downgraded_by_liquidity_sweep": True,
                "liquidity_sweep_downgrade_reason": "Bullish chase is risky near supply.",
                "liquidity_sweep_source": "engine",
            }],
            scenario_window=[],
            heads_up_window=[],
            option_window=[],
            market_data_records=[],
            notes=[],
            chop_records=[{"sweep_risk_active": True}],
            liquidity_sweep_records=[
                {"sweep_status": "SWEEP_WATCH", "level_source": "5m_supply", "inside_chop_range": True},
                {
                    "sweep_status": "SWEEP_CONFIRMED",
                    "sweep_direction": "ABOVE_LEVEL",
                    "level_source": "hod",
                    "telegram_sent": True,
                },
            ],
        )
        self.assertIn("Sweep watch records: 1", summary)
        self.assertIn("Sweep confirmed records: 1", summary)
        self.assertIn("Alerts downgraded by liquidity sweep: 1", summary)
        self.assertIn("Chop records strengthened by sweep risk: 1", summary)
        self.assertIn("Engine-based strategy sweep records: 1", summary)
        self.assertIn("Legacy fallback strategy sweep records: 0", summary)


if __name__ == "__main__":
    unittest.main()
