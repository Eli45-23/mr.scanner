from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..base import pct_change, recent_swing_high, recent_swing_low, vwap


def _distance_pct(price: float, level: float) -> float:
    return abs(pct_change(price, level))


def _level_candidates(levels: Dict[str, float], bars: List[Any]) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
    bullish_names = [
        "pmh",
        "pdh",
        "opening_range_high",
        "opening_range_15_high",
        "recent_swing_high",
        "vwap",
    ]
    bearish_names = [
        "pml",
        "pdl",
        "opening_range_low",
        "opening_range_15_low",
        "recent_swing_low",
        "vwap",
    ]
    enriched = dict(levels)
    swing_high = recent_swing_high(bars)
    swing_low = recent_swing_low(bars)
    current_vwap = vwap(bars)
    if swing_high:
        enriched.setdefault("recent_swing_high", swing_high)
    if swing_low:
        enriched.setdefault("recent_swing_low", swing_low)
    if current_vwap:
        enriched.setdefault("vwap", current_vwap)
    bullish = [(name, float(enriched[name])) for name in bullish_names if isinstance(enriched.get(name), (int, float)) and enriched[name] > 0]
    bearish = [(name, float(enriched[name])) for name in bearish_names if isinstance(enriched.get(name), (int, float)) and enriched[name] > 0]
    return bullish, bearish


def _find_bullish_retest(
    bars: List[Any],
    candidates: List[Tuple[str, float]],
    lookback: int,
    max_distance_pct: float,
    pullback_volume_max: float,
) -> Optional[Dict[str, Any]]:
    window_start = max(1, len(bars) - lookback)
    latest = bars[-1]
    for name, level in candidates:
        breakout_index = None
        breakout_volume = 0.0
        for idx in range(window_start, len(bars)):
            if bars[idx - 1].c <= level < bars[idx].c:
                breakout_index = idx
                breakout_volume = bars[idx].v
        if breakout_index is None or breakout_index >= len(bars) - 1:
            continue
        after = bars[breakout_index + 1:]
        pulled_back = any(bar.l <= level * (1 + max_distance_pct / 100.0) for bar in after)
        distance = _distance_pct(latest.c, level)
        if pulled_back and latest.c > level and distance <= max_distance_pct:
            pullback_volume = max((bar.v for bar in after), default=latest.v)
            score = 68.0
            reasons = [f"Retested {name.upper()} and held above"]
            warnings: List[str] = []
            if breakout_volume and pullback_volume <= breakout_volume * pullback_volume_max:
                score += 10
                reasons.append("Pullback volume stayed controlled")
            elif breakout_volume:
                score -= 10
                warnings.append("Pullback volume expanded against the breakout")
            if latest.c >= latest.o:
                score += 6
                reasons.append("Buyers stepped back in after retest")
            return {
                "retest_active": True,
                "retest_type": "VWAP_RETEST_HOLD" if name == "vwap" else "BREAKOUT_RETEST_HOLD",
                "label": "VWAP Retest Holding" if name == "vwap" else "Breakout Retest Holding",
                "direction": "bullish",
                "level_name": name.upper(),
                "level_price": level,
                "distance_from_level_pct": round(distance, 4),
                "score": int(max(0, min(100, round(score)))),
                "entry_quality_label": "GOOD_POSITION",
                "reasons": reasons[:6],
                "warnings": warnings[:6],
            }
    return None


def _find_bearish_retest(
    bars: List[Any],
    candidates: List[Tuple[str, float]],
    lookback: int,
    max_distance_pct: float,
    pullback_volume_max: float,
) -> Optional[Dict[str, Any]]:
    window_start = max(1, len(bars) - lookback)
    latest = bars[-1]
    for name, level in candidates:
        breakdown_index = None
        breakdown_volume = 0.0
        for idx in range(window_start, len(bars)):
            if bars[idx - 1].c >= level > bars[idx].c:
                breakdown_index = idx
                breakdown_volume = bars[idx].v
        if breakdown_index is None or breakdown_index >= len(bars) - 1:
            continue
        after = bars[breakdown_index + 1:]
        retested = any(bar.h >= level * (1 - max_distance_pct / 100.0) for bar in after)
        distance = _distance_pct(latest.c, level)
        if retested and latest.c < level and distance <= max_distance_pct:
            retest_volume = max((bar.v for bar in after), default=latest.v)
            score = 68.0
            reasons = [f"Retested underside of {name.upper()} and rejected"]
            warnings: List[str] = []
            if breakdown_volume and retest_volume <= breakdown_volume * pullback_volume_max:
                score += 10
                reasons.append("Retest volume stayed controlled")
            elif breakdown_volume:
                score -= 10
                warnings.append("Retest volume expanded against the breakdown")
            if latest.c <= latest.o:
                score += 6
                reasons.append("Sellers stepped back in after retest")
            return {
                "retest_active": True,
                "retest_type": "VWAP_RETEST_REJECT" if name == "vwap" else "BREAKDOWN_RETEST_REJECT",
                "label": "VWAP Retest Rejecting" if name == "vwap" else "Breakdown Retest Rejecting",
                "direction": "bearish",
                "level_name": name.upper(),
                "level_price": level,
                "distance_from_level_pct": round(distance, 4),
                "score": int(max(0, min(100, round(score)))),
                "entry_quality_label": "GOOD_POSITION",
                "reasons": reasons[:6],
                "warnings": warnings[:6],
            }
    return None


def evaluate_retest_hold(
    bars: List[Any],
    config: Dict[str, Any],
    levels: Dict[str, float],
    *,
    direction: str = "neutral",
) -> Dict[str, Any]:
    cfg = config.get("confirmation", {}).get("retest_hold", {})
    lookback = max(2, int(cfg.get("retest_lookback_candles", 10)))
    max_distance_pct = float(cfg.get("retest_max_distance_from_level_pct", 0.15))
    pullback_volume_max = float(cfg.get("retest_pullback_volume_max_multiplier", 1.2))

    if len(bars) < 3:
        return {
            "retest_active": False,
            "retest_type": "NONE",
            "level_name": None,
            "level_price": None,
            "distance_from_level_pct": 0.0,
            "score": 0,
            "entry_quality_label": "UNKNOWN",
            "reasons": [],
            "warnings": ["Not enough candles to evaluate retest"],
        }

    bullish, bearish = _level_candidates(levels, bars)
    result = None
    if direction in {"bullish", "neutral"}:
        result = _find_bullish_retest(bars, bullish, lookback, max_distance_pct, pullback_volume_max)
    if result is None and direction in {"bearish", "neutral"}:
        result = _find_bearish_retest(bars, bearish, lookback, max_distance_pct, pullback_volume_max)
    if result is not None:
        return result

    latest = bars[-1]
    warnings: List[str] = []
    window_start = max(1, len(bars) - lookback)
    for name, level in bullish:
        broke = any(bars[idx - 1].c <= level < bars[idx].c for idx in range(window_start, len(bars)))
        if broke and latest.c < level:
            warnings.append(f"Fakeout Risk: broke above {name.upper()} but lost the level")
            break
        if broke and latest.c > level and _distance_pct(latest.c, level) > max_distance_pct:
            warnings.append("Late Entry Risk: price is away from the retest level")
            break
    if not warnings:
        for name, level in bearish:
            broke = any(bars[idx - 1].c >= level > bars[idx].c for idx in range(window_start, len(bars)))
            if broke and latest.c > level:
                warnings.append(f"Fakeout Risk: broke below {name.upper()} but reclaimed the level")
                break
            if broke and latest.c < level and _distance_pct(latest.c, level) > max_distance_pct:
                warnings.append("Late Entry Risk: price is away from the retest level")
                break
    nearest: Optional[Tuple[str, float, float]] = None
    for name, level in bullish + bearish:
        distance = _distance_pct(latest.c, level)
        if nearest is None or distance < nearest[2]:
            nearest = (name, level, distance)
    if not warnings and nearest and nearest[2] > max_distance_pct:
        warnings.append("Late Entry Risk: price is away from the retest level")
    return {
        "retest_active": False,
        "retest_type": "NONE",
        "level_name": nearest[0].upper() if nearest else None,
        "level_price": nearest[1] if nearest else None,
        "distance_from_level_pct": round(nearest[2], 4) if nearest else 0.0,
        "score": 0,
        "entry_quality_label": "LATE" if warnings else "UNKNOWN",
        "reasons": [],
        "warnings": warnings[:6],
    }
