from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import elite_momentum_scanner as scanner
from scanner.chop_mode_engine import clean_breakout_exits_chop, evaluate_chop_mode
from scanner.missed_clean_entry import detect_missed_clean_entry


NOW = datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc)


def record(minutes: int, direction: str, conclusion: str, stage: str = "FORMING") -> dict:
    return {
        "timestamp": (NOW - timedelta(minutes=minutes)).isoformat(),
        "direction": direction,
        "phone_conclusion": conclusion,
        "stage": stage,
        "setup_name": "Bullish Pullback Holding" if direction == "BULLISH" else "Bearish Pullback Rejecting",
        "score": 85,
    }


class ChopModeTests(unittest.TestCase):
    def test_direction_flips_activate_chop(self) -> None:
        result = evaluate_chop_mode(
            [record(8, "BULLISH", "WATCH ONLY"), record(5, "BEARISH", "WATCH ONLY"), record(2, "BULLISH", "WATCH ONLY")],
            now=NOW,
        )
        self.assertTrue(result["chop_mode_active"])
        self.assertEqual(result["chop_mode_type"], "direction_flip")
        self.assertFalse(result["can_approve_trades"])

    def test_repeated_mixed_activates_chop(self) -> None:
        result = evaluate_chop_mode([record(8, "", "MIXED / NO TRADE"), record(5, "", "MIXED / NO TRADE"), record(2, "", "MIXED / NO TRADE")], now=NOW)
        self.assertEqual(result["chop_mode_type"], "mixed_overload")

    def test_supply_demand_range_activates_chop(self) -> None:
        result = evaluate_chop_mode(
            [],
            {"chop_range_detected": True, "range_low": 290.2, "range_high": 291.4},
            [{"timestamp": NOW.isoformat(), "sweep_status": "SWEEP_WATCH", "inside_chop_range": True}],
            now=NOW,
        )
        self.assertEqual(result["chop_mode_type"], "supply_demand_range")
        self.assertEqual(result["range_low"], 290.2)
        self.assertTrue(result["sweep_risk_active"])
        self.assertIn("fake breaks/sweeps", result["sweep_risk_reason"])

    def test_repeated_sweeps_inside_range_strengthen_chop(self) -> None:
        sweeps = [
            {"timestamp": (NOW - timedelta(minutes=4)).isoformat(), "sweep_status": "SWEEP_CONFIRMED", "inside_chop_range": True},
            {"timestamp": (NOW - timedelta(minutes=1)).isoformat(), "sweep_status": "SWEEP_FORMING", "inside_chop_range": True},
        ]
        result = evaluate_chop_mode([], {}, sweeps, now=NOW)
        self.assertTrue(result["chop_mode_active"])
        self.assertEqual(result["chop_mode_type"], "liquidity_sweep_range")
        self.assertEqual(result["recent_sweep_count"], 2)
        self.assertFalse(result["can_approve_trades"])

    def test_clean_breakout_can_exit_but_not_approve(self) -> None:
        chop = {"chop_mode_active": True, "range_low": 290.2, "range_high": 291.4}
        self.assertTrue(clean_breakout_exits_chop(chop, price=291.6, stage="GOOD_POSITION", option_tradable=True, market_alignment="ALIGNED", mixed_signal=False, structure_warning="breaking level"))
        self.assertFalse(clean_breakout_exits_chop(chop, price=291.6, stage="FORMING", option_tradable=True, market_alignment="ALIGNED", mixed_signal=False, structure_warning="breaking level"))


class MissedCleanEntryTests(unittest.TestCase):
    def test_good_position_then_late_is_missed_entry(self) -> None:
        result = detect_missed_clean_entry(
            [record(5, "BULLISH", "WATCH ONLY", "GOOD_POSITION")],
            setup_name="Bullish Pullback Holding",
            direction="BULLISH",
            current_stage="LATE",
            now=NOW,
        )
        self.assertTrue(result["missed_clean_entry"])
        self.assertIn("clean entry", result["lesson"].lower())
        self.assertFalse(result["can_approve_trades"])

    def test_bearish_missed_entry_supported(self) -> None:
        result = detect_missed_clean_entry(
            [record(4, "BEARISH", "WATCH ONLY", "GOOD_POSITION")],
            setup_name="Bearish Pullback Rejecting",
            direction="BEARISH",
            current_stage="DO_NOT_CHASE",
            now=NOW,
        )
        self.assertTrue(result["missed_clean_entry"])

    def test_nonlate_stage_is_not_missed_entry(self) -> None:
        result = detect_missed_clean_entry(
            [record(4, "BULLISH", "WATCH ONLY", "GOOD_POSITION")],
            setup_name="Bullish Pullback Holding",
            direction="BULLISH",
            current_stage="CONFIRMED",
            now=NOW,
        )
        self.assertFalse(result["missed_clean_entry"])


class ScannerDecisionIntegrationTests(unittest.TestCase):
    def make_scanner(self) -> scanner.EliteScanner:
        app = scanner.EliteScanner.__new__(scanner.EliteScanner)
        app.config = scanner.load_config(None)
        app.decision_history = []
        app.last_chop_warning_at = None
        app.last_missed_entry_alerts = {}
        return app

    def make_alert(self, **changes) -> scanner.Alert:
        values = {
            "symbol": "AAPL",
            "timestamp": NOW,
            "category": "WATCH AAPL BEARISH",
            "price": 291.0,
            "direction": "BEARISH",
            "scenario_direction": "BEARISH",
            "scenario_stage": "CONFIRMED",
            "setup_name": "Bearish Pullback Rejecting",
            "scenario_levels": {"vwap": 290.8, "ema9": 290.9},
            "volume_label": "WEAK",
            "market_alignment": "OPPOSED",
            "sms_allowed": True,
            "watch_allowed": True,
        }
        values.update(changes)
        return scanner.Alert(**values)

    def test_bearish_rejection_above_vwap_and_near_demand_downgrades(self) -> None:
        app = self.make_scanner()
        alert = self.make_alert()
        structure = {"structure_warning": "near demand", "current_price_location_summary": "AAPL is near 5m demand"}
        with patch.object(app, "latest_market_structure", return_value=structure):
            app.apply_market_structure_decision_quality(alert)
        self.assertFalse(alert.sms_allowed)
        self.assertEqual(alert.bearish_confirmation_quality, "WEAK")
        self.assertTrue(alert.bearish_downgraded_by_structure)
        self.assertIn("above VWAP", alert.bearish_confirmation_reason)

    def test_chop_sends_one_warning_then_suppresses_repeat(self) -> None:
        app = self.make_scanner()
        structure = {"chop_range_detected": True, "range_low": 290.2, "range_high": 291.4, "structure_warning": "inside chop range"}
        first = self.make_alert(timestamp=NOW)
        second = self.make_alert(timestamp=NOW + timedelta(minutes=1))
        with patch.object(app, "latest_market_structure", return_value=structure):
            app.apply_market_structure_decision_quality(first)
            app.apply_market_structure_decision_quality(second)
        self.assertTrue(first.chop_warning_sent)
        self.assertTrue(first.phase3_heads_up_sent)
        self.assertTrue(second.suppressed_by_chop)
        self.assertFalse(second.phase3_heads_up_sent)
        self.assertFalse(second.sms_allowed)

    def test_decision_tier_is_not_generic_risk_warning_for_mixed_or_late(self) -> None:
        mixed = self.make_alert(mixed_signal_detected=True, scenario_conflict=True)
        scanner.assign_professional_alert_tier(mixed)
        self.assertEqual(mixed.decision_tier, "MIXED_NO_TRADE")
        late = self.make_alert(scenario_stage="LATE", entry_quality_label="LATE")
        scanner.assign_professional_alert_tier(late)
        self.assertEqual(late.decision_tier, "DO_NOT_CHASE")

    def test_telegram_includes_short_market_structure_context(self) -> None:
        alert = self.make_alert(
            market_structure_summary="AAPL is between 5m demand near 290.26 and 5m supply near 291.40",
            invalidation_level=291.5,
            invalidation_reason="Reclaims supply",
        )
        message = scanner.professional_telegram_message(alert, "PHASE3_HEADS_UP")
        self.assertIn("Structure: AAPL is between 5m demand near 290.26 and 5m supply near 291.40", message)
        self.assertEqual(message.count("5m demand"), 1)

    def test_bullish_chase_near_supply_is_downgraded_by_sweep_risk(self) -> None:
        app = self.make_scanner()
        alert = self.make_alert(
            direction="BULLISH",
            scenario_direction="BULLISH",
            setup_name="Bullish Trend Continuation",
            scenario_levels={},
            volume_label="STRONG",
            market_alignment="ALIGNED",
        )
        structure = {
            "chop_range_detected": True,
            "structure_warning": "near supply inside chop range",
            "current_price_location_summary": "AAPL is trapped between demand and supply",
        }
        sweep = {
            "timestamp": NOW.isoformat(),
            "sweep_status": "SWEEP_WATCH",
            "sweep_direction": "ABOVE_LEVEL",
            "trap_bias": "BEARISH",
            "inside_chop_range": True,
            "nearest_upside_sweep_zone": {"zone_low": 291.4, "zone_high": 291.58},
        }
        with (
            patch.object(app, "latest_market_structure", return_value=structure),
            patch.object(app, "recent_liquidity_sweeps", return_value=[sweep]),
        ):
            app.apply_market_structure_decision_quality(alert)
        self.assertTrue(alert.downgraded_by_liquidity_sweep)
        self.assertEqual(alert.risk_label, "DO_NOT_CHASE")
        self.assertFalse(alert.sms_allowed)
        self.assertIn("near supply", alert.liquidity_sweep_downgrade_reason)

    def test_confirmed_downside_sweep_downgrades_bearish_continuation_only(self) -> None:
        app = self.make_scanner()
        alert = self.make_alert(
            setup_name="Bearish Trend Continuation",
            scenario_levels={},
            volume_label="STRONG",
            market_alignment="ALIGNED",
        )
        sweep = {
            "timestamp": NOW.isoformat(),
            "sweep_status": "SWEEP_CONFIRMED",
            "sweep_direction": "BELOW_LEVEL",
            "trap_bias": "BULLISH",
            "reason": "Price reclaimed demand after sweeping below.",
        }
        with (
            patch.object(app, "latest_market_structure", return_value={}),
            patch.object(app, "recent_liquidity_sweeps", return_value=[sweep]),
        ):
            app.apply_market_structure_decision_quality(alert)
        self.assertTrue(alert.downgraded_by_liquidity_sweep)
        self.assertEqual(alert.sweep_trap_bias, "BULLISH")
        self.assertFalse(alert.sms_allowed)
        self.assertIn("requires loss and hold", alert.liquidity_sweep_downgrade_reason)

    def test_bearish_chase_near_demand_is_downgraded_by_sweep_risk(self) -> None:
        app = self.make_scanner()
        alert = self.make_alert(
            setup_name="Bearish Pullback Rejecting",
            scenario_levels={},
            volume_label="STRONG",
            market_alignment="ALIGNED",
        )
        structure = {
            "chop_range_detected": True,
            "structure_warning": "near demand inside chop range",
            "current_price_location_summary": "AAPL is trapped between demand and supply",
        }
        sweep = {
            "timestamp": NOW.isoformat(),
            "sweep_status": "SWEEP_WATCH",
            "sweep_direction": "BELOW_LEVEL",
            "trap_bias": "BULLISH",
            "inside_chop_range": True,
            "nearest_downside_sweep_zone": {"zone_low": 290.05, "zone_high": 290.26},
        }
        with (
            patch.object(app, "latest_market_structure", return_value=structure),
            patch.object(app, "recent_liquidity_sweeps", return_value=[sweep]),
        ):
            app.apply_market_structure_decision_quality(alert)
        self.assertTrue(alert.downgraded_by_liquidity_sweep)
        self.assertFalse(alert.sms_allowed)
        self.assertIn("near demand", alert.liquidity_sweep_downgrade_reason)

    def test_confirmed_upside_sweep_downgrades_bullish_continuation_only(self) -> None:
        app = self.make_scanner()
        alert = self.make_alert(
            direction="BULLISH",
            scenario_direction="BULLISH",
            setup_name="Bullish Trend Continuation",
            scenario_levels={},
            volume_label="STRONG",
            market_alignment="ALIGNED",
        )
        sweep = {
            "timestamp": NOW.isoformat(),
            "sweep_status": "SWEEP_CONFIRMED",
            "sweep_direction": "ABOVE_LEVEL",
            "trap_bias": "BEARISH",
            "reason": "Price rejected after sweeping above supply.",
        }
        with (
            patch.object(app, "latest_market_structure", return_value={}),
            patch.object(app, "recent_liquidity_sweeps", return_value=[sweep]),
        ):
            app.apply_market_structure_decision_quality(alert)
        self.assertTrue(alert.downgraded_by_liquidity_sweep)
        self.assertEqual(alert.sweep_trap_bias, "BEARISH")
        self.assertFalse(alert.sms_allowed)
        self.assertIn("requires reclaim and hold", alert.liquidity_sweep_downgrade_reason)


if __name__ == "__main__":
    unittest.main()
