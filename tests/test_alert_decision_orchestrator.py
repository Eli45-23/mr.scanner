from __future__ import annotations

import unittest
from datetime import datetime, timezone

import elite_momentum_scanner as scanner
from scanner.alert_decision_orchestrator import format_orchestrator_summary, orchestrate_alert_decision


class AlertDecisionOrchestratorTests(unittest.TestCase):
    def config(self) -> dict:
        return scanner.load_config(None)

    def context(self, direction: str = "BEARISH", **overrides) -> dict:
        bearish = direction == "BEARISH"
        payload = {
            "symbol": "AAPL",
            "price": 99.0 if bearish else 101.0,
            "direction": direction,
            "strategy_direction": direction,
            "scenario_direction": direction,
            "strategy_confidence_score": 88,
            "setup_score": 88,
            "stock_setup_score": 88,
            "scenario_score": 84,
            "strategy_results": [{"active": True, "direction": direction.lower(), "score": 88}],
            "trend_1m": direction,
            "trend_5m": direction,
            "trend_15m": direction,
            "current_structure_bias": direction,
            "vwap": 100.0,
            "ema9": 100.0,
            "option_tradable": True,
            "option_quality": "TRADABLE",
            "market_alignment": "ALIGNED",
            "chop_mode_active": True,
            "sweep_risk_active": True,
            "liquidity_sweep_context": "Sweep risk is active near the range edge.",
            "mixed_signal_detected": False,
            "scenario_conflict": False,
            "do_not_chase": False,
            "existing_trade_ready": False,
            "existing_user_alert": True,
            "scenario_stage": "CONFIRMED",
            "market_structure_warning": "",
            "invalidation_reason": "Reclaim and hold above VWAP/EMA9.",
        }
        payload.update(overrides)
        return payload

    def test_strong_bearish_and_bullish_trend_pass_chop_as_watch_only(self) -> None:
        for direction in ("BEARISH", "BULLISH"):
            decision = orchestrate_alert_decision(self.context(direction), self.config())
            self.assertEqual(decision["final_alert_type"], "TREND_CONTEXT")
            self.assertEqual(decision["final_direction"], direction)
            self.assertTrue(decision["telegram_allowed"])
            self.assertTrue(decision["watch_only"])
            self.assertFalse(decision["trade_ready"])
            self.assertFalse(decision["can_approve_trades"])
            self.assertIn("Chop Mode", " ".join(decision["risk_notes"]))

    def test_chop_blocks_weak_mixed_alert(self) -> None:
        decision = orchestrate_alert_decision(
            self.context(trend_1m="BULLISH", trend_5m="BEARISH", trend_15m="NEUTRAL", setup_score=40, mixed_signal_detected=True),
            self.config(),
        )
        self.assertEqual(decision["final_alert_type"], "MIXED_NO_TRADE")
        self.assertFalse(decision["trade_ready"])

    def test_weak_trend_stays_blocked_by_chop(self) -> None:
        decision = orchestrate_alert_decision(
            self.context(
                trend_1m="BULLISH",
                trend_5m="BEARISH",
                trend_15m="NEUTRAL",
                setup_score=40,
                strategy_confidence_score=40,
                scenario_score=40,
                stock_setup_score=40,
                existing_user_alert=False,
            ),
            self.config(),
        )
        self.assertEqual(decision["final_alert_type"], "CHOP_WARNING")
        self.assertFalse(decision["telegram_allowed"])

    def test_sweep_adds_risk_but_does_not_approve_or_erase_trend(self) -> None:
        decision = orchestrate_alert_decision(
            self.context(sweep_trap_bias="BULLISH", liquidity_sweep_context="Downside sweep reclaimed strongly."),
            self.config(),
        )
        self.assertEqual(decision["final_alert_type"], "TREND_CONTEXT")
        self.assertIn("Downside sweep reclaimed strongly", " ".join(decision["risk_notes"]))
        self.assertFalse(decision["can_approve_trades"])

    def test_do_not_chase_is_not_silenced(self) -> None:
        decision = orchestrate_alert_decision(self.context(do_not_chase=True, risk_label="DO_NOT_CHASE"), self.config())
        self.assertEqual(decision["final_alert_type"], "DO_NOT_CHASE")
        self.assertTrue(decision["telegram_allowed"])
        self.assertFalse(decision["trade_ready"])

    def test_untradable_option_blocks_trade_and_trend_context(self) -> None:
        decision = orchestrate_alert_decision(
            self.context(option_tradable=False, option_quality="WIDE_SPREAD", existing_trade_ready=True),
            self.config(),
        )
        self.assertNotEqual(decision["final_alert_type"], "TRADE_QUALITY")
        self.assertFalse(decision["telegram_allowed"])

    def test_existing_trade_quality_not_loosened_and_chop_blocks_it(self) -> None:
        clean = orchestrate_alert_decision(self.context(chop_mode_active=False, existing_trade_ready=True), self.config())
        chopped = orchestrate_alert_decision(self.context(chop_mode_active=True, existing_trade_ready=True), self.config())
        self.assertEqual(clean["final_alert_type"], "TRADE_QUALITY")
        self.assertNotEqual(chopped["final_alert_type"], "TRADE_QUALITY")

    def test_no_clean_edge_and_forming_stage_downgrade_trade_quality_to_trend_context(self) -> None:
        for override in (
            {"market_structure_warning": "no clean edge"},
            {"scenario_stage": "FORMING"},
        ):
            decision = orchestrate_alert_decision(
                self.context(chop_mode_active=False, existing_trade_ready=True, **override),
                self.config(),
            )
            self.assertEqual(decision["final_alert_type"], "TREND_CONTEXT")
            self.assertTrue(decision["watch_only"])
            self.assertFalse(decision["trade_ready"])
            self.assertFalse(decision["can_approve_trades"])

    def test_trend_context_dedupe(self) -> None:
        recent = {"last_trend_context": {"direction": "BEARISH", "timestamp": datetime.now(timezone.utc).isoformat()}}
        decision = orchestrate_alert_decision(self.context(), self.config(), recent)
        self.assertEqual(decision["final_alert_type"], "DASHBOARD_ONLY")
        self.assertFalse(decision["telegram_allowed"])

    def test_meaningful_sweep_event_remains_context_only(self) -> None:
        context = self.context(
            trend_1m="NEUTRAL",
            trend_5m="NEUTRAL",
            trend_15m="NEUTRAL",
            setup_score=45,
            sweep_filter={
                "telegram_filter_allowed": True,
                "telegram_filter_reason": "Confirmed sweep event passed alert filter",
            },
        )
        decision = orchestrate_alert_decision(context, self.config())
        self.assertEqual(decision["final_alert_type"], "SWEEP_EVENT")
        self.assertTrue(decision["context_only"])
        self.assertFalse(decision["trade_ready"])
        self.assertFalse(decision["can_approve_trades"])

    def test_output_contains_required_logging_fields_and_dashboard_context(self) -> None:
        decision = orchestrate_alert_decision(self.context(), self.config())
        for field in (
            "final_alert_type",
            "final_direction",
            "final_priority",
            "telegram_allowed",
            "dashboard_allowed",
            "trade_ready",
            "watch_only",
            "context_only",
            "can_approve_trades",
            "decision_reason",
            "block_reason",
            "suppression_reason",
            "engine_votes",
            "conflicts",
            "risk_notes",
            "what_to_wait_for",
            "invalidation_notes",
            "primary_engine",
            "supporting_engines",
            "blocking_engines",
        ):
            self.assertIn(field, decision)
        self.assertTrue(decision["dashboard_allowed"])

    def test_summary_has_no_forbidden_instruction_wording(self) -> None:
        decision = orchestrate_alert_decision(self.context(), self.config())
        summary = format_orchestrator_summary(decision).lower()
        self.assertNotRegex(summary, r"\bbuy\b|\bsell\b|\benter\b|\bget in\b|\btake trade\b")


if __name__ == "__main__":
    unittest.main()
