from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .market_structure_models import ENGINE_VERSION, clamp_score, normalize_bars, strength, timestamp_text
from .zone_quality import rank_zones_by_quality
from .zone_triggers import derive_triggers_for_zones


def _average(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _candidate(price: float, source: str, index: int, bars: List[Dict[str, Any]], kind: str) -> Dict[str, Any]:
    bar = bars[index]
    avg_volume = _average(item["v"] for item in bars[max(0, index - 10) : index]) or bar["v"] or 1
    range_size = max(bar["h"] - bar["l"], 0.0001)
    close_away = ((bar["c"] - bar["l"]) / range_size >= 0.7) if kind == "support" else ((bar["h"] - bar["c"]) / range_size >= 0.7)
    return {
        "price": float(price),
        "source": source,
        "index": index,
        "volume_confirmed": bar["v"] >= avg_volume * 1.2,
        "strong_close": close_away,
        "last_touched_at": timestamp_text(bar["t"]),
    }


def _merge(candidates: List[Dict[str, Any]], bars: List[Dict[str, Any]], price: float, kind: str) -> List[Dict[str, Any]]:
    tolerance = max(price * 0.0008, _average(item["h"] - item["l"] for item in bars[-20:]) * 0.30)
    groups: List[List[Dict[str, Any]]] = []
    for candidate in sorted(candidates, key=lambda item: item["price"]):
        group = next((items for items in groups if abs(_average(item["price"] for item in items) - candidate["price"]) <= tolerance), None)
        if group is None:
            groups.append([candidate])
        else:
            group.append(candidate)

    output: List[Dict[str, Any]] = []
    for group in groups:
        level = _average(item["price"] for item in group)
        tests = len({item["index"] for item in group})
        latest_index = max(item["index"] for item in group)
        score = 50
        if tests >= 2:
            score += 15
        if any(item["strong_close"] for item in group):
            score += 15
        if any(item["source"] in {"vwap", "ema9", "ema20"} for item in group):
            score += 10
        if any(item["source"] in {"pmh", "pml", "pdh", "pdl", "pdc", "hod", "lod", "opening_range_high", "opening_range_low"} for item in group):
            score += 10
        if any(item["volume_confirmed"] for item in group):
            score += 10
        if any(item["strong_close"] for item in group):
            score += 10
        crosses = sum(
            1
            for left, right in zip(bars[-12:-1], bars[-11:])
            if (left["c"] - level) * (right["c"] - level) < 0
        )
        if crosses >= 3:
            score -= 15
        if tests >= 5:
            score -= 10
        failed = (kind == "support" and bars[-1]["c"] < level - tolerance) or (kind == "resistance" and bars[-1]["c"] > level + tolerance)
        if failed:
            score -= 20
        score = clamp_score(score)
        sources = sorted({item["source"] for item in group})
        reaction = "bounced" if kind == "support" else "rejected"
        output.append(
            {
                "price": round(level, 4),
                "strength": strength(score),
                "score": score,
                "times_tested": tests,
                "fresh": tests <= 1,
                "last_touched_at": max((item["last_touched_at"] for item in group if item["last_touched_at"]), default=None),
                "distance_from_current_price": round(abs(price - level), 4),
                "reason": f"Price {reaction} from this level {tests} time{'s' if tests != 1 else ''}; sources: {', '.join(sources)}",
                "source": "/".join(sources),
                "_latest_index": latest_index,
            }
        )
    return output


def detect_support_resistance(
    symbol: str,
    timeframe: str,
    candles: Iterable[Any],
    *,
    current_price: Optional[float] = None,
    known_levels: Optional[Dict[str, Optional[float]]] = None,
    max_levels: int = 3,
    min_strength: int = 0,
    zone_quality_config: Optional[Dict[str, Any]] = None,
    zone_trigger_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    bars = normalize_bars(candles)
    price = float(current_price or (bars[-1]["c"] if bars else 0))
    base = {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "current_price": round(price, 4) if price else None,
        "support_levels": [],
        "resistance_levels": [],
        "nearest_support_below": {},
        "nearest_resistance_above": {},
        "current_price_location": "between_levels",
        "engine_version": ENGINE_VERSION,
        "context_only": True,
    }
    if len(bars) < 5 or price <= 0:
        return {**base, "reason": "Not enough candle data to detect support/resistance"}

    supports: List[Dict[str, Any]] = []
    resistances: List[Dict[str, Any]] = []
    for index in range(2, len(bars) - 2):
        window = bars[index - 2 : index + 3]
        bar = bars[index]
        if bar["l"] <= min(item["l"] for item in window):
            supports.append(_candidate(bar["l"], "swing_low", index, bars, "support"))
        if bar["h"] >= max(item["h"] for item in window):
            resistances.append(_candidate(bar["h"], "swing_high", index, bars, "resistance"))

    for name, raw in (known_levels or {}).items():
        if not isinstance(raw, (int, float)) or raw <= 0:
            continue
        candidate = _candidate(float(raw), name.lower(), len(bars) - 1, bars, "support" if raw <= price else "resistance")
        (supports if raw <= price else resistances).append(candidate)

    # Retests and role reversals are identified from recent close crossings.
    for index in range(2, len(bars) - 1):
        prior = bars[index - 2]
        bar = bars[index]
        later = bars[index + 1]
        if prior["c"] < bar["h"] and later["l"] <= bar["h"] <= later["c"]:
            supports.append(_candidate(bar["h"], "retest_old_resistance", index + 1, bars, "support"))
        if prior["c"] > bar["l"] and later["h"] >= bar["l"] >= later["c"]:
            resistances.append(_candidate(bar["l"], "retest_old_support", index + 1, bars, "resistance"))

    context = {"known_levels": known_levels or {}}
    average_range = _average(item["h"] - item["l"] for item in bars[-20:]) or None
    support_levels = [item for item in _merge(supports, bars, price, "support") if item["score"] >= min_strength and item["price"] <= price]
    resistance_levels = [item for item in _merge(resistances, bars, price, "resistance") if item["score"] >= min_strength and item["price"] >= price]
    support_levels = derive_triggers_for_zones(
        rank_zones_by_quality(({**item, "zone_type": "support"} for item in support_levels), context, zone_quality_config),
        current_price=price, atr=average_range, config=zone_trigger_config,
    )[:max_levels]
    resistance_levels = derive_triggers_for_zones(
        rank_zones_by_quality(({**item, "zone_type": "resistance"} for item in resistance_levels), context, zone_quality_config),
        current_price=price, atr=average_range, config=zone_trigger_config,
    )[:max_levels]
    for item in support_levels + resistance_levels:
        item.pop("_latest_index", None)
    nearest_support = min(support_levels, key=lambda item: price - item["price"], default={})
    nearest_resistance = min(resistance_levels, key=lambda item: item["price"] - price, default={})
    tolerance = price * 0.0015
    location = "between_levels"
    if nearest_support and abs(price - nearest_support["price"]) <= tolerance:
        location = "near_support"
    if nearest_resistance and abs(nearest_resistance["price"] - price) <= tolerance:
        location = "near_resistance"
    if nearest_support and "retest_old_resistance" in nearest_support.get("source", "") and abs(price - nearest_support["price"]) <= tolerance:
        location = "retesting_old_resistance_as_support"
    if nearest_resistance and "retest_old_support" in nearest_resistance.get("source", "") and abs(nearest_resistance["price"] - price) <= tolerance:
        location = "retesting_old_support_as_resistance"
    if nearest_resistance and price > nearest_resistance["price"]:
        location = "breaking_above_resistance"
    if nearest_support and price < nearest_support["price"]:
        location = "breaking_below_support"
    return {
        **base,
        "support_levels": support_levels,
        "resistance_levels": resistance_levels,
        "nearest_support_below": nearest_support,
        "nearest_resistance_above": nearest_resistance,
        "current_price_location": location,
    }
