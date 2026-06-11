from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .market_structure_models import clamp_score, normalize_bars, strength


ENGINE_VERSION = "liquidity-sweep-phase1-1.0"
SWEEP_STATUSES = {
    "NO_ACTIVE_SWEEP",
    "SWEEP_WATCH",
    "SWEEP_FORMING",
    "SWEEP_CONFIRMED",
    "SWEEP_FAILED_HELD",
}


def _candidate(
    *,
    direction: str,
    level: float,
    source: str,
    timeframe: str,
    zone_low: Optional[float] = None,
    zone_high: Optional[float] = None,
    related_zone: Optional[Dict[str, Any]] = None,
    overlap: bool = False,
) -> Dict[str, Any]:
    return {
        "direction": direction,
        "level": float(level),
        "source": source,
        "timeframe": timeframe,
        "zone_low": float(zone_low) if zone_low is not None else None,
        "zone_high": float(zone_high) if zone_high is not None else None,
        "related_zone": related_zone,
        "overlap": overlap,
    }


def build_sweep_candidates(
    market_structure: Optional[Dict[str, Any]],
    known_levels: Optional[Dict[str, Any]] = None,
    *,
    timeframes: Iterable[str] = ("1m", "5m", "15m"),
    use_supply_demand: bool = True,
    use_support_resistance: bool = True,
) -> List[Dict[str, Any]]:
    structure = market_structure or {}
    support_resistance = structure.get("support_resistance") or {}
    supply_demand = structure.get("supply_demand") or {}
    candidates: List[Dict[str, Any]] = []
    for timeframe in timeframes:
        frame_sr = support_resistance.get(timeframe, {})
        frame_sd = supply_demand.get(timeframe, {})
        supports = frame_sr.get("support_levels") or []
        resistances = frame_sr.get("resistance_levels") or []
        support_prices = [item.get("price") for item in supports if isinstance(item.get("price"), (int, float))]
        resistance_prices = [item.get("price") for item in resistances if isinstance(item.get("price"), (int, float))]
        for item in resistances if use_support_resistance else []:
            if isinstance(item.get("price"), (int, float)):
                candidates.append(_candidate(
                    direction="ABOVE_LEVEL", level=item["price"], source="resistance",
                    timeframe=timeframe, overlap=False,
                ))
        for item in supports if use_support_resistance else []:
            if isinstance(item.get("price"), (int, float)):
                candidates.append(_candidate(
                    direction="BELOW_LEVEL", level=item["price"], source="support",
                    timeframe=timeframe, overlap=False,
                ))
        for zone in (frame_sd.get("supply_zones") or []) if use_supply_demand else []:
            if isinstance(zone.get("zone_high"), (int, float)):
                overlap = any(zone["zone_low"] <= price <= zone["zone_high"] for price in resistance_prices)
                candidates.append(_candidate(
                    direction="ABOVE_LEVEL", level=zone["zone_high"], source=f"{timeframe}_supply",
                    timeframe=timeframe, zone_low=zone["zone_low"], zone_high=zone["zone_high"],
                    related_zone=zone, overlap=overlap,
                ))
        for zone in (frame_sd.get("demand_zones") or []) if use_supply_demand else []:
            if isinstance(zone.get("zone_low"), (int, float)):
                overlap = any(zone["zone_low"] <= price <= zone["zone_high"] for price in support_prices)
                candidates.append(_candidate(
                    direction="BELOW_LEVEL", level=zone["zone_low"], source=f"{timeframe}_demand",
                    timeframe=timeframe, zone_low=zone["zone_low"], zone_high=zone["zone_high"],
                    related_zone=zone, overlap=overlap,
                ))

    level_directions = {
        "hod": "ABOVE_LEVEL", "pmh": "ABOVE_LEVEL", "pdh": "ABOVE_LEVEL",
        "opening_range_high": "ABOVE_LEVEL", "recent_swing_high": "ABOVE_LEVEL",
        "lod": "BELOW_LEVEL", "pml": "BELOW_LEVEL", "pdl": "BELOW_LEVEL",
        "opening_range_low": "BELOW_LEVEL", "recent_swing_low": "BELOW_LEVEL",
    }
    for name, direction in level_directions.items():
        value = (known_levels or {}).get(name)
        if isinstance(value, (int, float)) and value > 0:
            candidates.append(_candidate(direction=direction, level=value, source=name, timeframe="mixed"))

    deduped: List[Dict[str, Any]] = []
    for item in sorted(candidates, key=lambda candidate: (candidate["direction"], candidate["level"])):
        existing = next(
            (
                prior for prior in deduped
                if prior["direction"] == item["direction"]
                and abs(prior["level"] - item["level"]) <= max(item["level"] * 0.0003, 0.01)
            ),
            None,
        )
        if existing:
            if (
                "supply" in item["source"]
                or "demand" in item["source"]
                or (item["timeframe"] in {"5m", "15m"} and existing["timeframe"] == "1m")
            ):
                existing.update(item)
            existing["overlap"] = True
        else:
            deduped.append(item)
    return deduped


def _base_result(symbol: str, timestamp: str, current_candle_closed: bool) -> Dict[str, Any]:
    return {
        "symbol": symbol.upper(),
        "timestamp": timestamp,
        "current_price": None,
        "sweep_status": "NO_ACTIVE_SWEEP",
        "sweep_direction": "NONE",
        "trap_bias": "NEUTRAL",
        "sweep_level": None,
        "sweep_zone_low": None,
        "sweep_zone_high": None,
        "level_source": None,
        "timeframe": None,
        "confidence": "LOW",
        "score": 0,
        "reason": "No active liquidity sweep near a clean market-structure level.",
        "meaning": "No buyer or seller trap is confirmed.",
        "wait_for": "Price to approach and react at a key liquidity level.",
        "invalidation": "No active sweep to invalidate.",
        "current_candle_closed": bool(current_candle_closed),
        "related_demand_zone": None,
        "related_supply_zone": None,
        "inside_chop_range": False,
        "telegram_eligible": False,
        "dashboard_eligible": False,
        "context_only": True,
        "can_approve_trades": False,
        "engine_version": ENGINE_VERSION,
        "nearest_upside_sweep_zone": None,
        "nearest_downside_sweep_zone": None,
    }


def evaluate_liquidity_sweeps(
    symbol: str,
    candles: Iterable[Any],
    *,
    market_structure: Optional[Dict[str, Any]] = None,
    known_levels: Optional[Dict[str, Any]] = None,
    current_candle_closed: bool = True,
    market_alignment: str = "UNKNOWN",
    watch_distance_bps: float = 8.0,
    min_confidence_score: int = 55,
    recently_swept_sources: Optional[Iterable[str]] = None,
    timeframes: Iterable[str] = ("1m", "5m", "15m"),
    use_supply_demand: bool = True,
    use_support_resistance: bool = True,
) -> Dict[str, Any]:
    bars = normalize_bars(candles)
    timestamp = (
        str(bars[-1]["t"]) if bars and bars[-1].get("t") is not None
        else datetime.now(timezone.utc).isoformat()
    )
    result = _base_result(symbol, timestamp, current_candle_closed)
    if not bars:
        result["reason"] = "Not enough candle data to evaluate liquidity sweeps."
        return result
    latest = bars[-1]
    current_price = latest["c"]
    result["current_price"] = round(current_price, 4)
    structure = market_structure or {}
    summary = structure.get("summary") if isinstance(structure.get("summary"), dict) else structure
    inside_chop = bool(summary.get("chop_range_detected") or "chop range" in str(summary.get("structure_warning") or "").lower())
    candidates = build_sweep_candidates(
        structure,
        known_levels,
        timeframes=timeframes,
        use_supply_demand=use_supply_demand,
        use_support_resistance=use_support_resistance,
    )
    if not candidates:
        result["reason"] = "No clean market-structure levels are available for liquidity sweep evaluation."
        return result

    upside = [item for item in candidates if item["direction"] == "ABOVE_LEVEL" and item["level"] >= current_price]
    downside = [item for item in candidates if item["direction"] == "BELOW_LEVEL" and item["level"] <= current_price]
    result["nearest_upside_sweep_zone"] = min(upside, key=lambda item: item["level"] - current_price, default=None)
    result["nearest_downside_sweep_zone"] = min(downside, key=lambda item: current_price - item["level"], default=None)
    avg_volume = sum(item["v"] for item in bars[-21:-1]) / max(1, len(bars[-21:-1]))
    volume_confirmed = latest["v"] >= max(avg_volume * 1.2, 1)
    candle_range = max(latest["h"] - latest["l"], 0.0001)
    upper_wick_pct = (latest["h"] - max(latest["o"], latest["c"])) / candle_range
    lower_wick_pct = (min(latest["o"], latest["c"]) - latest["l"]) / candle_range
    recent_sources = {str(value).lower() for value in (recently_swept_sources or [])}
    evaluated: List[Dict[str, Any]] = []

    for item in candidates:
        direction = item["direction"]
        level = item["level"]
        zone_low = item["zone_low"]
        zone_high = item["zone_high"]
        distance_bps = abs(current_price - level) / max(current_price, 0.01) * 10000
        broke = latest["h"] > level if direction == "ABOVE_LEVEL" else latest["l"] < level
        rejection_boundary = zone_low if direction == "ABOVE_LEVEL" and zone_low is not None else (
            zone_high if direction == "BELOW_LEVEL" and zone_high is not None else level
        )
        reclaimed = latest["c"] < rejection_boundary if direction == "ABOVE_LEVEL" else latest["c"] > rejection_boundary
        held_beyond = latest["c"] > level if direction == "ABOVE_LEVEL" else latest["c"] < level
        wick_strong = upper_wick_pct >= 0.35 if direction == "ABOVE_LEVEL" else lower_wick_pct >= 0.35

        status = "NO_ACTIVE_SWEEP"
        if broke and not current_candle_closed:
            status = "SWEEP_FORMING"
        elif broke and current_candle_closed and reclaimed:
            status = "SWEEP_CONFIRMED"
        elif broke and current_candle_closed and held_beyond:
            status = "SWEEP_FAILED_HELD"
        elif distance_bps <= watch_distance_bps:
            status = "SWEEP_WATCH"
        if status == "NO_ACTIVE_SWEEP":
            continue

        score = 50
        if broke:
            score += 20
        if status == "SWEEP_CONFIRMED":
            score += 20
        if wick_strong:
            score += 15
        else:
            score -= 15
        score += 10 if volume_confirmed else -15
        if item["timeframe"] in {"5m", "15m"} and ("supply" in item["source"] or "demand" in item["source"]):
            score += 10
        if item["source"] in {"hod", "lod", "pmh", "pml", "pdh", "pdl"}:
            score += 10
        breakout_bias = "BULLISH" if direction == "ABOVE_LEVEL" else "BEARISH"
        if market_alignment.upper() == "OPPOSED":
            score += 10
        elif market_alignment.upper() == breakout_bias:
            score -= 10
        if inside_chop:
            score += 10
        if item["overlap"]:
            score += 5
        if not current_candle_closed and status == "SWEEP_FORMING":
            score -= 20
        if status == "SWEEP_FAILED_HELD":
            score -= 20
        if item["timeframe"] == "1m":
            score -= 10
        if item["source"].lower() in recent_sources:
            score -= 10
        score = clamp_score(score)
        trap_bias = "BEARISH" if direction == "ABOVE_LEVEL" else "BULLISH"
        label = item["source"].replace("_", " ")
        zone_text = (
            f"{zone_low:.2f}-{zone_high:.2f}" if zone_low is not None and zone_high is not None else f"{level:.2f}"
        )
        if status == "SWEEP_CONFIRMED":
            reason = f"Price broke {'above' if direction == 'ABOVE_LEVEL' else 'below'} {label} and closed back {'below' if direction == 'ABOVE_LEVEL' else 'above'}."
            meaning = f"{'Buyers' if trap_bias == 'BEARISH' else 'Sellers'} may be trapped {'above' if direction == 'ABOVE_LEVEL' else 'below'} the level."
            wait_for = "Failed reclaim or lower high." if trap_bias == "BEARISH" else "Reclaim hold or higher low."
        elif status == "SWEEP_FORMING":
            reason = f"Price moved {'above' if direction == 'ABOVE_LEVEL' else 'below'} {label}, but the candle has not closed."
            meaning = "A possible trap is forming, but it is not confirmed."
            wait_for = "Wait for the candle to close back through the level."
        elif status == "SWEEP_FAILED_HELD":
            reason = f"Price broke {label} and held beyond it."
            meaning = "The break held; do not label this move as a liquidity trap."
            wait_for = "Wait for a later failed hold or retest."
        else:
            reason = f"Price is approaching {label} near {zone_text}."
            meaning = "Liquidity may be resting around this level."
            wait_for = "Watch for a break and candle-close rejection or reclaim."
        invalidation = (
            f"Clean reclaim and hold above {zone_text}." if trap_bias == "BEARISH"
            else f"Clean loss and hold below {zone_text}."
        )
        evaluated.append({
            **item,
            "status": status,
            "score": score,
            "confidence": strength(score),
            "trap_bias": trap_bias,
            "reason": reason,
            "meaning": meaning,
            "wait_for": wait_for,
            "invalidation": invalidation,
        })

    if not evaluated:
        result["inside_chop_range"] = inside_chop
        return result
    status_rank = {"SWEEP_CONFIRMED": 4, "SWEEP_FORMING": 3, "SWEEP_FAILED_HELD": 2, "SWEEP_WATCH": 1}
    best = max(evaluated, key=lambda item: (status_rank[item["status"]], item["score"]))
    dashboard_eligible = best["score"] >= min_confidence_score or best["status"] in {"SWEEP_FORMING", "SWEEP_CONFIRMED", "SWEEP_FAILED_HELD"}
    return {
        **result,
        "sweep_status": best["status"],
        "sweep_direction": best["direction"],
        "trap_bias": best["trap_bias"] if best["status"] != "SWEEP_FAILED_HELD" else "NEUTRAL",
        "sweep_level": round(best["level"], 4),
        "sweep_zone_low": round(best["zone_low"], 4) if best["zone_low"] is not None else None,
        "sweep_zone_high": round(best["zone_high"], 4) if best["zone_high"] is not None else None,
        "level_source": best["source"],
        "timeframe": best["timeframe"],
        "confidence": best["confidence"],
        "score": best["score"],
        "reason": best["reason"],
        "meaning": best["meaning"],
        "wait_for": best["wait_for"],
        "invalidation": best["invalidation"],
        "related_demand_zone": best["related_zone"] if best["direction"] == "BELOW_LEVEL" else None,
        "related_supply_zone": best["related_zone"] if best["direction"] == "ABOVE_LEVEL" else None,
        "inside_chop_range": inside_chop,
        "telegram_eligible": best["status"] == "SWEEP_CONFIRMED" and best["score"] >= 75,
        "dashboard_eligible": dashboard_eligible,
    }
