from __future__ import annotations

import fcntl
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple


DISCLAIMER = "Heads-up only — confirm manually. Not a buy/sell signal."
IMPORTANT_SOURCE_PARTS = (
    "supply", "demand", "hod", "lod", "pmh", "pml", "pdh", "pdl",
    "resistance", "support", "swing_high", "swing_low",
)
FORBIDDEN_INSTRUCTION_PATTERNS = (
    r"\bbuy\b", r"\bsell\b", r"\benter\b", r"\bget in\b",
    r"\btake (?:this )?trade\b",
)


def _zone_text(payload: Dict[str, Any]) -> str:
    low, high, level = payload.get("sweep_zone_low"), payload.get("sweep_zone_high"), payload.get("sweep_level")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return f"{low:.2f}-{high:.2f}"
    if isinstance(level, (int, float)):
        return f"{level:.2f}"
    return "the level"


def _source_text(payload: Dict[str, Any]) -> str:
    return str(payload.get("level_source") or "key level").replace("_", " ")


def liquidity_sweep_alert_type(payload: Dict[str, Any]) -> Optional[str]:
    return {
        "SWEEP_WATCH": "LIQUIDITY_SWEEP_WATCH",
        "SWEEP_FORMING": "LIQUIDITY_SWEEP_FORMING",
        "SWEEP_CONFIRMED": "LIQUIDITY_SWEEP_CONFIRMED",
    }.get(str(payload.get("sweep_status") or "").upper())


def format_liquidity_sweep_message(payload: Dict[str, Any], max_chars: int = 900) -> str:
    symbol = str(payload.get("symbol") or "AAPL").upper()
    status = str(payload.get("sweep_status") or "NO_ACTIVE_SWEEP").upper()
    direction = str(payload.get("sweep_direction") or "NONE").upper()
    source = _source_text(payload)
    zone = _zone_text(payload)
    above = direction == "ABOVE_LEVEL"
    boundary = payload.get("sweep_zone_high") if above else payload.get("sweep_zone_low")
    boundary_text = f"{float(boundary):.2f}" if isinstance(boundary, (int, float)) else zone
    structure = str(payload.get("market_structure_summary") or "").strip()

    if status == "SWEEP_WATCH":
        title = f"{symbol} SWEEP WATCH — Near {source}"
        why = (
            f"{symbol} is approaching {zone} {source}, where a fake breakout could trap buyers."
            if above else
            f"{symbol} is approaching {zone} {source}, where a fake breakdown could trap sellers."
        )
        watch = "Break above the zone, then close back below." if above else "Break below the zone, then reclaim."
        meaning = "If it fails back below, buyers may be trapped." if above else "If it reclaims, sellers may be trapped."
        invalidation = f"Clean hold {'above' if above else 'below'} {boundary_text}."
        sections = [title, f"Why:\n{why}", f"Watch:\n{watch}", f"Meaning:\n{meaning}", f"Invalidation:\n{invalidation}", DISCLAIMER]
    elif status == "SWEEP_FORMING":
        title = f"{symbol} SWEEP FORMING — {'Above' if above else 'Below'} {source}"
        why = f"Price is pushing {'above' if above else 'below'} {zone}, but the candle has not closed yet."
        risk = (
            "This can become a fake breakout if it closes back below."
            if above else "This can become a fake breakdown if it reclaims."
        )
        sections = [title, f"Why:\n{why}", f"Risk:\n{risk}", "Wait for:\nCandle close.", DISCLAIMER]
    else:
        title = (
            f"{symbol} LIQUIDITY SWEEP ABOVE SUPPLY — Buyer trap risk"
            if above else f"{symbol} LIQUIDITY SWEEP BELOW DEMAND — Seller trap risk"
        )
        why = (
            f"{symbol} broke above {zone} {source}, then closed back below."
            if above else f"{symbol} broke below {zone} {source}, then reclaimed."
        )
        meaning = str(payload.get("meaning") or (
            "Buyers may be trapped above the level." if above else "Sellers may be trapped below the level."
        ))
        wait_for = str(payload.get("wait_for") or (
            "Failed reclaim or lower high." if above else "Reclaim hold or higher low."
        ))
        invalidation = (
            f"Clean hold back above {boundary_text}."
            if above else f"Clean loss back below {boundary_text}."
        )
        sections = [
            title, f"Why:\n{why}", f"Meaning:\n{meaning}",
            f"Wait for:\n{wait_for}", f"Invalidation:\n{invalidation}", DISCLAIMER,
        ]
    if structure:
        sections.insert(-1, f"Structure:\n{structure}")
    return "\n\n".join(sections)[: max(300, int(max_chars))]


def protected_sweep_facts(payload: Dict[str, Any]) -> Dict[str, str]:
    return {
        key: "" if payload.get(key) is None else str(payload.get(key))
        for key in (
            "sweep_level", "sweep_zone_low", "sweep_zone_high", "level_source",
            "sweep_status", "sweep_direction", "trap_bias", "invalidation",
        )
    }


def validate_liquidity_sweep_message(
    payload: Dict[str, Any],
    message: str,
    *,
    rule_message: Optional[str] = None,
    max_chars: int = 900,
) -> Tuple[bool, str]:
    failures = []
    if DISCLAIMER not in message:
        failures.append("missing disclaimer")
    actionable = message.replace(DISCLAIMER, "")
    for pattern in FORBIDDEN_INSTRUCTION_PATTERNS:
        if re.search(pattern, actionable, flags=re.IGNORECASE):
            failures.append(f"forbidden trade instruction: {pattern}")
    if len(message) > max_chars:
        failures.append(f"message exceeds {max_chars} characters")
    locked = protected_sweep_facts(payload)
    reference = rule_message or format_liquidity_sweep_message(payload, max_chars=max_chars)
    for key in ("sweep_status", "sweep_direction", "trap_bias", "level_source"):
        value = locked[key]
        if value and value.lower().replace("_", " ") not in message.lower().replace("_", " ") and value.lower() in reference.lower():
            failures.append(f"changed or omitted protected {key}")
    locked_numbers = set(re.findall(r"\d+(?:\.\d+)?", reference))
    output_numbers = set(re.findall(r"\d+(?:\.\d+)?", message))
    if not locked_numbers.issubset(output_numbers):
        failures.append("changed or omitted protected numeric sweep level/zone")
    return not failures, "; ".join(dict.fromkeys(failures))


def select_liquidity_sweep_message(
    payload: Dict[str, Any],
    *,
    formatted_message: Optional[str] = None,
    max_chars: int = 900,
) -> Tuple[str, Dict[str, Any]]:
    rule_message = format_liquidity_sweep_message(payload, max_chars=max_chars)
    if formatted_message is None:
        return rule_message, {
            "openai_formatter_used": False,
            "openai_validation_passed": False,
            "fallback_used": False,
            "fallback_reason": "",
        }
    valid, reason = validate_liquidity_sweep_message(
        payload,
        formatted_message,
        rule_message=rule_message,
        max_chars=max_chars,
    )
    if valid:
        return formatted_message, {
            "openai_formatter_used": True,
            "openai_validation_passed": True,
            "fallback_used": False,
            "fallback_reason": "",
        }
    return rule_message, {
        "openai_formatter_used": True,
        "openai_validation_passed": False,
        "fallback_used": True,
        "fallback_reason": reason,
    }


def sweep_telegram_eligibility(payload: Dict[str, Any], config: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    settings = config.get("liquidity_sweep_engine", {})
    alert_type = liquidity_sweep_alert_type(payload)
    if not settings.get("telegram_enabled", True):
        return False, "liquidity sweep Telegram disabled", alert_type
    if str(payload.get("symbol") or "").upper() != "AAPL":
        return False, "AAPL is the only alert symbol", alert_type
    if not alert_type:
        return False, "sweep status is not Telegram eligible", alert_type
    status = str(payload.get("sweep_status") or "").upper()
    enabled_key = {
        "SWEEP_WATCH": "telegram_watch_enabled",
        "SWEEP_FORMING": "telegram_forming_enabled",
        "SWEEP_CONFIRMED": "telegram_confirmed_enabled",
    }[status]
    if not settings.get(enabled_key, True):
        return False, f"{status.lower()} Telegram disabled", alert_type
    score = int(payload.get("score") or 0)
    threshold = int(
        settings.get("telegram_confirmed_min_confidence", 65)
        if status == "SWEEP_CONFIRMED" else settings.get("telegram_min_confidence", 55)
    )
    if score < threshold:
        return False, f"sweep score {score} below {threshold}", alert_type
    source = str(payload.get("level_source") or "").lower()
    if not any(part in source for part in IMPORTANT_SOURCE_PARTS):
        return False, "sweep level source is not important enough", alert_type
    if payload.get("inside_chop_range") and status != "SWEEP_CONFIRMED" and score < 75:
        return False, "chop mode allows only high-quality watch/forming sweeps", alert_type
    return True, "important liquidity sweep context", alert_type


def sweep_dedupe_key(payload: Dict[str, Any]) -> str:
    source = str(payload.get("level_source") or "unknown").upper()
    direction = str(payload.get("sweep_direction") or "NONE").upper()
    status = str(payload.get("sweep_status") or "NO_ACTIVE_SWEEP").upper()
    level = payload.get("sweep_level")
    level_text = f"{float(level):.2f}" if isinstance(level, (int, float)) else "none"
    return f"AAPL|{source}|{direction}|{status}|{level_text}"


def claim_sweep_delivery(
    payload: Dict[str, Any],
    cooldown_minutes: int,
    state_path: Path,
    *,
    now: Optional[datetime] = None,
) -> Tuple[bool, str, Optional[str]]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    key = sweep_dedupe_key(payload)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            state = {}
        previous = state.get(key)
        last_sent_at = previous.get("sent_at") if isinstance(previous, dict) else None
        try:
            last_sent = datetime.fromisoformat(str(last_sent_at)) if last_sent_at else None
        except ValueError:
            last_sent = None
        allowed = not last_sent or now - last_sent > timedelta(minutes=cooldown_minutes)
        reason = "first sweep alert" if not last_sent else "sweep cooldown elapsed"
        if not allowed:
            reason = "duplicate sweep alert within cooldown"
        else:
            state[key] = {
                "sent_at": now.isoformat(),
                "fingerprint": hashlib.sha256(format_liquidity_sweep_message(payload).encode("utf-8")).hexdigest(),
            }
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return allowed, reason, last_sent_at


def append_sweep_telegram_log(path: Path, payload: Dict[str, Any], **fields: Any) -> None:
    record = {
        **payload,
        "event_timestamp": payload.get("timestamp"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": "AAPL",
        "context_only": True,
        "can_approve_trades": False,
        **fields,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
