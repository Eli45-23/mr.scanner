from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


ENGINE_VERSION = "phase1-1.0"


def bar_value(bar: Any, short: str, long: str) -> Any:
    if isinstance(bar, dict):
        return bar.get(short, bar.get(long))
    return getattr(bar, short, getattr(bar, long, None))


def normalize_bars(bars: Iterable[Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for bar in bars or []:
        try:
            timestamp = bar_value(bar, "t", "timestamp")
            normalized.append(
                {
                    "t": timestamp,
                    "o": float(bar_value(bar, "o", "open")),
                    "h": float(bar_value(bar, "h", "high")),
                    "l": float(bar_value(bar, "l", "low")),
                    "c": float(bar_value(bar, "c", "close")),
                    "v": float(bar_value(bar, "v", "volume") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    return sorted(normalized, key=lambda item: str(item.get("t") or ""))


def timestamp_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def strength(score: float) -> str:
    if score >= 75:
        return "HIGH"
    if score >= 55:
        return "MEDIUM"
    return "LOW"


def clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def resample_bars(bars: Iterable[Any], minutes: int) -> List[Dict[str, Any]]:
    normalized = normalize_bars(bars)
    if minutes <= 1:
        return normalized
    buckets: Dict[Any, List[Dict[str, Any]]] = {}
    for bar in normalized:
        timestamp = bar["t"]
        if not isinstance(timestamp, datetime):
            continue
        base = timestamp.replace(second=0, microsecond=0)
        bucket = base.replace(minute=(base.minute // minutes) * minutes)
        buckets.setdefault(bucket, []).append(bar)
    return [
        {
            "t": timestamp,
            "o": group[0]["o"],
            "h": max(item["h"] for item in group),
            "l": min(item["l"] for item in group),
            "c": group[-1]["c"],
            "v": sum(item["v"] for item in group),
        }
        for timestamp, group in sorted(buckets.items())
    ]


def _nearest(items: List[Dict[str, Any]], field: str, price: float, below: bool) -> Dict[str, Any]:
    eligible = [
        item
        for item in items
        if (float(item[field]) <= price if below else float(item[field]) >= price)
    ]
    if not eligible:
        return {}
    return min(eligible, key=lambda item: abs(float(item[field]) - price))


def _confluence(prices: List[float], current_price: float, tolerance_pct: float = 0.15) -> bool:
    if len(prices) < 2 or current_price <= 0:
        return False
    tolerance = current_price * tolerance_pct / 100.0
    return any(abs(left - right) <= tolerance for index, left in enumerate(prices) for right in prices[index + 1 :])


def combine_market_structure(
    symbol: str,
    support_resistance: Dict[str, Dict[str, Any]],
    supply_demand: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    frames = ("1m", "5m", "15m")
    current_price = next(
        (
            float(result.get("current_price"))
            for frame in frames
            for result in (support_resistance.get(frame, {}), supply_demand.get(frame, {}))
            if result.get("current_price") is not None
        ),
        0.0,
    )
    weights = {"1m": 1, "5m": 2, "15m": 3}

    supports = [
        (frame, item)
        for frame in frames
        for item in support_resistance.get(frame, {}).get("support_levels", [])
    ]
    resistances = [
        (frame, item)
        for frame in frames
        for item in support_resistance.get(frame, {}).get("resistance_levels", [])
    ]
    demands = [
        (frame, item)
        for frame in frames
        for item in supply_demand.get(frame, {}).get("demand_zones", [])
    ]
    supplies = [
        (frame, item)
        for frame in frames
        for item in supply_demand.get(frame, {}).get("supply_zones", [])
    ]

    def best(items: List[tuple[str, Dict[str, Any]]], price_field: str) -> Dict[str, Any]:
        if not items:
            return {}
        frame, item = max(items, key=lambda pair: pair[1].get("score", 0) + weights[pair[0]] * 5)
        return {"timeframe": frame, **item, "price": item.get(price_field)}

    major_support = best(supports, "price")
    major_resistance = best(resistances, "price")
    major_demand = best(demands, "midpoint")
    major_supply = best(supplies, "midpoint")
    support_confluence = _confluence([float(item["price"]) for _, item in supports], current_price)
    resistance_confluence = _confluence([float(item["price"]) for _, item in resistances], current_price)
    demand_confluence = _confluence([float(item["midpoint"]) for _, item in demands], current_price)
    supply_confluence = _confluence([float(item["midpoint"]) for _, item in supplies], current_price)

    five_demand = _nearest(supply_demand.get("5m", {}).get("demand_zones", []), "midpoint", current_price, True)
    five_supply = _nearest(supply_demand.get("5m", {}).get("supply_zones", []), "midpoint", current_price, False)
    range_low = five_demand.get("zone_low")
    range_high = five_supply.get("zone_high")
    chop_range = bool(
        five_demand
        and five_supply
        and float(five_supply["zone_low"]) > float(five_demand["zone_high"])
        and (float(five_supply["zone_low"]) - float(five_demand["zone_high"])) / max(current_price, 0.01) * 100 <= 1.0
    )

    frame_locations = {
        frame: support_resistance.get(frame, {}).get("current_price_location")
        for frame in frames
    }
    if frame_locations.get("5m") in {"breaking_above_resistance", "retesting_old_resistance_as_support"} and frame_locations.get("15m") in {
        "breaking_above_resistance",
        "retesting_old_resistance_as_support",
    }:
        bias = "BULLISH"
    elif frame_locations.get("5m") in {"breaking_below_support", "retesting_old_support_as_resistance"} and frame_locations.get("15m") in {
        "breaking_below_support",
        "retesting_old_support_as_resistance",
    }:
        bias = "BEARISH"
    else:
        bias = "MIXED" if (supports or resistances or demands or supplies) else "NEUTRAL"
    strongest = max(
        [item.get("score", 0) for _, item in supports + resistances + demands + supplies] or [0]
    )
    quality = strength(strongest)
    warning = "inside chop range" if chop_range else (
        "near supply" if five_supply and abs(float(five_supply["zone_low"]) - current_price) / max(current_price, 0.01) * 100 <= 0.2
        else "near demand" if five_demand and abs(current_price - float(five_demand["zone_high"])) / max(current_price, 0.01) * 100 <= 0.2
        else "no clean edge"
    )
    if five_demand and five_supply:
        location = (
            f"{symbol} is between 5m demand near {float(five_demand['midpoint']):.2f} "
            f"and 5m supply near {float(five_supply['midpoint']):.2f}"
        )
    else:
        location = f"{symbol} has incomplete 5m demand/supply structure"
    confluences = [
        label
        for label, active in (
            ("support", support_confluence),
            ("resistance", resistance_confluence),
            ("demand", demand_confluence),
            ("supply", supply_confluence),
        )
        if active
    ]
    return {
        "symbol": symbol,
        "current_price": round(current_price, 4) if current_price else None,
        "major_support_area": major_support,
        "major_resistance_area": major_resistance,
        "major_demand_area": major_demand,
        "major_supply_area": major_supply,
        "support_confluence": support_confluence,
        "resistance_confluence": resistance_confluence,
        "demand_confluence": demand_confluence,
        "supply_confluence": supply_confluence,
        "confluence_reason": f"Overlapping multi-timeframe {', '.join(confluences)} detected" if confluences else "No clear multi-timeframe confluence",
        "current_price_location_summary": location,
        "market_structure_bias": bias,
        "structure_quality": quality,
        "structure_warning": warning,
        "chop_range_detected": chop_range,
        "range_low": round(float(range_low), 4) if range_low is not None else None,
        "range_high": round(float(range_high), 4) if range_high is not None else None,
        "can_approve_trades": False,
        "context_only": True,
        "engine_version": ENGINE_VERSION,
    }
