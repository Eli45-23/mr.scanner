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

    def test_watch_and_failed_held_are_dashboard_only(self) -> None:
        for status in ("SWEEP_WATCH", "SWEEP_FAILED_HELD"):
            payload = self.payload(status)
            classification = classify_sweep_output(payload)
            allowed, _, metadata = should_send_liquidity_sweep_telegram(payload, [], {}, self.config())
            self.assertTrue(classification["sweep_map_only"])
            self.assertFalse(allowed)
            self.assertTrue(metadata["map_only"])
            self.assertTrue(payload["dashboard_eligible"])

    def test_forming_and_confirmed_meaningful_events_can_alert(self) -> None:
        forming = self.payload("SWEEP_FORMING", score=75, confidence="MEDIUM")
        confirmed = self.payload("SWEEP_CONFIRMED", score=85, confidence="HIGH")
        self.assertTrue(should_send_liquidity_sweep_telegram(forming, [], {}, self.config())[0])
        self.assertTrue(should_send_liquidity_sweep_telegram(confirmed, [], {}, self.config())[0])

    def test_low_confidence_and_one_minute_only_are_suppressed(self) -> None:
        low = self.payload(confidence="LOW")
        one_minute = self.payload(level_source="resistance", timeframe="1m")
        self.assertFalse(should_send_liquidity_sweep_telegram(low, [], {}, self.config())[0])
        self.assertFalse(should_send_liquidity_sweep_telegram(one_minute, [], {}, self.config())[0])

    def test_nearby_levels_share_zone_bucket_and_state_suppresses_second(self) -> None:
        first = self.payload(sweep_level=291.67)
        second = self.payload(sweep_level=291.71)
        self.assertEqual(sweep_zone_bucket(first), sweep_zone_bucket(second))
        bucket = sweep_zone_bucket(first)
        state = {f"{bucket}|CONFIRMED": {"sent_at": NOW.isoformat()}}
        allowed, reason, metadata = should_send_liquidity_sweep_telegram(second, [], state, self.config())
        self.assertFalse(allowed)
        self.assertIn("same zone bucket", reason)
        self.assertEqual(metadata["suppression_type"], "same_zone_cooldown")

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

    def test_chop_requires_high_confidence_confirmed_event(self) -> None:
        forming = self.payload("SWEEP_FORMING", score=90, confidence="HIGH", inside_chop_range=True)
        confirmed = self.payload("SWEEP_CONFIRMED", score=90, confidence="HIGH", inside_chop_range=True)
        self.assertFalse(should_send_liquidity_sweep_telegram(forming, [], {}, self.config())[0])
        self.assertTrue(should_send_liquidity_sweep_telegram(confirmed, [], {}, self.config())[0])

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
