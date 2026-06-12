from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .market_structure_models import ENGINE_VERSION, clamp_score, normalize_bars, strength, timestamp_text
from .zone_quality import rank_zones_by_quality
from .zone_triggers import derive_triggers_for_zones


DEFAULT_PRECISION_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "max_zone_width_bps": {"1m": 35, "5m": 55, "15m": 85},
    "max_zone_width_atr_multiple": {"1m": 0.8, "5m": 1.0, "15m": 1.25},
    "shrink_wide_wick_zones": True,
    "wick_to_body_shrink_threshold": 1.8,
    "prefer_fresh_zones": True,
    "prefer_impulse_away": True,
    "require_min_reaction_strength": True,
    "min_reaction_score": 55,
    "volume_weight_enabled": True,
    "merge_nearby_zones": True,
    "merge_max_gap_bps": 15,
    "separate_major_and_precision_zones": True,
}


def _avg(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _precision_settings(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    settings = dict(DEFAULT_PRECISION_CONFIG)
    incoming = config or {}
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(settings.get(key), dict):
            settings[key] = {**settings[key], **value}
        else:
            settings[key] = value
    return settings


def _timeframe_value(settings: Dict[str, Any], key: str, timeframe: str, default: float) -> float:
    values = settings.get(key, {})
    if isinstance(values, dict):
        try:
            return float(values.get(timeframe, default))
        except (TypeError, ValueError):
            return default
    return default


def _zone_label(score: int, *, too_wide: bool, tests: int) -> str:
    if too_wide:
        return "Too Wide"
    if tests >= 3:
        return "Old Zone"
    if score >= 90:
        return "A+ Zone"
    if score >= 75:
        return "A Zone"
    if tests == 0:
        return "Untested"
    if tests > 0:
        return "Already Tapped"
    if score >= 55:
        return "B Zone"
    return "Weak Zone"


def _candidate_zone(
    *,
    kind: str,
    timeframe: str,
    bar: Dict[str, Any],
    next_bar: Dict[str, Any],
    index: int,
    bars: List[Dict[str, Any]],
    average_range: float,
    average_volume: float,
    aligns_with_level: bool,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    bar_range = max(bar["h"] - bar["l"], 0.0001)
    body_low, body_high = min(bar["o"], bar["c"]), max(bar["o"], bar["c"])
    body_width = max(body_high - body_low, bar_range * 0.08)
    lower_wick = body_low - bar["l"]
    upper_wick = bar["h"] - body_high
    wick = lower_wick if kind == "demand" else upper_wick
    next_body = abs(next_bar["c"] - next_bar["o"])
    impulse_ratio = next_body / max(average_range, 0.0001)
    volume_ratio = max(bar["v"], next_bar["v"]) / max(average_volume, 1)
    impulse = (next_bar["c"] > next_bar["o"]) if kind == "demand" else (next_bar["c"] < next_bar["o"])
    strong_impulse = impulse and impulse_ratio >= 1.2
    clean_wick = wick / bar_range >= 0.4
    sweep = (
        lower_wick / bar_range >= 0.5 and next_bar["c"] > bar["h"]
        if kind == "demand"
        else upper_wick / bar_range >= 0.5 and next_bar["c"] < bar["l"]
    )
    major_low = bar["l"] if kind == "demand" else body_low
    major_high = body_high if kind == "demand" else bar["h"]
    wick_to_body = wick / max(body_width, 0.0001)
    shrink_wick = bool(
        settings.get("shrink_wide_wick_zones", True)
        and wick_to_body >= float(settings.get("wick_to_body_shrink_threshold", 1.8))
    )
    if shrink_wick:
        precision_low, precision_high = body_low, body_high
        if precision_high - precision_low < bar_range * 0.08:
            midpoint = (body_low + body_high) / 2
            half_width = bar_range * 0.04
            precision_low = max(major_low, midpoint - half_width)
            precision_high = min(major_high, midpoint + half_width)
    else:
        precision_low, precision_high = major_low, major_high

    tests = sum(
        1
        for later in bars[index + 2 :]
        if later["l"] <= precision_high and later["h"] >= precision_low
    )
    reaction_score = clamp_score(
        35
        + min(35, impulse_ratio * 20)
        + (15 if clean_wick else 0)
        + (10 if sweep else 0)
    )
    volume_score = clamp_score(min(100, volume_ratio * 50))
    freshness_score = 100 if tests == 0 else max(20, 80 - tests * 20)
    impulse_score = clamp_score(min(100, impulse_ratio * 55)) if impulse else 20
    quality = 40
    quality += (reaction_score - 50) * 0.35
    quality += (volume_score - 50) * 0.20 if settings.get("volume_weight_enabled", True) else 0
    quality += (freshness_score - 50) * 0.20 if settings.get("prefer_fresh_zones", True) else 0
    quality += (impulse_score - 50) * 0.25 if settings.get("prefer_impulse_away", True) else 0
    quality += 10 if aligns_with_level else 0
    quality += 8 if sweep else 0
    quality_score = clamp_score(quality)
    return {
        "zone_type": kind,
        "timeframe": timeframe,
        "zone_low": precision_low,
        "zone_high": precision_high,
        "precision_zone_low": precision_low,
        "precision_zone_high": precision_high,
        "major_zone_low": major_low,
        "major_zone_high": major_high,
        "body_low": body_low,
        "body_high": body_high,
        "best_reaction_level": body_high if kind == "demand" else body_low,
        "quality_score": quality_score,
        "score": quality_score,
        "reaction_score": reaction_score,
        "volume_score": volume_score,
        "freshness_score": freshness_score,
        "impulse_score": impulse_score,
        "times_tested": tests,
        "last_touched_at": timestamp_text(bar["t"]),
        "last_reaction": (
            "liquidity_sweep_reclaim" if kind == "demand" and sweep
            else "liquidity_sweep_reject" if kind == "supply" and sweep
            else "bullish_impulse" if kind == "demand" and strong_impulse
            else "impulse_down" if kind == "supply" and strong_impulse
            else "bounce" if kind == "demand"
            else "bearish_rejection"
        ),
        "reason": (
            f"{'Strong' if reaction_score >= 70 else 'Moderate'} {kind} reaction; "
            f"reaction {reaction_score}, volume {volume_score}, freshness {freshness_score}, impulse {impulse_score}"
            + ("; wick-heavy area shrunk to candle body" if shrink_wick else "")
        ),
    }


def _merge_zones(
    zones: List[Dict[str, Any]],
    price: float,
    kind: str,
    timeframe: str,
    average_range: float,
    settings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    groups: List[List[Dict[str, Any]]] = []
    max_gap = price * float(settings.get("merge_max_gap_bps", 15)) / 10000.0
    for zone in sorted(zones, key=lambda item: item["precision_zone_low"]):
        group = next(
            (
                items
                for items in groups
                if settings.get("merge_nearby_zones", True)
                and zone["precision_zone_low"] <= max(item["precision_zone_high"] for item in items) + max_gap
                and zone["precision_zone_high"] >= min(item["precision_zone_low"] for item in items) - max_gap
                and abs(
                    (zone["precision_zone_low"] + zone["precision_zone_high"]) / 2
                    - _avg((item["precision_zone_low"] + item["precision_zone_high"]) / 2 for item in items)
                ) <= max(
                    max_gap,
                    max(item["precision_zone_high"] - item["precision_zone_low"] for item in items),
                    zone["precision_zone_high"] - zone["precision_zone_low"],
                )
            ),
            None,
        )
        if group is None:
            groups.append([zone])
        else:
            group.append(zone)

    output: List[Dict[str, Any]] = []
    max_bps = _timeframe_value(settings, "max_zone_width_bps", timeframe, 55)
    max_atr = _timeframe_value(settings, "max_zone_width_atr_multiple", timeframe, 1.0)
    for group in groups:
        best = max(group, key=lambda item: item["quality_score"])
        major_low = min(item["major_zone_low"] for item in group)
        major_high = max(item["major_zone_high"] for item in group)
        precision_low = min(item["precision_zone_low"] for item in group)
        precision_high = max(item["precision_zone_high"] for item in group)
        major_width = major_high - major_low
        precision_width = precision_high - precision_low
        major_width_bps = major_width / max(price, 0.01) * 10000
        atr_multiple = major_width / max(average_range, 0.0001)
        too_wide = major_width_bps > max_bps or atr_multiple > max_atr
        if too_wide and settings.get("separate_major_and_precision_zones", True):
            precision_low = best["body_low"]
            precision_high = best["body_high"]
            precision_width = precision_high - precision_low
        tests = sum(item["times_tested"] for item in group)
        quality_score = clamp_score(max(item["quality_score"] for item in group) + min(8, (len(group) - 1) * 4))
        if too_wide:
            quality_score = clamp_score(quality_score - 8)
        if tests >= 3:
            quality_score = clamp_score(quality_score - 12)
        midpoint = (precision_low + precision_high) / 2
        label = _zone_label(quality_score, too_wide=too_wide, tests=tests)
        output.append(
            {
                "timeframe": timeframe,
                "zone_type": kind,
                "zone_low": round(precision_low, 4),
                "zone_high": round(precision_high, 4),
                "precision_zone_low": round(precision_low, 4),
                "precision_zone_high": round(precision_high, 4),
                "major_zone_low": round(major_low, 4),
                "major_zone_high": round(major_high, 4),
                "midpoint": round(midpoint, 4),
                "width": round(precision_width, 4),
                "width_bps": round(precision_width / max(price, 0.01) * 10000, 2),
                "major_width": round(major_width, 4),
                "major_width_bps": round(major_width_bps, 2),
                "atr_width_multiple": round(atr_multiple, 3) if average_range else None,
                "strength": strength(quality_score),
                "quality_score": quality_score,
                "score": quality_score,
                "reaction_score": max(item["reaction_score"] for item in group),
                "volume_score": max(item["volume_score"] for item in group),
                "freshness_score": max(item["freshness_score"] for item in group),
                "impulse_score": max(item["impulse_score"] for item in group),
                "label": label,
                "fresh": tests == 0,
                "times_tested": tests,
                "last_touched_at": max((item["last_touched_at"] for item in group if item["last_touched_at"]), default=None),
                "last_reaction": best["last_reaction"],
                "best_reaction_level": round(best["best_reaction_level"], 4),
                "distance_from_current_price": round(abs(price - midpoint), 4),
                "reason": best["reason"] + ("; broad major area retained separately" if too_wide else ""),
                "invalidation": f"Clean {'break below' if kind == 'demand' else 'hold above'} {precision_low if kind == 'demand' else precision_high:.2f}",
                "too_wide": too_wide,
                "context_only": True,
                "can_approve_trades": False,
            }
        )
    return output


def detect_supply_demand(
    symbol: str,
    timeframe: str,
    candles: Iterable[Any],
    *,
    current_price: Optional[float] = None,
    known_levels: Optional[Dict[str, Optional[float]]] = None,
    support_resistance: Optional[Dict[str, Any]] = None,
    max_zones: int = 3,
    min_strength: int = 0,
    precision_config: Optional[Dict[str, Any]] = None,
    zone_quality_config: Optional[Dict[str, Any]] = None,
    zone_trigger_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    bars = normalize_bars(candles)
    price = float(current_price or (bars[-1]["c"] if bars else 0))
    settings = _precision_settings(precision_config)
    base = {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "current_price": round(price, 4) if price else None,
        "demand_zones": [],
        "supply_zones": [],
        "nearest_demand_below": {},
        "nearest_supply_above": {},
        "current_price_location": "between_zones",
        "engine_version": ENGINE_VERSION,
        "precision_enabled": bool(settings.get("enabled", True)),
        "context_only": True,
        "can_approve_trades": False,
    }
    if len(bars) < 5 or price <= 0:
        return {**base, "reason": "Not enough candle data to detect supply/demand"}

    average_range = _avg(item["h"] - item["l"] for item in bars[-20:]) or 0.01
    average_volume = _avg(item["v"] for item in bars[-20:] if item["v"] > 0) or 1
    demand: List[Dict[str, Any]] = []
    supply: List[Dict[str, Any]] = []
    sr_supports = [item.get("price") for item in (support_resistance or {}).get("support_levels", [])]
    sr_resistances = [item.get("price") for item in (support_resistance or {}).get("resistance_levels", [])]

    for index in range(1, len(bars) - 1):
        bar, next_bar = bars[index], bars[index + 1]
        bar_range = max(bar["h"] - bar["l"], 0.0001)
        lower_wick = min(bar["o"], bar["c"]) - bar["l"]
        upper_wick = bar["h"] - max(bar["o"], bar["c"])
        next_body = abs(next_bar["c"] - next_bar["o"])
        bullish = (
            next_bar["c"] > next_bar["o"] and next_body >= average_range * 1.2
        ) or (lower_wick / bar_range >= 0.4 and next_bar["c"] > bar["c"])
        bearish = (
            next_bar["c"] < next_bar["o"] and next_body >= average_range * 1.2
        ) or (upper_wick / bar_range >= 0.4 and next_bar["c"] < bar["c"])
        if bullish:
            demand.append(
                _candidate_zone(
                    kind="demand", timeframe=timeframe, bar=bar, next_bar=next_bar, index=index, bars=bars,
                    average_range=average_range, average_volume=average_volume,
                    aligns_with_level=any(level and bar["l"] <= level <= max(bar["o"], bar["c"]) for level in sr_supports),
                    settings=settings,
                )
            )
        if bearish:
            supply.append(
                _candidate_zone(
                    kind="supply", timeframe=timeframe, bar=bar, next_bar=next_bar, index=index, bars=bars,
                    average_range=average_range, average_volume=average_volume,
                    aligns_with_level=any(level and min(bar["o"], bar["c"]) <= level <= bar["h"] for level in sr_resistances),
                    settings=settings,
                )
            )

    demand_zones = _merge_zones(demand, price, "demand", timeframe, average_range, settings)
    supply_zones = _merge_zones(supply, price, "supply", timeframe, average_range, settings)
    if settings.get("require_min_reaction_strength", True):
        minimum = int(settings.get("min_reaction_score", 55))
        demand_zones = [zone for zone in demand_zones if zone["reaction_score"] >= minimum]
        supply_zones = [zone for zone in supply_zones if zone["reaction_score"] >= minimum]
    demand_zones = [zone for zone in demand_zones if zone["score"] >= min_strength]
    supply_zones = [zone for zone in supply_zones if zone["score"] >= min_strength]
    quality_context = {"known_levels": known_levels or {}}
    demand_zones = derive_triggers_for_zones(
        rank_zones_by_quality(demand_zones, quality_context, zone_quality_config),
        current_price=price, atr=average_range, config=zone_trigger_config,
    )[:max_zones]
    supply_zones = derive_triggers_for_zones(
        rank_zones_by_quality(supply_zones, quality_context, zone_quality_config),
        current_price=price, atr=average_range, config=zone_trigger_config,
    )[:max_zones]
    nearest_demand = min((item for item in demand_zones if item["midpoint"] <= price), key=lambda item: price - item["midpoint"], default={})
    nearest_supply = min((item for item in supply_zones if item["midpoint"] >= price), key=lambda item: item["midpoint"] - price, default={})
    location = "between_zones"
    if any(zone["zone_low"] <= price <= zone["zone_high"] for zone in demand_zones):
        location = "inside_demand"
    elif any(zone["zone_low"] <= price <= zone["zone_high"] for zone in supply_zones):
        location = "inside_supply"
    elif nearest_demand and abs(price - nearest_demand["zone_high"]) / price * 100 <= 0.2:
        location = "near_demand"
    elif nearest_supply and abs(nearest_supply["zone_low"] - price) / price * 100 <= 0.2:
        location = "near_supply"
    return {
        **base,
        "demand_zones": demand_zones,
        "supply_zones": supply_zones,
        "nearest_demand_below": nearest_demand,
        "nearest_supply_above": nearest_supply,
        "current_price_location": location,
    }
