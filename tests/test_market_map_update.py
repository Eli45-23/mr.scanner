from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import elite_momentum_scanner as scanner
from scanner.market_map_update import (
    build_market_map_payload,
    format_market_map_message,
    mark_market_map_update_sent,
    market_map_interval_key,
    should_send_market_map_update,
    validate_market_map_message,
)


ET = ZoneInfo("America/New_York")
CONFIG = {
    "market_map_update": {
        "enabled": True,
        "telegram_enabled": True,
        "symbol": "AAPL",
        "interval_minutes": 10,
        "send_during_regular_hours_only": True,
        "send_once_per_interval": True,
        "max_chars": 1200,
    }
}


def sample_payload():
    return build_market_map_payload(
        "AAPL",
        292.0,
        {"pmh": 292.4, "pml": 289.8, "pdh": 294.1, "pdl": 289.25, "pdc": 291.6},
        market_structure={
            "market_structure_bias": "MIXED",
            "structure_quality": "HIGH",
            "structure_warning": "inside chop range",
            "chop_range_detected": True,
            "major_support_area": {"price": 290.1},
            "major_resistance_area": {"price": 292.4},
            "major_demand_area": {"zone_low": 290.05, "zone_high": 290.26},
            "major_supply_area": {"zone_low": 292.2, "zone_high": 292.45},
        },
        liquidity_sweep={
            "nearest_upside_sweep_zone": {"sweep_zone_low": 292.2, "sweep_zone_high": 292.45},
            "nearest_downside_sweep_zone": {"sweep_level": 290.05},
        },
        market_context={"alignment": "ALIGNED"},
        option_context={"quality": "TRADABLE"},
    )


def test_sends_after_interval_elapsed(tmp_path: Path):
    now = datetime(2026, 6, 11, 10, 10, tzinfo=ET)
    allowed, _ = should_send_market_map_update(now, CONFIG, tmp_path / "state.json")
    assert allowed


def test_does_not_send_before_interval_elapsed(tmp_path: Path):
    path = tmp_path / "state.json"
    first = datetime(2026, 6, 11, 10, 10, tzinfo=ET)
    mark_market_map_update_sent(path, market_map_interval_key(first, 10))
    allowed, reason = should_send_market_map_update(
        datetime(2026, 6, 11, 10, 19, tzinfo=ET), CONFIG, path
    )
    assert not allowed
    assert "already attempted" in reason


def test_sends_next_interval(tmp_path: Path):
    path = tmp_path / "state.json"
    first = datetime(2026, 6, 11, 10, 10, tzinfo=ET)
    mark_market_map_update_sent(path, market_map_interval_key(first, 10))
    allowed, _ = should_send_market_map_update(
        datetime(2026, 6, 11, 10, 20, tzinfo=ET), CONFIG, path
    )
    assert allowed


def test_regular_hours_only(tmp_path: Path):
    allowed, reason = should_send_market_map_update(
        datetime(2026, 6, 11, 9, 29, tzinfo=ET), CONFIG, tmp_path / "state.json"
    )
    assert not allowed
    assert reason == "outside regular market hours"


def test_message_includes_key_levels_and_disclaimers():
    payload = sample_payload()
    message = format_market_map_message(payload)
    for text in (
        "AAPL Market Map Update",
        "292.40",
        "290.05-290.26",
        "Watch only.",
        "Confirm manually.",
        "Not a buy/sell signal.",
    ):
        assert text in message
    assert validate_market_map_message(payload, message)[0]


def test_missing_levels_are_safe():
    payload = build_market_map_payload("AAPL", None)
    message = format_market_map_message(payload)
    assert "unavailable" in message
    assert validate_market_map_message(payload, message)[0]


def test_forbidden_action_language_fails_validation():
    payload = sample_payload()
    message = format_market_map_message(payload) + "\nEnter now."
    valid, reason = validate_market_map_message(payload, message)
    assert not valid
    assert "forbidden" in reason


def test_missing_disclaimer_fails_validation():
    payload = sample_payload()
    message = format_market_map_message(payload).replace("Confirm manually.", "")
    valid, reason = validate_market_map_message(payload, message)
    assert not valid
    assert "missing required disclaimer" in reason


def test_protected_numeric_level_change_fails_validation():
    payload = sample_payload()
    message = format_market_map_message(payload).replace("292.40", "299.40")
    valid, reason = validate_market_map_message(payload, message)
    assert not valid
    assert "protected numeric" in reason


def test_max_chars_enforced():
    payload = sample_payload()
    message = format_market_map_message(payload, max_chars=300)
    valid, reason = validate_market_map_message(payload, message, max_chars=300)
    assert not valid
    assert "exceeds" in reason


def integration_scanner():
    instance = scanner.EliteScanner.__new__(scanner.EliteScanner)
    instance.config = scanner.load_config(None)
    instance.config["notifications"]["telegram_enabled"] = True
    instance.config["market_map_update"]["enabled"] = True
    instance.config["market_map_update"]["telegram_enabled"] = True
    instance.latest_liquidity_sweep_context = {}
    return instance


def test_scanner_integration_sends_once_per_interval(tmp_path: Path):
    instance = integration_scanner()
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "log.jsonl"
    now = datetime(2026, 6, 11, 10, 10, tzinfo=ET)
    with patch.object(scanner, "send_telegram_message", return_value=(True, "")) as send:
        assert instance.process_market_map_update(None, {}, now=now, state_path=state_path, log_path=log_path)
        assert not instance.process_market_map_update(None, {}, now=now, state_path=state_path, log_path=log_path)
    assert send.call_count == 1
    assert json.loads(log_path.read_text().splitlines()[0])["sent"] is True


def test_telegram_failure_logged_not_fatal(tmp_path: Path):
    instance = integration_scanner()
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "log.jsonl"
    now = datetime(2026, 6, 11, 10, 10, tzinfo=ET)
    with patch.object(scanner, "send_telegram_message", return_value=(False, "network unavailable")):
        assert not instance.process_market_map_update(None, {}, now=now, state_path=state_path, log_path=log_path)
    record = json.loads(log_path.read_text().splitlines()[0])
    assert record["sent"] is False
    assert "network unavailable" in record["reason"]
    assert json.loads(state_path.read_text())["last_sent_interval"] == market_map_interval_key(now, 10)


def test_market_map_env_overrides():
    with patch.dict(
        "os.environ",
        {
            "ENABLE_MARKET_MAP_UPDATE": "false",
            "MARKET_MAP_UPDATE_INTERVAL_MINUTES": "15",
            "MARKET_MAP_TELEGRAM_ENABLED": "false",
            "MARKET_MAP_MAX_CHARS": "1000",
        },
        clear=False,
    ):
        config = scanner.load_config(None)["market_map_update"]
    assert config["enabled"] is False
    assert config["interval_minutes"] == 15
    assert config["telegram_enabled"] is False
    assert config["max_chars"] == 1000
