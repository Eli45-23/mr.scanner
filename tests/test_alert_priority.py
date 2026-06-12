from __future__ import annotations

import unittest
from unittest.mock import patch

import elite_momentum_scanner as scanner
from scanner.alert_priority import classify_alert_priority, validate_priority_decision


class AlertPriorityTests(unittest.TestCase):
    def config(self) -> dict:
        return scanner.load_config(None)

    def payload(self, **overrides) -> dict:
        payload = {
            "symbol": "AAPL",
            "primary_setup": "Clean Breakout",
            "scenario_stage": "CONFIRMED",
            "strategy_confidence_score": 88,
            "existing_user_facing_approved": True,
            "context_only": False,
        }
        payload.update(overrides)
        return payload

    def classify(self, **overrides) -> dict:
        return classify_alert_priority(self.payload(**overrides), self.config())

    def test_clean_bullish_breakout_and_bearish_breakdown_are_tier_one(self) -> None:
        for setup in ("Clean Bullish Breakout", "Clean Bearish Breakdown"):
            result = self.classify(primary_setup=setup)
            self.assertEqual(result["tier"], "TIER_1_TEXT_ALERT")
            self.assertTrue(result["should_send_telegram"])

    def test_support_resistance_and_supply_demand_are_dashboard_only(self) -> None:
        for update_type in ("Support/Resistance Update", "Supply/Demand Update"):
            result = self.classify(update_type=update_type)
            self.assertEqual(result["tier"], "TIER_2_DASHBOARD_ONLY")
            self.assertFalse(result["should_send_telegram"])

    def test_sweep_watch_forming_and_failed_are_dashboard_only(self) -> None:
        for status in ("SWEEP_WATCH", "SWEEP_FORMING", "SWEEP_FAILED_HELD"):
            result = self.classify(liquidity_sweep_status=status, score=90, near_major_level=True)
            self.assertEqual(result["tier"], "TIER_2_DASHBOARD_ONLY")

    def test_confirmed_sweep_threshold_and_major_level_gate(self) -> None:
        weak = self.classify(liquidity_sweep_status="SWEEP_CONFIRMED", score=69, near_major_level=True)
        strong = self.classify(
            liquidity_sweep_status="SWEEP_CONFIRMED",
            score=85,
            level_source="5m_supply",
            telegram_filter_allowed=True,
        )
        self.assertEqual(weak["tier"], "TIER_2_DASHBOARD_ONLY")
        self.assertEqual(strong["tier"], "TIER_1_TEXT_ALERT")

    def test_chop_mixed_and_do_not_chase_are_not_repeated_text_alerts(self) -> None:
        expected = {
            "CHOP MODE": "repeated_chop",
            "MIXED / NO TRADE": "mixed_dashboard_only",
            "DO NOT CHASE": "do_not_chase_dashboard_only",
            "LATE MOVE": "late_move_dashboard_only",
        }
        for conclusion, reason in expected.items():
            result = self.classify(phone_conclusion=conclusion, primary_setup="", chop_activation_first=False)
            self.assertEqual(result["tier"], "TIER_2_DASHBOARD_ONLY")
            self.assertFalse(result["should_send_telegram"])
            self.assertEqual(result["reason"], reason)
            self.assertTrue(result["warning_filter_suppressed"])
            self.assertEqual(result["warning_suppression_reason"], reason)

    def test_first_chop_activation_can_text_once(self) -> None:
        result = self.classify(
            phone_conclusion="CHOP MODE",
            primary_setup="",
            chop_activation_first=True,
            strategy_confidence_score=85,
        )
        self.assertEqual(result["tier"], "TIER_1_TEXT_ALERT")
        self.assertTrue(result["should_send_telegram"])

    def test_clean_confirmed_chop_exit_can_be_tier_one(self) -> None:
        result = self.classify(
            phone_conclusion="CHOP MODE",
            primary_setup="Clean Bullish Breakout",
            chop_exit_clean_confirmation=True,
            strategy_confidence_score=90,
        )
        self.assertEqual(result["tier"], "TIER_1_TEXT_ALERT")
        self.assertTrue(result["should_send_telegram"])
        self.assertIn("exit from Chop Mode", result["reason"])

    def test_do_not_chase_warning_line_does_not_block_separate_tier_one_setup(self) -> None:
        result = self.classify(
            primary_setup="Clean Bullish Breakout",
            phone_conclusion="TRADE QUALITY WATCH",
            do_not_chase_warning=True,
            strategy_confidence_score=90,
        )
        self.assertEqual(result["tier"], "TIER_1_TEXT_ALERT")

    def test_warning_filter_defaults_and_env_overrides_load(self) -> None:
        config = self.config()
        self.assertTrue(config["warning_alert_filter"]["mixed_dashboard_only"])
        self.assertTrue(config["warning_alert_filter"]["do_not_chase_dashboard_only"])
        self.assertTrue(config["warning_alert_filter"]["late_move_dashboard_only"])
        with patch.dict(
            "os.environ",
            {
                "WARNING_ALERT_FILTER_ENABLED": "false",
                "CHOP_ACTIVATION_TEXT_ONCE": "false",
                "MIXED_DASHBOARD_ONLY": "false",
                "DO_NOT_CHASE_DASHBOARD_ONLY": "false",
                "LATE_MOVE_DASHBOARD_ONLY": "false",
            },
        ):
            scanner.apply_strategy_env_config(config)
        self.assertFalse(config["warning_alert_filter"]["enabled"])
        self.assertFalse(config["warning_alert_filter"]["chop_activation_text_once"])
        self.assertFalse(config["warning_alert_filter"]["mixed_dashboard_only"])
        self.assertFalse(config["warning_alert_filter"]["do_not_chase_dashboard_only"])
        self.assertFalse(config["warning_alert_filter"]["late_move_dashboard_only"])

    def test_duplicate_and_low_confidence_noise_are_log_only(self) -> None:
        duplicate = self.classify(dedupe_blocked=True)
        low = self.classify(
            primary_setup="Internal Diagnostic",
            existing_user_facing_approved=False,
            strategy_confidence_score=20,
        )
        self.assertEqual(duplicate["tier"], "TIER_3_LOG_ONLY")
        self.assertEqual(low["tier"], "TIER_3_LOG_ONLY")

    def test_context_only_never_approves_trades_and_tier_two_three_block_telegram(self) -> None:
        result = self.classify(phone_conclusion="CONTEXT ONLY")
        self.assertFalse(result["can_approve_trades"])
        self.assertFalse(result["should_send_telegram"])
        self.assertTrue(validate_priority_decision(result)[0])

    def test_forbidden_wording_fails_validation_but_required_disclaimer_is_allowed(self) -> None:
        result = self.classify()
        result["message"] = "Enter now. Not a buy/sell signal."
        self.assertFalse(validate_priority_decision(result)[0])
        result["message"] = "Watch only. Confirm manually. Not a buy/sell signal."
        self.assertTrue(validate_priority_decision(result)[0])

    def test_priority_system_failure_can_use_safe_dashboard_fallback(self) -> None:
        fallback = {
            "tier": "TIER_2_DASHBOARD_ONLY",
            "should_send_telegram": False,
            "dashboard_only": True,
            "reason": "Priority classifier failed safely",
            "priority_score": 0,
            "can_approve_trades": False,
            "context_only": True,
        }
        self.assertTrue(validate_priority_decision(fallback)[0])

    def test_scanner_priority_failure_downgrades_without_crashing(self) -> None:
        instance = object.__new__(scanner.EliteScanner)
        instance.config = self.config()
        alert = scanner.Alert(
            symbol="AAPL",
            timestamp=scanner.now_utc(),
            category="WATCH AAPL",
            price=200.0,
            watch_allowed=True,
        )
        with patch.object(scanner, "classify_alert_priority", side_effect=RuntimeError("priority unavailable")):
            result = instance.apply_alert_priority(alert)
        self.assertEqual(result["tier"], "TIER_2_DASHBOARD_ONLY")
        self.assertFalse(alert.watch_allowed)
        self.assertFalse(alert.alert_priority_telegram_allowed)
        self.assertFalse(alert.alert_priority_can_approve_trades)

    def test_scanner_gate_preserves_tier_one_and_blocks_tier_two_delivery_flags(self) -> None:
        instance = object.__new__(scanner.EliteScanner)
        instance.config = self.config()
        tier_one = scanner.Alert(
            symbol="AAPL",
            timestamp=scanner.now_utc(),
            category="CLEAN BREAKOUT CONFIRMED",
            price=200.0,
            primary_setup="Clean Bullish Breakout",
            scenario_stage="CONFIRMED",
            strategy_confidence_score=90,
            sms_allowed=True,
        )
        tier_two = scanner.Alert(
            symbol="AAPL",
            timestamp=scanner.now_utc(),
            category="CONTEXT UPDATE",
            price=200.0,
            primary_setup="Support/Resistance Update",
            watch_allowed=True,
        )
        self.assertEqual(instance.apply_alert_priority(tier_one)["tier"], "TIER_1_TEXT_ALERT")
        self.assertTrue(tier_one.sms_allowed)
        self.assertEqual(instance.apply_alert_priority(tier_two)["tier"], "TIER_2_DASHBOARD_ONLY")
        self.assertFalse(tier_two.sms_allowed)
        self.assertFalse(tier_two.watch_allowed)
        self.assertFalse(tier_two.phase3_heads_up_sent)


if __name__ == "__main__":
    unittest.main()
