from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


DEFAULT_ZONE_QUALITY_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "a_plus_min_score": 85,
    "a_min_score": 75,
    "b_min_score": 60,
    "weak_below_score": 60,
    "downgrade_too_wide": True,
    "downgrade_already_tapped": True,
    "downgrade_old_zones": True,
    "boost_confluence": True,
    "max_primary_zones_per_type": 3,
}


def _settings(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {**DEFAULT_ZONE_QUALITY_CONFIG, **(config or {})}


def _numeric_values(value: Any) -> List[float]:
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        return [
            float(item)
            for key, item in value.items()
            if isinstance(item, (int, float))
        ]
    if isinstance(value, (list, tuple)):
        return [number for item in value for number in _numeric_values(item)]
    return []


def _zone_bounds(zone: Dict[str, Any]) -> tuple[float, float]:
    price = zone.get("price")
    low = zone.get("precision_zone_low", zone.get("zone_low", price))
    high = zone.get("precision_zone_high", zone.get("zone_high", price))
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return 0.0, 0.0
    return float(min(low, high)), float(max(low, high))


def label_zone_quality(score_detail: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> str:
    settings = _settings(config)
    score = int(score_detail.get("quality_score", 0))
    if score_detail.get("is_too_wide") and settings.get("downgrade_too_wide", True):
        return "Too Wide"
    if score_detail.get("is_stale") and settings.get("downgrade_old_zones", True):
        return "Old Zone"
    if score_detail.get("is_already_tapped") and settings.get("downgrade_already_tapped", True):
        return "Already Tapped"
    if score >= int(settings["a_plus_min_score"]):
        return "A+ Zone"
    if score >= int(settings["a_min_score"]):
        return "A Zone"
    if score >= int(settings["b_min_score"]):
        return "B Zone"
    if int(score_detail.get("times_tested", 0)) == 0:
        return "Untested"
    return "Weak Zone"


def score_zone_quality(
    zone: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = _settings(config)
    context = context or {}
    tests = int(zone.get("times_tested", 0) or 0)
    reaction = int(zone.get("reaction_score", zone.get("score", 50)) or 50)
    volume = int(zone.get("volume_score", 65 if zone.get("volume_confirmed") else 45) or 0)
    freshness = int(zone.get("freshness_score", 100 if zone.get("fresh", tests <= 1) else max(10, 80 - tests * 15)) or 0)
    impulse = int(zone.get("impulse_score", 80 if zone.get("strong_close") else 45) or 0)
    width_bps = float(zone.get("major_width_bps", zone.get("width_bps", 0)) or 0)
    is_too_wide = bool(zone.get("too_wide") or str(zone.get("label") or "") == "Too Wide")
    is_stale = tests >= 4 or freshness <= 25
    is_already_tapped = tests >= 2
    low, high = _zone_bounds(zone)
    center = (low + high) / 2 if low or high else 0.0
    tolerance = max(abs(center) * 0.001, 0.05)
    confluence_values = []
    for key in ("known_levels", "liquidity_sweep_zones", "dynamic_levels"):
        confluence_values.extend(_numeric_values(context.get(key)))
    confluence = sum(1 for value in confluence_values if low - tolerance <= value <= high + tolerance)

    score = round(reaction * 0.30 + volume * 0.15 + freshness * 0.20 + impulse * 0.20 + 15)
    reasons = [
        f"Reaction strength {reaction}",
        f"Freshness {freshness}",
        f"Impulse {impulse}",
    ]
    if volume >= 65:
        reasons.append("Volume confirmed")
    if tests == 0:
        score += 5
        reasons.append("Fresh and untested")
    elif tests >= 2:
        score -= min(20, tests * 5)
        reasons.append(f"Already tapped {tests} times")
    if width_bps and width_bps <= 35:
        score += 5
        reasons.append("Efficient width")
    if is_too_wide:
        score -= 15
        reasons.append("Zone is too wide for precision use")
    if is_stale:
        score -= 12
        reasons.append("Zone is old or repeatedly tested")
    if confluence and settings.get("boost_confluence", True):
        score += min(15, confluence * 5)
        reasons.append(f"Confluence with {confluence} protected level{'s' if confluence != 1 else ''}")
    score = max(0, min(100, int(score)))
    detail = {
        "quality_score": score,
        "quality_reasons": reasons,
        "is_dashboard_primary": False,
        "is_too_wide": is_too_wide,
        "is_stale": is_stale,
        "is_already_tapped": is_already_tapped,
        "times_tested": tests,
        "context_only": True,
        "can_approve_trades": False,
    }
    detail["quality_label"] = label_zone_quality(detail, settings)
    return detail


def rank_zones_by_quality(
    zones: Iterable[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    settings = _settings(config)
    enriched = [{**zone, **score_zone_quality(zone, context, settings)} for zone in zones]
    enriched.sort(
        key=lambda item: (
            -int(item.get("quality_score", 0)),
            bool(item.get("is_too_wide")),
            bool(item.get("is_stale")),
            int(item.get("times_tested", 0)),
        )
    )
    primary_limit = max(0, int(settings.get("max_primary_zones_per_type", 3)))
    for index, item in enumerate(enriched):
        item["is_dashboard_primary"] = bool(
            index < primary_limit
            and item.get("quality_label") not in {"Weak Zone", "Too Wide", "Old Zone", "Already Tapped"}
        )
    return enriched
