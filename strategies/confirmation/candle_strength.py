from __future__ import annotations

from typing import Any, Dict, List, Optional


def _pct(value: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return (value / total) * 100.0


def evaluate_candle_strength(
    bars: List[Any],
    config: Dict[str, Any],
    *,
    direction: str = "neutral",
    volume_quality: Optional[Dict[str, Any]] = None,
    levels: Optional[Dict[str, float]] = None,
    setup_label: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = config.get("confirmation", {}).get("candle_strength", {})
    buyer_top_pct = float(cfg.get("buyer_control_close_top_pct", 25))
    seller_bottom_pct = float(cfg.get("seller_control_close_bottom_pct", 25))
    min_body_pct = float(cfg.get("min_body_pct_for_control", 45))
    large_wick_pct = float(cfg.get("large_wick_pct", 40))
    indecision_body_pct = float(cfg.get("indecision_body_pct", 25))

    if not bars:
        return {
            "candle_score": 0,
            "candle_label": "NEUTRAL",
            "close_position_pct": 0.0,
            "body_pct_of_range": 0.0,
            "upper_wick_pct": 0.0,
            "lower_wick_pct": 0.0,
            "reasons": [],
            "warnings": ["Not enough candles to evaluate candle strength"],
        }

    latest = bars[-1]
    candle_range = max(latest.h - latest.l, 0.01)
    body = abs(latest.c - latest.o)
    upper_wick = latest.h - max(latest.o, latest.c)
    lower_wick = min(latest.o, latest.c) - latest.l
    close_position = _pct(latest.c - latest.l, candle_range)
    body_pct = _pct(body, candle_range)
    upper_pct = _pct(max(0.0, upper_wick), candle_range)
    lower_pct = _pct(max(0.0, lower_wick), candle_range)

    reasons: List[str] = []
    warnings: List[str] = []
    score = 45.0
    label = "NEUTRAL"
    is_green = latest.c >= latest.o
    is_red = latest.c <= latest.o
    closes_near_high = close_position >= 100.0 - buyer_top_pct
    closes_near_low = close_position <= seller_bottom_pct
    meaningful_body = body_pct >= min_body_pct
    setup_text = (setup_label or "").lower()

    if body_pct <= indecision_body_pct:
        score -= 12
        label = "INDECISION"
        warnings.append("Small candle body shows indecision")
        if (volume_quality or {}).get("volume_label") in {"STRONG", "CLIMAX"}:
            score -= 5
            warnings.append("High volume with small body can mean churn")

    if label != "INDECISION" and upper_pct >= large_wick_pct and (direction == "bullish" or "breakout" in setup_text or "orb long" in setup_text):
        score -= 20
        label = "REJECTION"
        warnings.append("Upper wick rejection near breakout level")
    elif label != "INDECISION" and lower_pct >= large_wick_pct and (direction == "bearish" or "breakdown" in setup_text or "orb short" in setup_text):
        score -= 20
        label = "REJECTION"
        warnings.append("Lower wick demand/reclaim risk near breakdown level")

    if is_green and closes_near_high and meaningful_body and upper_pct < large_wick_pct:
        score += 24
        label = "BUYER_CONTROL"
        reasons.append("Candle closed near high with buyer control")
    elif is_red and closes_near_low and meaningful_body and lower_pct < large_wick_pct:
        score += 24
        label = "SELLER_CONTROL"
        reasons.append("Candle closed near low with seller control")
    elif label == "NEUTRAL":
        if closes_near_high and direction == "bullish":
            score += 8
            reasons.append("Candle close supports bullish setup")
        elif closes_near_low and direction == "bearish":
            score += 8
            reasons.append("Candle close supports bearish setup")

    if direction == "bullish" and label in {"SELLER_CONTROL", "REJECTION", "INDECISION"}:
        score -= 8
        warnings.append("Candle quality contradicts bullish setup")
    if direction == "bearish" and label in {"BUYER_CONTROL", "REJECTION", "INDECISION"}:
        score -= 8
        warnings.append("Candle quality contradicts bearish setup")

    return {
        "candle_score": int(max(0, min(100, round(score)))),
        "candle_label": label,
        "close_position_pct": round(close_position, 2),
        "body_pct_of_range": round(body_pct, 2),
        "upper_wick_pct": round(upper_pct, 2),
        "lower_wick_pct": round(lower_pct, 2),
        "reasons": reasons[:6],
        "warnings": warnings[:6],
    }
