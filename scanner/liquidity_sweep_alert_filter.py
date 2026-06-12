from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


ALERT_FILTER_VERSION = "liquidity-sweep-alert-filter-2.0"
MEANINGFUL_SOURCES = {
    "hod", "lod", "pmh", "pml", "pdh", "pdl",
    "opening_range_high", "opening_range_low",
}


def _settings(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (config or {}).get("liquidity_sweep_engine", {})


def _filter_settings(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (config or {}).get("liquidity_sweep_telegram_filter", {})


def _level(payload: Dict[str, Any]) -> Optional[float]:
    value = payload.get("sweep_level")
    return float(value) if isinstance(value, (int, float)) else None


def _source_importance(payload: Dict[str, Any]) -> str:
    source = str(payload.get("level_source") or "").lower()
    timeframe = str(payload.get("timeframe") or "").lower()
    if source in MEANINGFUL_SOURCES or timeframe == "15m" or source.startswith("15m_"):
        return "HIGH"
    if timeframe == "5m" or source.startswith("5m_") or "supply" in source or "demand" in source:
        return "HIGH"
    if "support" in source or "resistance" in source or "swing_" in source:
        return "LOW" if timeframe == "1m" else "MEDIUM"
    return "LOW"


def sweep_zone_bucket(payload: Dict[str, Any], bucket_bps: float = 12) -> str:
    symbol = str(payload.get("symbol") or "AAPL").upper()
    direction = str(payload.get("sweep_direction") or "NONE").upper()
    bias = str(payload.get("trap_bias") or "NEUTRAL").upper()
    source = str(payload.get("level_source") or "unknown").lower()
    source_group = (
        "supply" if "supply" in source else "demand" if "demand" in source
        else "high" if source in {"hod", "pmh", "pdh", "opening_range_high", "recent_swing_high"} or "resistance" in source
        else "low" if source in {"lod", "pml", "pdl", "opening_range_low", "recent_swing_low"} or "support" in source
        else source
    )
    timeframe = str(payload.get("timeframe") or "mixed").lower()
    timeframe_group = timeframe if timeframe in {"5m", "15m"} else "intraday"
    level = _level(payload)
    if level is None:
        bucket = "none"
    else:
        reference = float(payload.get("current_price") or level)
        increment = max(reference * max(float(bucket_bps), 1.0) / 10000.0, 0.01)
        bucket_low = math.floor(level / increment) * increment
        bucket = f"{bucket_low:.2f}"
    return f"{symbol}|{direction}|{bias}|{source_group}|{timeframe_group}|{bucket}"


def classify_sweep_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    status = str(payload.get("sweep_status") or "NO_ACTIVE_SWEEP").upper()
    event_type = status.removeprefix("SWEEP_") if status.startswith("SWEEP_") else "MAP_ONLY"
    if status == "NO_ACTIVE_SWEEP":
        event_type = "MAP_ONLY"
    importance = _source_importance(payload)
    candidate = status in {"SWEEP_FORMING", "SWEEP_CONFIRMED"}
    return {
        "sweep_map_only": not candidate,
        "event_alert_candidate": candidate,
        "event_type": event_type,
        "importance": importance,
        "reason": "Meaningful sweep event candidate" if candidate else f"{status} remains on the sweep map",
        "can_alert_telegram": candidate and importance in {"MEDIUM", "HIGH"},
    }


def detect_repeated_range_sweeps(
    recent_sweeps: Iterable[Dict[str, Any]],
    lookback_minutes: int = 10,
    max_range_bps: float = 35,
    min_sweeps: int = 3,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max(1, int(lookback_minutes)))
    records = []
    for record in recent_sweeps or []:
        if str(record.get("sweep_status") or "").upper() not in {"SWEEP_FORMING", "SWEEP_CONFIRMED"}:
            continue
        level = _level(record)
        if level is None:
            continue
        raw_time = record.get("event_timestamp") or record.get("timestamp")
        try:
            timestamp = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            timestamp = now
        if timestamp >= cutoff:
            records.append((level, str(record.get("sweep_direction") or "NONE").upper()))
    levels = [item[0] for item in records]
    directions = list(dict.fromkeys(item[1] for item in records))
    low, high = (min(levels), max(levels)) if levels else (None, None)
    range_bps = ((high - low) / max((high + low) / 2, 0.01) * 10000) if low is not None else 0.0
    repeated = len(records) >= min_sweeps and len(directions) > 1 and range_bps <= max_range_bps
    return {
        "repeated_range_sweeps": repeated,
        "range_low": low,
        "range_high": high,
        "sweep_count": len(records),
        "directions": directions,
        "reason": "Repeated alternating sweeps inside tight range" if repeated else "No repeated tight-range sweep loop",
    }


def is_meaningful_sweep_event(
    payload: Dict[str, Any],
    market_structure: Optional[Dict[str, Any]] = None,
    recent_sweeps: Optional[Iterable[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    settings = _settings(config)
    filters = _filter_settings(config)
    status = str(payload.get("sweep_status") or "NO_ACTIVE_SWEEP").upper()
    filter_enabled = bool(filters.get("enabled", True))
    if filter_enabled:
        if filters.get("confirmed_only", True) and status != "SWEEP_CONFIRMED":
            return False, "dashboard_only_status"
        if status == "SWEEP_WATCH" and filters.get("watch_dashboard_only", True):
            return False, "dashboard_only_status"
        if status == "SWEEP_FORMING" and filters.get("forming_dashboard_only", True):
            return False, "dashboard_only_status"
        if status == "SWEEP_FAILED_HELD" and filters.get("failed_held_dashboard_only", True):
            return False, "dashboard_only_status"
    if status in {"SWEEP_WATCH", "NO_ACTIVE_SWEEP"}:
        return False, "dashboard_only_status"
    if status == "SWEEP_FAILED_HELD" and not settings.get("telegram_failed_held_enabled", False):
        return False, "dashboard_only_status"
    if status not in {"SWEEP_FORMING", "SWEEP_CONFIRMED", "SWEEP_FAILED_HELD"}:
        return False, "Sweep status is not an event alert"
    score = int(payload.get("score") or 0)
    if status == "SWEEP_CONFIRMED" and score < int(filters.get("confirmed_min_confidence", 70)):
        return False, "below_confidence"
    threshold = int(
        filters.get("confirmed_min_confidence", 70)
        if status == "SWEEP_CONFIRMED" and filters.get("enabled", True)
        else settings.get("telegram_confirmed_min_score", 80)
        if status == "SWEEP_CONFIRMED"
        else settings.get("telegram_forming_min_score", 70)
    )
    if score < threshold:
        return False, "below_confidence" if status == "SWEEP_CONFIRMED" else f"sweep score {score} below {threshold}"
    confidence = str(payload.get("confidence") or "LOW").upper()
    required = str(settings.get("telegram_confirmed_min_confidence", "HIGH") if status == "SWEEP_CONFIRMED" else settings.get("telegram_min_confidence", "MEDIUM")).upper()
    ranks = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    if ranks.get(confidence, 0) < ranks.get(required, 1):
        return False, "below_confidence" if status == "SWEEP_CONFIRMED" else f"sweep confidence {confidence} below {required}"
    importance = _source_importance(payload)
    if status == "SWEEP_CONFIRMED" and filters.get("require_major_level", True) and importance != "HIGH":
        return False, "not_major_level"
    if settings.get("telegram_require_meaningful_source", True) and importance == "LOW":
        return False, "sweep source lacks meaningful 5m/15m or major-level importance"
    if settings.get("telegram_suppress_1m_only", True) and str(payload.get("timeframe") or "").lower() == "1m" and importance == "LOW":
        return False, "1m-only sweep noise suppressed"
    repeated = detect_repeated_range_sweeps(
        recent_sweeps or [],
        int(settings.get("telegram_repeated_range_lookback_minutes", 10)),
        float(settings.get("telegram_repeated_range_max_bps", 35)),
        int(settings.get("telegram_repeated_range_min_sweeps", 3)),
    )
    if settings.get("telegram_suppress_repeated_range_sweeps", True) and repeated["repeated_range_sweeps"]:
        return False, repeated["reason"]
    inside_chop = bool(payload.get("inside_chop_range"))
    summary = (market_structure or {}).get("summary") if isinstance((market_structure or {}).get("summary"), dict) else market_structure or {}
    inside_chop = inside_chop or bool(summary.get("chop_range_detected"))
    if inside_chop and settings.get("telegram_chop_requires_high_confidence", True):
        clean_reversal = bool(payload.get("clean_trap_reversal")) or (
            status == "SWEEP_CONFIRMED"
            and confidence == "HIGH"
            and score >= int(settings.get("telegram_chop_min_score", 85))
            and str(payload.get("trap_bias") or "NEUTRAL").upper() in {"BULLISH", "BEARISH"}
            and bool(payload.get("current_candle_closed", True))
        )
        if filters.get("suppress_inside_chop_unless_reversal", True) and not clean_reversal:
            return False, "chop_suppressed"
        if not filters.get("suppress_inside_chop_unless_reversal", True) and (
            status != "SWEEP_CONFIRMED" or confidence != "HIGH" or score < int(settings.get("telegram_chop_min_score", 85))
        ):
            return False, "chop_suppressed"
    return True, "meaningful new liquidity sweep event"


def _state_has_recent_bucket(state: Any, bucket: str, cooldown_minutes: int) -> bool:
    if not state:
        return False
    if isinstance(state, (str, Path)):
        try:
            state = json.loads(Path(state).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
    record = None
    if isinstance(state, dict):
        record = state.get(bucket)
        if not isinstance(record, dict):
            record = next((value for key, value in state.items() if str(key).startswith(f"{bucket}|")), None)
    raw = record.get("sent_at") if isinstance(record, dict) else None
    try:
        sent_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - sent_at <= timedelta(minutes=cooldown_minutes)


def should_send_liquidity_sweep_telegram(
    payload: Dict[str, Any],
    recent_sweeps: Optional[Iterable[Dict[str, Any]]],
    state: Any,
    config: Dict[str, Any],
    market_structure: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    settings = _settings(config)
    filters = _filter_settings(config)
    classification = classify_sweep_output(payload)
    bucket = sweep_zone_bucket(payload, float(settings.get("telegram_zone_bucket_bps", 12)))
    repeated = detect_repeated_range_sweeps(
        recent_sweeps or [],
        int(settings.get("telegram_repeated_range_lookback_minutes", 10)),
        float(settings.get("telegram_repeated_range_max_bps", 35)),
        int(settings.get("telegram_repeated_range_min_sweeps", 3)),
    )
    allowed, reason = is_meaningful_sweep_event(payload, market_structure, recent_sweeps, config)
    suppression = ""
    cooldown = int(filters.get("cooldown_minutes", 10))
    if allowed and _state_has_recent_bucket(state, bucket, cooldown):
        allowed, reason, suppression = False, "duplicate_cooldown", "duplicate_cooldown"
    elif not allowed:
        suppression = "repeated_range_sweeps" if repeated["repeated_range_sweeps"] else reason
    metadata = {
        "alert_filter_version": ALERT_FILTER_VERSION,
        "zone_bucket": bucket,
        "map_only": classification["sweep_map_only"],
        "event_alert_candidate": classification["event_alert_candidate"],
        "suppression_type": suppression,
        "repeated_range_sweeps": repeated["repeated_range_sweeps"],
        "range_low": repeated["range_low"],
        "range_high": repeated["range_high"],
        "importance": classification["importance"],
        "dashboard_only_reason": "" if allowed else reason,
        "telegram_alert_reason": reason if allowed else "",
        "telegram_filter_allowed": allowed,
        "telegram_filter_reason": reason,
        "context_only": True,
        "can_approve_trades": False,
    }
    return allowed, reason, metadata
