from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
WATCH_ONLY = "Watch only."
CONFIRMATION = "Confirm manually."
DISCLAIMER = "Not a buy/sell signal."
FORBIDDEN_ACTION_PATTERNS = (
    r"\bbuy\b",
    r"\bsell\b",
    r"\benter\b",
    r"\bget in\b",
    r"\btake (?:this )?trade\b",
    r"\bguaranteed\b",
)
PROTECTED_NUMERIC_KEYS = (
    "current_price",
    "pmh",
    "pml",
    "pdh",
    "pdl",
    "pdc",
)
PROTECTED_AREA_KEYS = (
    "best_support",
    "best_resistance",
    "best_demand_zone",
    "best_supply_zone",
    "nearest_upside_sweep_zone",
    "nearest_downside_sweep_zone",
)


def _settings(config: Dict[str, Any]) -> Dict[str, Any]:
    return config.get("market_map_update", config)


def _load_state(state_path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _interval_key(now_et: datetime, interval_minutes: int) -> str:
    now_et = now_et.astimezone(ET)
    interval_minutes = max(1, int(interval_minutes))
    minute = (now_et.minute // interval_minutes) * interval_minutes
    return now_et.replace(minute=minute, second=0, microsecond=0).isoformat()


def market_map_interval_key(now_et: datetime, interval_minutes: int) -> str:
    return _interval_key(now_et, interval_minutes)


def should_send_market_map_update(
    now_et: datetime,
    config: Dict[str, Any],
    state_path: Path,
) -> Tuple[bool, str]:
    settings = _settings(config)
    now_et = now_et.astimezone(ET)
    if not settings.get("enabled", True):
        return False, "market map update disabled"
    if settings.get("telegram_enabled", True) is False:
        return False, "market map Telegram disabled"
    if str(settings.get("symbol") or "AAPL").upper() != "AAPL":
        return False, "AAPL is the only supported market map symbol"
    if now_et.weekday() >= 5:
        return False, "weekend"
    if settings.get("send_during_regular_hours_only", True):
        try:
            market_open = time.fromisoformat(str(settings.get("market_open") or "09:30"))
            market_close = time.fromisoformat(str(settings.get("market_close") or "16:00"))
        except ValueError:
            return False, "invalid regular-hours configuration"
        current_time = now_et.time().replace(tzinfo=None)
        if not market_open <= current_time < market_close:
            return False, "outside regular market hours"
    interval = int(settings.get("interval_minutes", 10))
    key = _interval_key(now_et, interval)
    if settings.get("send_once_per_interval", True):
        state = _load_state(Path(state_path))
        if str(state.get("last_sent_interval") or "") == key:
            return False, "market map already attempted this interval"
    return True, f"market map eligible for interval {key}"


def _pick(mapping: Optional[Dict[str, Any]], *keys: str) -> Any:
    mapping = mapping or {}
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _normalize_area(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    low = _pick(value, "low", "zone_low", "sweep_zone_low")
    high = _pick(value, "high", "zone_high", "sweep_zone_high")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return {"low": float(low), "high": float(high)}
    return _pick(value, "price", "level", "midpoint", "sweep_level")


def _market_context_summary(market_context: Dict[str, Any]) -> str:
    direct = _pick(market_context, "summary", "market_context_summary", "alignment")
    if direct:
        return str(direct)
    spy = _pick(market_context, "spy", "SPY", "spy_state")
    qqq = _pick(market_context, "qqq", "QQQ", "qqq_state")
    if spy or qqq:
        return f"SPY: {spy or 'unavailable'} | QQQ: {qqq or 'unavailable'}"
    return "unavailable"


def _option_summary(option_context: Dict[str, Any]) -> str:
    return str(
        _pick(
            option_context,
            "summary",
            "message",
            "option_quality_message",
            "quality",
            "option_quality",
            "feed_status",
        )
        or "unavailable"
    )


def build_market_map_payload(
    symbol: str,
    current_price: Optional[float],
    known_levels: Optional[Dict[str, Any]] = None,
    *,
    market_structure: Optional[Dict[str, Any]] = None,
    liquidity_sweep: Optional[Dict[str, Any]] = None,
    market_context: Optional[Dict[str, Any]] = None,
    option_context: Optional[Dict[str, Any]] = None,
    chop_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    levels = known_levels or {}
    structure = market_structure or {}
    if isinstance(structure.get("summary"), dict):
        structure = structure["summary"]
    sweep = liquidity_sweep or {}
    context = market_context or {}
    option = option_context or {}
    chop = chop_context or {}
    chop_active = bool(
        _pick(chop, "chop_mode_active")
        if "chop_mode_active" in chop
        else _pick(structure, "chop_range_detected")
    )
    chop_reason = _pick(chop, "chop_mode_reason", "suppression_reason")
    if not chop_reason and chop_active:
        chop_reason = _pick(structure, "structure_warning", "warning") or "price is trapped in a range"
    location = _pick(structure, "current_price_location_summary", "location_summary")
    no_clean_edge = chop_active or "chop" in str(_pick(structure, "structure_warning", "warning") or "").lower()
    return {
        "symbol": str(symbol or "AAPL").upper(),
        "timestamp": datetime.now(ET).isoformat(),
        "current_price": current_price,
        "market_structure_bias": _pick(structure, "market_structure_bias", "bias"),
        "structure_quality": _pick(structure, "structure_quality", "quality"),
        "structure_warning": _pick(structure, "structure_warning", "warning"),
        "current_price_location_summary": location,
        "chop_mode_active": chop_active,
        "chop_mode_reason": chop_reason,
        "pmh": _pick(levels, "pmh", "premarket_high"),
        "pml": _pick(levels, "pml", "premarket_low"),
        "pdh": _pick(levels, "pdh", "previous_day_high"),
        "pdl": _pick(levels, "pdl", "previous_day_low"),
        "pdc": _pick(levels, "pdc", "previous_day_close"),
        "best_support": _normalize_area(_pick(structure, "major_support_area", "nearest_support_below")),
        "best_resistance": _normalize_area(_pick(structure, "major_resistance_area", "nearest_resistance_above")),
        "best_demand_zone": _normalize_area(_pick(structure, "major_demand_area", "nearest_demand_below")),
        "best_supply_zone": _normalize_area(_pick(structure, "major_supply_area", "nearest_supply_above")),
        "nearest_upside_sweep_zone": _normalize_area(
            _pick(sweep, "nearest_upside_sweep_zone", "upside_sweep_zone")
        ),
        "nearest_downside_sweep_zone": _normalize_area(
            _pick(sweep, "nearest_downside_sweep_zone", "downside_sweep_zone")
        ),
        "market_context_summary": _market_context_summary(context),
        "option_quality_summary": _option_summary(option),
        "short_plan": (
            "No clean edge if price is trapped between demand and supply."
            if no_clean_edge
            else "Wait for clean confirmation at a key level."
        ),
        "context_only": True,
        "can_approve_trades": False,
    }


def _display(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    if isinstance(value, dict):
        low, high = value.get("low"), value.get("high")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            return f"{float(low):.2f}-{float(high):.2f}"
    return str(value) if value not in (None, "") else "unavailable"


def format_market_map_message(payload: Dict[str, Any], max_chars: int = 1200) -> str:
    sections = [
        "AAPL Market Map Update",
        (
            f"Price: {_display(payload.get('current_price'))}\n"
            f"Structure: {_display(payload.get('market_structure_bias'))} | "
            f"Quality: {_display(payload.get('structure_quality'))}\n"
            f"Chop: {'ACTIVE' if payload.get('chop_mode_active') else 'OFF'} | "
            f"Warning: {_display(payload.get('structure_warning'))}"
        ),
        (
            "Reference Levels:\n"
            f"PMH/PML: {_display(payload.get('pmh'))} / {_display(payload.get('pml'))}\n"
            f"PDH/PDL/PDC: {_display(payload.get('pdh'))} / {_display(payload.get('pdl'))} / "
            f"{_display(payload.get('pdc'))}"
        ),
        (
            "Key Areas:\n"
            f"Support/Resistance: {_display(payload.get('best_support'))} / "
            f"{_display(payload.get('best_resistance'))}\n"
            f"Demand/Supply: {_display(payload.get('best_demand_zone'))} / "
            f"{_display(payload.get('best_supply_zone'))}"
        ),
        (
            "Liquidity Sweep Map:\n"
            f"Upside/Downside: {_display(payload.get('nearest_upside_sweep_zone'))} / "
            f"{_display(payload.get('nearest_downside_sweep_zone'))}"
        ),
        (
            f"Market: {_display(payload.get('market_context_summary'))}\n"
            f"Option: {_display(payload.get('option_quality_summary'))}"
        ),
        f"Plan:\n{payload.get('short_plan') or 'Wait for clean confirmation at a key level.'}",
        f"{WATCH_ONLY}\n{CONFIRMATION}\n{DISCLAIMER}",
    ]
    message = "\n\n".join(sections)
    if len(message) <= int(max_chars):
        return message
    compact = "\n\n".join([sections[0], sections[1], sections[2], sections[3], sections[4], sections[6], sections[7]])
    return compact


def _protected_numeric_text(payload: Dict[str, Any]) -> set[str]:
    values = {
        f"{float(payload[key]):.2f}"
        for key in PROTECTED_NUMERIC_KEYS
        if isinstance(payload.get(key), (int, float))
    }
    for key in PROTECTED_AREA_KEYS:
        value = payload.get(key)
        if isinstance(value, (int, float)):
            values.add(f"{float(value):.2f}")
        elif isinstance(value, dict):
            for part in ("low", "high"):
                if isinstance(value.get(part), (int, float)):
                    values.add(f"{float(value[part]):.2f}")
    return values


def validate_market_map_message(
    payload: Dict[str, Any],
    message: str,
    max_chars: int = 1200,
) -> Tuple[bool, str]:
    failures = []
    for required in (WATCH_ONLY, CONFIRMATION, DISCLAIMER):
        if required not in message:
            failures.append(f"missing required disclaimer: {required}")
    actionable = message.replace(DISCLAIMER, "")
    for pattern in FORBIDDEN_ACTION_PATTERNS:
        if re.search(pattern, actionable, flags=re.IGNORECASE):
            failures.append(f"forbidden action wording: {pattern}")
    missing_numbers = sorted(value for value in _protected_numeric_text(payload) if value not in message)
    if missing_numbers:
        failures.append("changed or omitted protected numeric levels: " + ", ".join(missing_numbers))
    if len(message) > int(max_chars):
        failures.append(f"message exceeds {max_chars} characters")
    if payload.get("can_approve_trades") is not False or payload.get("context_only") is not True:
        failures.append("payload safety flags invalid")
    return not failures, "; ".join(failures)


def mark_market_map_update_sent(state_path: Path, interval_key: str) -> None:
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = _load_state(state_path)
        state["last_sent_interval"] = str(interval_key)
        state["updated_at"] = datetime.now(ET).isoformat()
        fd, temporary = tempfile.mkstemp(prefix=state_path.name, dir=state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
            os.replace(temporary, state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def append_market_map_log(
    path: Path,
    payload: Dict[str, Any],
    sent: bool,
    reason: str,
    **extra_fields: Any,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **payload,
        **extra_fields,
        "timestamp": datetime.now(ET).isoformat(),
        "sent": bool(sent),
        "reason": str(reason),
        "symbol": "AAPL",
        "context_only": True,
        "can_approve_trades": False,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
