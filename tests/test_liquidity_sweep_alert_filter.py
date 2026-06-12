from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import elite_momentum_scanner as scanner
from scanner.liquidity_sweep_alert_filter import (
    classify_sweep_output,
    detect_repeated_range_sweeps,
    should_send_liquidity_sweep_telegram,
    sweep_zone_bucket,
)


NOW = datetime.now(timezone.utc)


class LiquiditySweepAlertFilterTests(unittest.TestCase):
    def config(self) -> dict:
        return scanner.load_config(None)

    def payload(self, status: str = "SWEEP_CONFIRMED", **overrides) -> dict:
        payload = {
            "symbol": "AAPL",
            "timestamp": NOW.isoformat(),
            "current_price": 292.0,
            "sweep_status": status,
            "sweep_direction": "ABOVE_LEVEL",
            "trap_bias": "BEARISH",
            "sweep_level": 291.67,
            "sweep_zone_low": 291.60,
            "sweep_zone_high": 291.70,
            "level_source": "5m_supply",
            "timeframe": "5m",
            "score": 90,
            "confidence": "HIGH",
            "inside_chop_range": False,
            "dashboard_eligible": True,
            "context_only": True,
            "can_approve_trades": False,
        }
        payload.update(overrides)
        return payload

    def test_non_confirmed_statuses_are_dashboard_only(self) -> None:
        for status in ("SWEEP_WATCH", "SWEEP_FORMING", "SWEEP_FAILED_HELD", "NO_ACTIVE_SWEEP"):
            payload = self.payload(status)
            classification = classify_sweep_output(payload)
            allowed, reason, metadata = should_send_liquidity_sweep_telegram(payload, [], {}, self.config())
            self.assertFalse(allowed)
            self.assertEqual(reason, "dashboard_only_status")
            self.assertEqual(metadata["suppression_type"], "dashboard_only_status")
            self.assertEqual(metadata["dashboard_only_reason"], "dashboard_only_status")
            self.assertTrue(payload["dashboard_eligible"])
            if status != "SWEEP_FORMING":
                self.assertTrue(classification["sweep_map_only"])

    def test_only_confirmed_meaningful_event_can_alert(self) -> None:
        forming = self.payload("SWEEP_FORMING", score=75, confidence="MEDIUM")
        confirmed = self.payload("SWEEP_CONFIRMED", score=85, confidence="HIGH")
        self.assertFalse(should_send_liquidity_sweep_telegram(forming, [], {}, self.config())[0])
        self.assertTrue(should_send_liquidity_sweep_telegram(confirmed, [], {}, self.config())[0])

    def test_low_confidence_and_non_major_level_have_exact_suppression_reasons(self) -> None:
        low = self.payload(score=69)
        one_minute = self.payload(level_source="resistance", timeframe="1m")
        low_allowed, low_reason, low_metadata = should_send_liquidity_sweep_telegram(low, [], {}, self.config())
        minor_allowed, minor_reason, minor_metadata = should_send_liquidity_sweep_telegram(
            one_minute, [], {}, self.config()
        )
        self.assertFalse(low_allowed)
        self.assertEqual(low_reason, "below_confidence")
        self.assertEqual(low_metadata["suppression_type"], "below_confidence")
        self.assertFalse(minor_allowed)
        self.assertEqual(minor_reason, "not_major_level")
        self.assertEqual(minor_metadata["suppression_type"], "not_major_level")

    def test_nearby_levels_share_zone_bucket_and_state_suppresses_second(self) -> None:
        first = self.payload(sweep_level=291.67)
        second = self.payload(sweep_level=291.71)
        self.assertEqual(sweep_zone_bucket(first), sweep_zone_bucket(second))
        bucket = sweep_zone_bucket(first)
        state = {f"{bucket}|CONFIRMED": {"sent_at": NOW.isoformat()}}
        allowed, reason, metadata = should_send_liquidity_sweep_telegram(second, [], state, self.config())
        self.assertFalse(allowed)
        self.assertEqual(reason, "duplicate_cooldown")
        self.assertEqual(metadata["suppression_type"], "duplicate_cooldown")

    def test_repeated_alternating_tight_range_sweeps_are_suppressed(self) -> None:
        recent = [
            self.payload(sweep_level=291.67, sweep_direction="ABOVE_LEVEL", timestamp=(NOW - timedelta(minutes=3)).isoformat()),
            self.payload(sweep_level=291.71, sweep_direction="BELOW_LEVEL", timestamp=(NOW - timedelta(minutes=2)).isoformat()),
            self.payload(sweep_level=291.77, sweep_direction="ABOVE_LEVEL", timestamp=(NOW - timedelta(minutes=1)).isoformat()),
        ]
        repeated = detect_repeated_range_sweeps(recent, now=NOW)
        self.assertTrue(repeated["repeated_range_sweeps"])
        allowed, reason, metadata = should_send_liquidity_sweep_telegram(self.payload(), recent, {}, self.config())
        self.assertFalse(allowed)
        self.assertIn("alternating sweeps", reason)
        self.assertTrue(metadata["repeated_range_sweeps"])

    def test_chop_suppresses_confirmed_sweep_unless_clean_reversal(self) -> None:
        forming = self.payload("SWEEP_FORMING", score=90, confidence="HIGH", inside_chop_range=True)
        confirmed = self.payload("SWEEP_CONFIRMED", score=80, confidence="HIGH", inside_chop_range=True)
        clean_reversal = self.payload(
            "SWEEP_CONFIRMED",
            score=80,
            confidence="HIGH",
            inside_chop_range=True,
            clean_trap_reversal=True,
        )
        self.assertFalse(should_send_liquidity_sweep_telegram(forming, [], {}, self.config())[0])
        allowed, reason, metadata = should_send_liquidity_sweep_telegram(confirmed, [], {}, self.config())
        self.assertFalse(allowed)
        self.assertEqual(reason, "chop_suppressed")
        self.assertEqual(metadata["suppression_type"], "chop_suppressed")
        self.assertTrue(should_send_liquidity_sweep_telegram(clean_reversal, [], {}, self.config())[0])

    def test_state_file_is_read_safely_and_context_cannot_approve(self) -> None:
        payload = self.payload()
        bucket = sweep_zone_bucket(payload)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(json.dumps({f"{bucket}|CONFIRMED": {"sent_at": NOW.isoformat()}}), encoding="utf-8")
            allowed, _, metadata = should_send_liquidity_sweep_telegram(payload, [], path, self.config())
        self.assertFalse(allowed)
        self.assertTrue(metadata["context_only"])
        self.assertFalse(metadata["can_approve_trades"])
        self.assertFalse(self.config()["liquidity_sweep_engine"]["can_upgrade"])


if __name__ == "__main__":
    unittest.main()
