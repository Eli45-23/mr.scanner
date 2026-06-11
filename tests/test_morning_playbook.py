from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import elite_momentum_scanner as scanner
from scanner.morning_playbook import (
    build_morning_playbook_payload,
    format_morning_playbook_message,
    mark_morning_playbook_sent,
    should_send_morning_playbook,
    validate_morning_playbook_message,
)


ET = ZoneInfo("America/New_York")
CONFIG = {
    "morning_playbook": {
        "enabled": True,
        "symbol": "AAPL",
        "send_time_et": "09:25",
        "telegram_enabled": True,
        "send_once_per_day": True,
    }
}


def sample_payload():
    return build_morning_playbook_payload(
        "AAPL",
        292.0,
        {"pmh": 292.4, "pml": 289.8, "pdh": 294.1, "pdl": 289.25, "pdc": 291.6},
        market_structure={
            "market_structure_bias": "MIXED",
            "structure_quality": "HIGH",
            "structure_warning": "inside chop range",
            "current_price_location_summary": "AAPL is between demand and supply",
            "major_support_area": {"price": 290.1},
            "major_resistance_area": {"price": 292.4},
            "major_demand_area": {"zone_low": 290.05, "zone_high": 290.26},
            "major_supply_area": {"zone_low": 292.2, "zone_high": 292.45},
        },
        liquidity_sweep={
            "nearest_upside_sweep_zone": {"level": 292.4},
            "nearest_downside_sweep_zone": {"level": 290.05},
            "sweep_status": "SWEEP_WATCH",
            "trap_bias": "BEARISH",
            "confidence": "LOW",
        },
    )


def test_should_send_after_time_on_weekday(tmp_path: Path):
    allowed, _ = should_send_morning_playbook(datetime(2026, 6, 11, 9, 25, tzinfo=ET), CONFIG, tmp_path / "state.json")
    assert allowed


def test_should_not_send_before_time(tmp_path: Path):
    allowed, reason = should_send_morning_playbook(datetime(2026, 6, 11, 9, 24, tzinfo=ET), CONFIG, tmp_path / "state.json")
    assert not allowed
    assert "before" in reason


def test_should_not_send_twice(tmp_path: Path):
    path = tmp_path / "state.json"
    mark_morning_playbook_sent(path, "2026-06-11")
    allowed, reason = should_send_morning_playbook(datetime(2026, 6, 11, 9, 30, tzinfo=ET), CONFIG, path)
    assert not allowed
    assert "already sent" in reason


def test_should_not_send_on_weekend(tmp_path: Path):
    allowed, reason = should_send_morning_playbook(datetime(2026, 6, 13, 9, 30, tzinfo=ET), CONFIG, tmp_path / "state.json")
    assert not allowed
    assert reason == "weekend"


def test_payload_is_context_only_and_cannot_approve():
    payload = sample_payload()
    assert payload["context_only"] is True
    assert payload["can_approve_trades"] is False


def test_format_includes_levels_structure_and_sweeps():
    message = format_morning_playbook_message(sample_payload())
    for text in ("PMH", "PML", "PDH", "PDL", "PDC", "MIXED", "292.40", "290.05"):
        assert text in message


def test_validation_accepts_rule_message():
    payload = sample_payload()
    message = format_morning_playbook_message(payload)
    assert validate_morning_playbook_message(payload, message)[0]


def test_validation_rejects_forbidden_words():
    payload = sample_payload()
    message = format_morning_playbook_message(payload) + "\nEnter now."
    valid, reason = validate_morning_playbook_message(payload, message)
    assert not valid
    assert "forbidden" in reason


def test_validation_rejects_missing_disclaimer():
    payload = sample_payload()
    message = format_morning_playbook_message(payload).replace(payload["disclaimer"], "")
    assert not validate_morning_playbook_message(payload, message)[0]


def test_validation_rejects_changed_numeric_level():
    payload = sample_payload()
    message = format_morning_playbook_message(payload).replace("292.40", "299.40")
    assert not validate_morning_playbook_message(payload, message)[0]


def test_max_chars_enforced():
    payload = sample_payload()
    message = format_morning_playbook_message(payload, max_chars=500)
    assert len(message) <= 500
    assert not validate_morning_playbook_message(payload, message, max_chars=500)[0]


def test_mark_sent_writes_safe_state(tmp_path: Path):
    path = tmp_path / "state.json"
    mark_morning_playbook_sent(path, "2026-06-11")
    assert json.loads(path.read_text())["last_sent_date"] == "2026-06-11"


def integration_scanner():
    instance = scanner.EliteScanner.__new__(scanner.EliteScanner)
    instance.config = scanner.load_config(None)
    instance.config["notifications"]["telegram_enabled"] = True
    instance.config["morning_playbook"]["enabled"] = True
    instance.config["morning_playbook"]["telegram_enabled"] = True
    instance.latest_liquidity_sweep_context = {}
    return instance


def test_scanner_integration_handles_missing_data_and_sends_once(tmp_path: Path):
    instance = integration_scanner()
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "log.jsonl"
    now = datetime(2026, 6, 11, 9, 30, tzinfo=ET)
    with patch.object(scanner, "send_telegram_message", return_value=(True, "")) as send:
        assert instance.process_morning_playbook(None, {}, now=now, state_path=state_path, log_path=log_path)
        assert not instance.process_morning_playbook(None, {}, now=now, state_path=state_path, log_path=log_path)
    assert send.call_count == 1
    assert json.loads(log_path.read_text().splitlines()[0])["sent"] is True


def test_telegram_failure_is_logged_and_not_fatal(tmp_path: Path):
    instance = integration_scanner()
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "log.jsonl"
    now = datetime(2026, 6, 11, 9, 30, tzinfo=ET)
    with patch.object(scanner, "send_telegram_message", return_value=(False, "network unavailable")):
        assert not instance.process_morning_playbook(None, {}, now=now, state_path=state_path, log_path=log_path)
    record = json.loads(log_path.read_text().splitlines()[0])
    assert record["sent"] is False
    assert "network unavailable" in record["reason"]
    assert json.loads(state_path.read_text())["last_sent_date"] == "2026-06-11"


def test_morning_playbook_env_overrides():
    with patch.dict(
        "os.environ",
        {
            "ENABLE_MORNING_PLAYBOOK": "false",
            "MORNING_PLAYBOOK_SEND_TIME_ET": "09:27",
            "MORNING_PLAYBOOK_TELEGRAM_ENABLED": "false",
            "MORNING_PLAYBOOK_MAX_CHARS": "1000",
        },
        clear=False,
    ):
        config = scanner.load_config(None)["morning_playbook"]
    assert config["enabled"] is False
    assert config["send_time_et"] == "09:27"
    assert config["telegram_enabled"] is False
    assert config["max_chars"] == 1000
