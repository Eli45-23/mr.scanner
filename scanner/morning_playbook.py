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
DISCLAIMER = "Not a buy/sell signal."
CONFIRMATION = "Confirm manually."
FORBIDDEN_ACTION_PATTERNS = (
    r"\bbuy\b",
    r"\bsell\b",
    r"\benter\b",
    r"\bget in\b",
    r"\btake (?:this )?trade\b",
)
PROTECTED_NUMERIC_KEYS = (
    "current_price",
    "pmh",
    "pml",
    "pdh",
    "pdl",
    "pdc",
)


def _settings(config: Dict[str, Any]) -> Dict[str, Any]:
    return config.get("morning_playbook", config)


def _date_key(now_et: datetime) -> str:
    return now_et.astimezone(ET).date().isoformat()


def _load_state(state_path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def should_send_morning_playbook(
    now_et: datetime,
    config: Dict[str, Any],
    state_path: Path,
) -> Tuple[bool, str]:
    settings = _settings(config)
    now_et = now_et.astimezone(ET)
    if not settings.get("enabled", True):
        return False, "morning playbook disabled"
    if str(settings.get("symbol") or "AAPL").upper() != "AAPL":
        return False, "AAPL is the only supported morning playbook symbol"
    if settings.get("telegram_enabled", True) is False:
        return False, "morning playbook Telegram disabled"
    if now_et.weekday() >= 5:
        return False, "weekend"
    try:
        send_at = time.fromisoformat(str(settings.get("send_time_et") or "09:25"))
    except ValueError:
        return False, "invalid morning playbook send_time_et"
    if now_et.time().replace(tzinfo=None) < send_at:
        return False, f"before configured send time {send_at.strftime('%H:%M')} ET"
    if settings.get("send_once_per_day", True):
        state = _load_state(Path(state_path))
        if str(state.get("last_sent_date") or "") == _date_key(now_et):
            return False, "morning playbook already sent today"
    return True, "morning playbook eligible"


def _pick(mapping: Optional[Dict[str, Any]], *keys: str) -> Any:
    mapping = mapping or {}
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _zone_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    low = _pick(value, "zone_low", "sweep_zone_low")
    high = _pick(value, "zone_high", "sweep_zone_high")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return {"low": float(low), "high": float(high)}
    return _pick(value, "price", "level", "midpoint", "sweep_level")


def build_morning_playbook_payload(
    symbol: str,
    current_price: Optional[float],
    known_levels: Dict[str, Any],
    market_structure: Optional[Dict[str, Any]] = None,
    liquidity_sweep: Optional[Dict[str, Any]] = None,
    market_context: Optional[Dict[str, Any]] = None,
    option_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    structure = market_structure or {}
    if isinstance(structure.get("summary"), dict):
        structure = structure["summary"]
    sweep = liquidity_sweep or {}
    option_context = option_context or {}
    market_context = market_context or {}
    wait_for = [
        "Clean break and hold above PMH/PDH with SPY/QQQ confirmation.",
        "Clean break below PML/PDL with SPY/QQQ weakness.",
        "Sweep below demand then reclaim, or sweep above supply then fail back below.",
        "Pullback to VWAP/EMA9 that holds or rejects cleanly.",
    ]
    avoid = [
        "Mixed SPY/QQQ context.",
        "Price trapped between supply and demand.",
        "Weak volume or wide option spreads.",
        "Chop Mode active or price too extended from VWAP/EMA9.",
    ]
    if market_context.get("alignment") == "MIXED":
        avoid.insert(0, "Current SPY/QQQ context is mixed.")
    if option_context.get("spread_warning"):
        avoid.insert(0, str(option_context["spread_warning"]))
    return {
        "symbol": str(symbol or "AAPL").upper(),
        "date": datetime.now(ET).date().isoformat(),
        "current_price": current_price,
        "pmh": _pick(known_levels, "pmh", "premarket_high"),
        "pml": _pick(known_levels, "pml", "premarket_low"),
        "pdh": _pick(known_levels, "pdh", "previous_day_high"),
        "pdl": _pick(known_levels, "pdl", "previous_day_low"),
        "pdc": _pick(known_levels, "pdc", "previous_day_close"),
        "market_structure_bias": _pick(structure, "market_structure_bias", "bias"),
        "structure_quality": _pick(structure, "structure_quality", "quality"),
        "structure_warning": _pick(structure, "structure_warning", "warning"),
        "current_price_location_summary": structure.get("current_price_location_summary"),
        "major_support_area": _zone_value(structure.get("major_support_area")),
        "major_resistance_area": _zone_value(structure.get("major_resistance_area")),
        "major_demand_area": _zone_value(structure.get("major_demand_area")),
        "major_supply_area": _zone_value(structure.get("major_supply_area")),
        "nearest_upside_sweep_zone": _zone_value(sweep.get("nearest_upside_sweep_zone")),
        "nearest_downside_sweep_zone": _zone_value(sweep.get("nearest_downside_sweep_zone")),
        "sweep_status": sweep.get("sweep_status"),
        "trap_bias": sweep.get("trap_bias"),
        "sweep_confidence": _pick(sweep, "confidence", "score"),
        "wait_for": wait_for,
        "avoid": avoid,
        "disclaimer": DISCLAIMER,
        "can_approve_trades": False,
        "context_only": True,
    }


def _display(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    if isinstance(value, dict):
        low, high = value.get("low"), value.get("high")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            return f"{float(low):.2f}-{float(high):.2f}"
    return str(value) if value not in (None, "") else "unavailable"


def format_morning_playbook_message(payload: Dict[str, Any], max_chars: int = 1200) -> str:
    sections = [
        "AAPL Morning Playbook",
        (
            "Price:\n"
            f"Current: {_display(payload.get('current_price'))} | PMH: {_display(payload.get('pmh'))} | "
            f"PML: {_display(payload.get('pml'))}\n"
            f"PDH: {_display(payload.get('pdh'))} | PDL: {_display(payload.get('pdl'))} | "
            f"PDC: {_display(payload.get('pdc'))}"
        ),
        (
            "Structure:\n"
            f"Bias: {_display(payload.get('market_structure_bias'))} | Quality: {_display(payload.get('structure_quality'))}\n"
            f"Warning: {_display(payload.get('structure_warning'))}\n"
            f"Location: {_display(payload.get('current_price_location_summary'))}"
        ),
        (
            "Key Areas:\n"
            f"Demand: {_display(payload.get('major_demand_area'))} | Supply: {_display(payload.get('major_supply_area'))}\n"
            f"Support: {_display(payload.get('major_support_area'))} | Resistance: {_display(payload.get('major_resistance_area'))}"
        ),
        (
            "Liquidity Sweep Map:\n"
            f"Upside: {_display(payload.get('nearest_upside_sweep_zone'))} | "
            f"Downside: {_display(payload.get('nearest_downside_sweep_zone'))}\n"
            f"Status: {_display(payload.get('sweep_status'))} | Trap bias: {_display(payload.get('trap_bias'))} | "
            f"Confidence: {_display(payload.get('sweep_confidence'))}"
        ),
        (
            "Plan:\n"
            "Watch only. Confirm manually.\n"
            "Wait for a clean PMH/PDH or PML/PDL hold with SPY/QQQ confirmation, or a clean VWAP/EMA9 hold/rejection.\n"
            "Avoid chasing the first candle. Respect Chop Mode.\n"
            "No clean edge if price stays trapped between demand and supply."
        ),
        f"{DISCLAIMER}\nUse this as a morning map, not a trade alert.",
    ]
    message = "\n\n".join(sections)
    limit = max(300, int(max_chars))
    if len(message) <= limit:
        return message
    compact = "\n\n".join([sections[0], sections[1], sections[2], sections[4], sections[5]])
    return compact if len(compact) <= limit else compact[:limit]


def _protected_numeric_text(payload: Dict[str, Any]) -> set[str]:
    values = {
        f"{float(payload[key]):.2f}"
        for key in PROTECTED_NUMERIC_KEYS
        if isinstance(payload.get(key), (int, float))
    }
    for key in (
        "major_support_area",
        "major_resistance_area",
        "major_demand_area",
        "major_supply_area",
        "nearest_upside_sweep_zone",
        "nearest_downside_sweep_zone",
    ):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            values.add(f"{float(value):.2f}")
        elif isinstance(value, dict):
            for part in ("low", "high"):
                if isinstance(value.get(part), (int, float)):
                    values.add(f"{float(value[part]):.2f}")
    return values


def validate_morning_playbook_message(
    payload: Dict[str, Any],
    message: str,
    max_chars: int = 1200,
) -> Tuple[bool, str]:
    failures = []
    if DISCLAIMER not in message:
        failures.append("missing Not a buy/sell signal disclaimer")
    if CONFIRMATION not in message:
        failures.append("missing Confirm manually language")
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


def mark_morning_playbook_sent(state_path: Path, date_key: str) -> None:
    state_path = Path(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = _load_state(state_path)
        state["last_sent_date"] = str(date_key)
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


def append_morning_playbook_log(
    path: Path,
    payload: Dict[str, Any],
    sent: bool,
    reason: str,
    **formatter_fields: Any,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **payload,
        **formatter_fields,
        "timestamp": datetime.now(ET).isoformat(),
        "sent": bool(sent),
        "reason": str(reason),
        "symbol": "AAPL",
        "context_only": True,
        "can_approve_trades": False,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
