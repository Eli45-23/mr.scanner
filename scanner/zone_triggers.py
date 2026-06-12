from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


DEFAULT_ZONE_TRIGGER_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "use_best_reaction_level": True,
    "use_atr_buffer": True,
    "invalidation_atr_fraction": 0.15,
    "min_buffer_cents": 0.05,
    "round_to_tick": True,
}


def _round(value: float, tick_size: float, enabled: bool) -> float:
    if not enabled or tick_size <= 0:
        return round(value, 4)
    return round(round(value / tick_size) * tick_size, 4)


def derive_zone_triggers(
    zone: Dict[str, Any],
    current_price: Optional[float] = None,
    atr: Optional[float] = None,
    tick_size: float = 0.01,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = {**DEFAULT_ZONE_TRIGGER_CONFIG, **(config or {})}
    price = zone.get("price")
    low = zone.get("precision_zone_low", zone.get("zone_low", price))
    high = zone.get("precision_zone_high", zone.get("zone_high", price))
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return {
            "trigger_level": None,
            "rejection_line": None,
            "reclaim_line": None,
            "invalidation_level": None,
            "trigger_confidence": "LOW",
            "trigger_reason": "No clean zone bounds available",
            "context_only": True,
            "can_approve_trades": False,
        }
    low, high = float(min(low, high)), float(max(low, high))
    kind = str(zone.get("zone_type") or zone.get("type") or "").lower()
    if kind == "support":
        kind = "demand"
    if kind == "resistance":
        kind = "supply"
    midpoint = (low + high) / 2
    reaction = zone.get("best_reaction_level")
    trigger = float(reaction) if settings.get("use_best_reaction_level", True) and isinstance(reaction, (int, float)) and low <= reaction <= high else midpoint
    buffer = float(settings.get("min_buffer_cents", 0.05))
    if settings.get("use_atr_buffer", True) and isinstance(atr, (int, float)) and atr > 0:
        buffer = max(buffer, float(atr) * float(settings.get("invalidation_atr_fraction", 0.15)))
    rounded = bool(settings.get("round_to_tick", True))
    if kind == "supply":
        rejection_line = min(high, max(trigger, midpoint))
        reclaim_line = None
        invalidation = high + buffer
        line_reason = "Supply trigger uses reaction area; invalidation is above the zone"
    else:
        rejection_line = None
        reclaim_line = max(low, min(trigger, midpoint))
        invalidation = low - buffer
        line_reason = "Demand trigger uses reaction area; invalidation is below the zone"
    quality = int(zone.get("quality_score", zone.get("score", 0)) or 0)
    confidence = "HIGH" if quality >= 75 else "MEDIUM" if quality >= 55 else "LOW"
    current_distance = abs(float(current_price) - trigger) if isinstance(current_price, (int, float)) else None
    return {
        "trigger_level": _round(trigger, tick_size, rounded),
        "rejection_line": _round(rejection_line, tick_size, rounded) if rejection_line is not None else None,
        "reclaim_line": _round(reclaim_line, tick_size, rounded) if reclaim_line is not None else None,
        "invalidation_level": _round(invalidation, tick_size, rounded),
        "best_reaction_level": _round(float(reaction), tick_size, rounded) if isinstance(reaction, (int, float)) else _round(trigger, tick_size, rounded),
        "distance_from_current_price": round(current_distance, 4) if current_distance is not None else None,
        "trigger_confidence": confidence,
        "trigger_reason": line_reason,
        "context_only": True,
        "can_approve_trades": False,
    }


def derive_triggers_for_zones(
    zones: Iterable[Dict[str, Any]],
    current_price: Optional[float] = None,
    atr: Optional[float] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    return [
        {**zone, **derive_zone_triggers(zone, current_price=current_price, atr=atr, config=config)}
        for zone in zones
    ]
