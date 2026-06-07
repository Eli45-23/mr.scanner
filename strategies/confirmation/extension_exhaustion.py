from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..base import ema, pct_change, vwap


def _consecutive_large_candles(bars: List[Any], direction: str) -> int:
    count = 0
    for bar in reversed(bars[-6:]):
        candle_range = max(bar.h - bar.l, 0.01)
        body_pct = abs(bar.c - bar.o) / candle_range
        aligned = (direction == "bullish" and bar.c > bar.o) or (direction == "bearish" and bar.c < bar.o)
        if aligned and body_pct >= 0.55:
            count += 1
        else:
            break
    return count


def _nearest_level_distance(price: float, levels: Dict[str, float]) -> float:
    distances = [abs(pct_change(price, level)) for level in levels.values() if isinstance(level, (int, float)) and level > 0]
    return min(distances) if distances else 0.0


def evaluate_extension_exhaustion(
    bars: List[Any],
    config: Dict[str, Any],
    levels: Dict[str, float],
    *,
    direction: str = "neutral",
    volume_quality: Optional[Dict[str, Any]] = None,
    candle_strength: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = config.get("confirmation", {}).get("extension_exhaustion", {})
    max_vwap_pct = float(cfg.get("max_extension_from_vwap_pct", config.get("strategy_engine", {}).get("max_extension_from_vwap_pct", 0.6)))
    max_ema_pct = float(cfg.get("max_extension_from_ema9_pct", config.get("strategy_engine", {}).get("max_extension_from_ema9_pct", 0.4)))
    max_level_pct = float(cfg.get("max_extension_from_key_level_pct", 0.3))
    large_limit = int(cfg.get("consecutive_large_candle_limit", 3))
    do_not_chase_score = float(cfg.get("do_not_chase_extension_score", 80))

    if not bars:
        return {
            "extension_score": 0,
            "extension_label": "NORMAL",
            "distance_from_vwap_pct": 0.0,
            "distance_from_ema9_pct": 0.0,
            "distance_from_key_level_pct": 0.0,
            "consecutive_large_candles": 0,
            "reasons": [],
            "warnings": ["Not enough candles to evaluate extension"],
        }

    latest = bars[-1]
    current_vwap = vwap(bars)
    current_ema9 = ema([bar.c for bar in bars], 9)
    distance_vwap = abs(pct_change(latest.c, current_vwap)) if current_vwap else 0.0
    distance_ema = abs(pct_change(latest.c, current_ema9)) if current_ema9 else 0.0
    distance_level = _nearest_level_distance(latest.c, levels)
    large_count = _consecutive_large_candles(bars, direction)

    score = 20.0
    reasons: List[str] = []
    warnings: List[str] = []
    label = "NORMAL"

    if distance_vwap <= max_vwap_pct and distance_ema <= max_ema_pct and (not levels or distance_level <= max_level_pct):
        reasons.append("Price remains near VWAP/EMA9 and key level")
    if distance_vwap > max_vwap_pct:
        score += min(35, (distance_vwap / max_vwap_pct) * 12)
        warnings.append("Setup is valid but price is extended from VWAP")
    if distance_ema > max_ema_pct:
        score += min(30, (distance_ema / max_ema_pct) * 10)
        warnings.append("Setup is valid but price is extended from EMA9")
    if levels and distance_level > max_level_pct:
        score += min(25, (distance_level / max_level_pct) * 8)
        warnings.append("Late Entry Risk: price is far from the key level")
    if large_count >= large_limit:
        score += 22
        warnings.append("Late Entry Risk: multiple large candles already printed")
    if (volume_quality or {}).get("is_volume_exhausted") or (volume_quality or {}).get("volume_label") == "CLIMAX":
        score += 18
        warnings.append("Volume climax after extension may be exhaustion")
    if (candle_strength or {}).get("candle_label") == "REJECTION" and score >= 40:
        score += 12
        warnings.append("Wick rejection after extension increases exhaustion risk")

    score_int = int(max(0, min(100, round(score))))
    if score_int >= do_not_chase_score:
        label = "DO_NOT_CHASE"
        warnings.append("Do Not Chase: setup is valid but entry may be late")
    elif score_int >= 65:
        label = "VERY_EXTENDED"
    elif score_int >= 40:
        label = "EXTENDED"

    return {
        "extension_score": score_int,
        "extension_label": label,
        "distance_from_vwap_pct": round(distance_vwap, 4),
        "distance_from_ema9_pct": round(distance_ema, 4),
        "distance_from_key_level_pct": round(distance_level, 4),
        "consecutive_large_candles": large_count,
        "reasons": reasons[:6],
        "warnings": warnings[:8],
    }
