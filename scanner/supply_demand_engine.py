from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .market_structure_models import ENGINE_VERSION, clamp_score, normalize_bars, strength, timestamp_text


def _avg(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _merge_zones(zones: List[Dict[str, Any]], price: float, kind: str) -> List[Dict[str, Any]]:
    groups: List[List[Dict[str, Any]]] = []
    tolerance = price * 0.0008
    for zone in sorted(zones, key=lambda item: item["zone_low"]):
        group = next(
            (
                items
                for items in groups
                if zone["zone_low"] <= max(item["zone_high"] for item in items) + tolerance
                and zone["zone_high"] >= min(item["zone_low"] for item in items) - tolerance
            ),
            None,
        )
        if group is None:
            groups.append([zone])
        else:
            group.append(zone)
    output = []
    for group in groups:
        low = min(item["zone_low"] for item in group)
        high = max(item["zone_high"] for item in group)
        score = max(item["score"] for item in group) + min(10, (len(group) - 1) * 5)
        tests = sum(item["times_tested"] for item in group)
        if tests >= 3:
            score -= 15
        midpoint = (low + high) / 2
        score = clamp_score(score)
        output.append(
            {
                "zone_low": round(low, 4),
                "zone_high": round(high, 4),
                "midpoint": round(midpoint, 4),
                "strength": strength(score),
                "score": score,
                "fresh": tests == 0,
                "times_tested": tests,
                "last_touched_at": max((item["last_touched_at"] for item in group if item["last_touched_at"]), default=None),
                "last_reaction": max(group, key=lambda item: item["score"])["last_reaction"],
                "distance_from_current_price": round(abs(price - midpoint), 4),
                "reason": max(group, key=lambda item: item["score"])["reason"],
                "invalidation": f"Clean {'break below' if kind == 'demand' else 'hold above'} {low if kind == 'demand' else high:.2f}",
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
) -> Dict[str, Any]:
    bars = normalize_bars(candles)
    price = float(current_price or (bars[-1]["c"] if bars else 0))
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
        "context_only": True,
    }
    if len(bars) < 5 or price <= 0:
        return {**base, "reason": "Not enough candle data to detect supply/demand"}

    average_range = _avg(item["h"] - item["l"] for item in bars[-20:]) or 0.01
    average_volume = _avg(item["v"] for item in bars[-20:]) or 1
    demand: List[Dict[str, Any]] = []
    supply: List[Dict[str, Any]] = []
    sr_supports = [item.get("price") for item in (support_resistance or {}).get("support_levels", [])]
    sr_resistances = [item.get("price") for item in (support_resistance or {}).get("resistance_levels", [])]

    for index in range(1, len(bars) - 1):
        bar = bars[index]
        next_bar = bars[index + 1]
        bar_range = max(bar["h"] - bar["l"], 0.0001)
        lower_wick = min(bar["o"], bar["c"]) - bar["l"]
        upper_wick = bar["h"] - max(bar["o"], bar["c"])
        next_body = abs(next_bar["c"] - next_bar["o"])
        volume_confirmed = max(bar["v"], next_bar["v"]) >= average_volume * 1.25
        bullish_impulse = next_bar["c"] > next_bar["o"] and next_body >= average_range * 1.2
        bearish_impulse = next_bar["c"] < next_bar["o"] and next_body >= average_range * 1.2
        bullish_sweep = lower_wick / bar_range >= 0.5 and next_bar["c"] > bar["h"]
        bearish_sweep = upper_wick / bar_range >= 0.5 and next_bar["c"] < bar["l"]

        if bullish_impulse or (lower_wick / bar_range >= 0.4 and next_bar["c"] > bar["c"]):
            low, high = bar["l"], max(bar["o"], bar["c"])
            score = 50 + (20 if bullish_impulse else 0) + (15 if volume_confirmed else -10) + (15 if lower_wick / bar_range >= 0.4 else 0)
            if bullish_sweep:
                score += 10
            if any(level and low <= level <= high for level in sr_supports):
                score += 10
            tests = sum(1 for later in bars[index + 2 :] if later["l"] <= high and later["h"] >= low)
            if tests == 0:
                score += 10
            demand.append(
                {
                    "zone_low": low,
                    "zone_high": high,
                    "score": clamp_score(score),
                    "times_tested": tests,
                    "last_touched_at": timestamp_text(bar["t"]),
                    "last_reaction": "liquidity_sweep_reclaim" if bullish_sweep else ("bullish_impulse" if bullish_impulse else "bounce"),
                    "reason": "Strong bullish reaction from this area" + (" with volume confirmation" if volume_confirmed else " on weak volume"),
                }
            )

        if bearish_impulse or (upper_wick / bar_range >= 0.4 and next_bar["c"] < bar["c"]):
            low, high = min(bar["o"], bar["c"]), bar["h"]
            score = 50 + (20 if bearish_impulse else 0) + (15 if volume_confirmed else -10) + (15 if upper_wick / bar_range >= 0.4 else 0)
            if bearish_sweep:
                score += 10
            if any(level and low <= level <= high for level in sr_resistances):
                score += 10
            tests = sum(1 for later in bars[index + 2 :] if later["l"] <= high and later["h"] >= low)
            if tests == 0:
                score += 10
            supply.append(
                {
                    "zone_low": low,
                    "zone_high": high,
                    "score": clamp_score(score),
                    "times_tested": tests,
                    "last_touched_at": timestamp_text(bar["t"]),
                    "last_reaction": "liquidity_sweep_reject" if bearish_sweep else ("impulse_down" if bearish_impulse else "bearish_rejection"),
                    "reason": "Strong bearish reaction from this area" + (" with volume confirmation" if volume_confirmed else " on weak volume"),
                }
            )

    demand_zones = [zone for zone in _merge_zones(demand, price, "demand") if zone["score"] >= min_strength]
    supply_zones = [zone for zone in _merge_zones(supply, price, "supply") if zone["score"] >= min_strength]
    demand_zones = sorted(demand_zones, key=lambda item: (-item["score"], abs(price - item["midpoint"])))[:max_zones]
    supply_zones = sorted(supply_zones, key=lambda item: (-item["score"], abs(price - item["midpoint"])))[:max_zones]
    nearest_demand = min(
        (item for item in demand_zones if item["midpoint"] <= price),
        key=lambda item: price - item["midpoint"],
        default={},
    )
    nearest_supply = min(
        (item for item in supply_zones if item["midpoint"] >= price),
        key=lambda item: item["midpoint"] - price,
        default={},
    )
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
