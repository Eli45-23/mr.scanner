from __future__ import annotations

from typing import Any, Dict, List, Optional


def _avg(values: List[float]) -> Optional[float]:
    vals = [value for value in values if value is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _directional_large_candle_count(bars: List[Any], direction: str) -> int:
    count = 0
    for bar in reversed(bars[-5:]):
        body = abs(bar.c - bar.o)
        candle_range = max(bar.h - bar.l, 0.01)
        large_body = body / candle_range >= 0.55
        aligned = (direction == "bullish" and bar.c > bar.o) or (direction == "bearish" and bar.c < bar.o)
        if large_body and aligned:
            count += 1
        else:
            break
    return count


def evaluate_volume_quality(
    bars: List[Any],
    config: Dict[str, Any],
    *,
    relative_volume: Optional[float] = None,
    direction: str = "neutral",
    setup_label: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = config.get("confirmation", {}).get("volume_quality", {})
    lookback = max(2, int(cfg.get("rvol_lookback_candles", 20)))
    min_confirm = float(cfg.get("min_rvol_confirmation", 1.5))
    strong_confirm = float(cfg.get("strong_rvol_confirmation", 2.0))
    climax_mult = float(cfg.get("climax_rvol_multiplier", 3.5))
    exhaustion_count = int(cfg.get("volume_exhaustion_candle_count", 3))

    if len(bars) < 2:
        return {
            "volume_score": 0,
            "volume_label": "WEAK",
            "rvol": 0.0,
            "is_volume_confirmed": False,
            "is_volume_exhausted": False,
            "reasons": [],
            "warnings": ["Not enough candles to confirm volume"],
        }

    latest = bars[-1]
    prior = bars[-(lookback + 1):-1] if len(bars) > lookback else bars[:-1]
    avg_volume = _avg([bar.v for bar in prior]) or 0.0
    rvol = float(relative_volume) if relative_volume is not None else (latest.v / avg_volume if avg_volume > 0 else 0.0)
    previous_volume = prior[-1].v if prior else 0.0
    expansion = latest.v / previous_volume if previous_volume > 0 else 0.0

    reasons: List[str] = []
    warnings: List[str] = []
    score = 35.0
    label = "NORMAL"
    if rvol < 1.2:
        score -= 20
        label = "WEAK"
        warnings.append("Volume confirmation is weak")
    elif rvol >= climax_mult:
        score += 28
        label = "CLIMAX"
        reasons.append(f"Climax volume {rvol:.2f}x recent average")
    elif rvol >= strong_confirm:
        score += 24
        label = "STRONG"
        reasons.append(f"Strong volume {rvol:.2f}x recent average")
    elif rvol >= min_confirm:
        score += 16
        label = "STRONG"
        reasons.append(f"Volume confirms at {rvol:.2f}x recent average")
    else:
        score += 2
        warnings.append(f"RVOL {rvol:.2f}x is below confirmation threshold")

    if expansion >= 1.25:
        score += 8
        reasons.append("Volume expanded versus prior candle")
    elif expansion and expansion < 0.75:
        score -= 4
        reasons.append("Pullback/retest volume is lighter than prior candle")

    setup_text = (setup_label or "").lower()
    if ("breakout" in setup_text or "breakdown" in setup_text or "orb" in setup_text) and rvol < min_confirm:
        score -= 10
        warnings.append("Breakout/breakdown is happening on low volume")
    if "sweep" in setup_text and rvol >= min_confirm:
        score += 8
        reasons.append("Sweep/reclaim has volume confirmation")
    if "retest" in setup_text and expansion < 1.2 and rvol < min_confirm:
        score += 5
        reasons.append("Retest/pullback volume is controlled")

    large_count = _directional_large_candle_count(bars, direction)
    exhausted = label == "CLIMAX" and large_count >= exhaustion_count
    if exhausted:
        score -= 18
        warnings.append("Volume climax after multiple large candles may be exhaustion")

    return {
        "volume_score": int(max(0, min(100, round(score)))),
        "volume_label": label,
        "rvol": round(rvol, 4),
        "is_volume_confirmed": rvol >= min_confirm,
        "is_volume_exhausted": exhausted,
        "reasons": reasons[:6],
        "warnings": warnings[:6],
    }
