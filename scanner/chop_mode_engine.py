from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional


def _time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _direction(record: Dict[str, Any]) -> str:
    value = str(record.get("direction") or record.get("scenario_direction") or "").upper()
    return value if value in {"BULLISH", "BEARISH"} else "NEUTRAL"


def evaluate_chop_mode(
    history: Iterable[Dict[str, Any]],
    market_structure: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
    lookback_minutes: int = 15,
    min_flips: int = 2,
    min_mixed_alerts: int = 3,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=lookback_minutes)
    recent = [
        record for record in history
        if (_time(record.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    directions = [_direction(record) for record in recent if _direction(record) != "NEUTRAL"]
    flips = sum(left != right for left, right in zip(directions, directions[1:]))
    conclusions = [
        str(record.get("phone_conclusion") or record.get("decision_label") or "").upper()
        for record in recent
    ]
    mixed_count = sum(value in {"MIXED / NO TRADE", "MIXED_NO_TRADE"} for value in conclusions)
    late_mixed_count = sum(value in {"MIXED / NO TRADE", "MIXED_NO_TRADE", "DO NOT CHASE", "DO_NOT_CHASE"} for value in conclusions)

    structure = market_structure or {}
    summary = structure.get("summary") if isinstance(structure.get("summary"), dict) else structure
    warning = str(summary.get("structure_warning") or summary.get("warning") or "").lower()
    location = str(summary.get("current_price_location_summary") or "").lower()
    range_chop = bool(
        summary.get("chop_range_detected")
        or "inside chop range" in warning
        or ("between 5m demand" in location and "5m supply" in location)
        or "trapped between" in location
    )

    active = False
    chop_type = ""
    reason = ""
    if range_chop:
        active, chop_type = True, "supply_demand_range"
        reason = summary.get("current_price_location_summary") or "Market structure reports price inside a chop range"
    elif mixed_count >= min_mixed_alerts:
        active, chop_type = True, "mixed_overload"
        reason = f"{mixed_count} mixed/no-trade conclusions occurred inside {lookback_minutes} minutes"
    elif flips >= min_flips:
        active, chop_type = True, "direction_flip"
        reason = f"Top scenario direction flipped {flips} times inside {lookback_minutes} minutes"
    elif late_mixed_count >= max(min_mixed_alerts + 1, 4):
        active, chop_type = True, "low_volume_range"
        reason = "Repeated mixed and do-not-chase conclusions indicate no clean edge"

    return {
        "chop_mode_active": active,
        "chop_mode_reason": reason,
        "chop_mode_type": chop_type,
        "range_low": summary.get("range_low"),
        "range_high": summary.get("range_high"),
        "demand_zone": summary.get("major_demand_area") or None,
        "supply_zone": summary.get("major_supply_area") or None,
        "suppression_active": active,
        "suppression_reason": "Repeated noncritical setup alerts are suppressed while chop mode remains active" if active else "",
        "wait_for": "Clean 5m direction, VWAP hold/rejection, and SPY/QQQ alignment.",
        "expires_at": (now + timedelta(minutes=lookback_minutes)).isoformat() if active else None,
        "direction_flips": flips,
        "mixed_alert_count": mixed_count,
        "can_approve_trades": False,
    }


def clean_breakout_exits_chop(
    chop: Dict[str, Any],
    *,
    price: float,
    stage: str,
    option_tradable: bool,
    market_alignment: str,
    mixed_signal: bool,
    structure_warning: str,
) -> bool:
    if not chop.get("chop_mode_active"):
        return True
    low, high = chop.get("range_low"), chop.get("range_high")
    outside = (isinstance(low, (int, float)) and price < low) or (isinstance(high, (int, float)) and price > high)
    return bool(
        outside
        and stage.upper() == "GOOD_POSITION"
        and option_tradable
        and market_alignment.upper() == "ALIGNED"
        and not mixed_signal
        and "chop" not in structure_warning.lower()
    )
