from __future__ import annotations

from typing import Dict, Optional, Tuple

from .base import StrategyContext, StrategyResult, clamp_score, confidence_label, ema, is_volume_confirmed, market_opposes, pct_change, recent_swing_high, recent_swing_low, vwap


def _candidate_levels(ctx: StrategyContext) -> tuple[Dict[str, float], Dict[str, float]]:
    highs: Dict[str, float] = {}
    lows: Dict[str, float] = {}
    for key in ("pmh", "pdh", "opening_range_high", "opening_range_15_high"):
        value = ctx.levels.get(key)
        if value:
            highs[key] = value
    for key in ("pml", "pdl", "opening_range_low", "opening_range_15_low"):
        value = ctx.levels.get(key)
        if value:
            lows[key] = value
    swing_high = recent_swing_high(ctx.bars)
    swing_low = recent_swing_low(ctx.bars)
    if swing_high:
        highs["recent_swing_high"] = swing_high
    if swing_low:
        lows["recent_swing_low"] = swing_low
    return highs, lows


def _nearest_broken_level(price: float, highs: Dict[str, float], lows: Dict[str, float]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    broken_highs = [(name, level) for name, level in highs.items() if price > level]
    broken_lows = [(name, level) for name, level in lows.items() if price < level]
    best_high = min(broken_highs, key=lambda item: abs(price - item[1]), default=None)
    best_low = min(broken_lows, key=lambda item: abs(price - item[1]), default=None)
    if best_high and not best_low:
        return best_high[0], best_high[1], "bullish"
    if best_low and not best_high:
        return best_low[0], best_low[1], "bearish"
    if best_high and best_low:
        high_dist = abs(pct_change(price, best_high[1]))
        low_dist = abs(pct_change(price, best_low[1]))
        if high_dist <= low_dist:
            return best_high[0], best_high[1], "bullish"
        return best_low[0], best_low[1], "bearish"
    return None, None, None


def evaluate(ctx: StrategyContext) -> StrategyResult:
    latest = ctx.latest
    highs, lows = _candidate_levels(ctx)
    level_name, level, direction = _nearest_broken_level(latest.c, highs, lows)
    if not level or not direction:
        return StrategyResult(strategy="breakout", label="No Breakout", active=False)

    bars = ctx.bars
    price_vwap = vwap(bars)
    price_ema9 = ema([bar.c for bar in bars], 9)
    volume_ok = is_volume_confirmed(ctx)
    above_vwap = price_vwap is not None and latest.c > price_vwap
    below_vwap = price_vwap is not None and latest.c < price_vwap
    above_ema = price_ema9 is not None and latest.c > price_ema9
    below_ema = price_ema9 is not None and latest.c < price_ema9

    score = 45.0
    reasons = [f"Closed beyond {level_name.replace('_', ' ')}"]
    warnings = []
    if volume_ok:
        score += 15
        reasons.append("Volume confirms")
    else:
        score -= 12
        warnings.append("Weak breakout volume")

    if direction == "bullish":
        label = "Clean Breakout"
        if above_vwap:
            score += 10
            reasons.append("Price above VWAP")
        else:
            score -= 10
            warnings.append("Price is not above VWAP")
        if above_ema:
            score += 8
            reasons.append("Price above EMA9")
        else:
            warnings.append("Price is not above EMA9")
    else:
        label = "Clean Breakdown"
        if below_vwap:
            score += 10
            reasons.append("Price below VWAP")
        else:
            score -= 10
            warnings.append("Price is not below VWAP")
        if below_ema:
            score += 8
            reasons.append("Price below EMA9")
        else:
            warnings.append("Price is not below EMA9")

    if market_opposes(ctx, direction):
        score -= 18
        warnings.append("SPY/QQQ are opposing the setup")
    elif ctx.market_alignment == "ALIGNED":
        score += 10
        reasons.append("SPY/QQQ confirming")

    extension_ref = price_vwap or price_ema9
    if extension_ref:
        extension = abs(pct_change(latest.c, extension_ref))
        if extension > float(ctx.config.get("strategy_engine", {}).get("max_extension_from_vwap_pct", 0.6)):
            score -= 22
            warnings.append("Do Not Chase: price is extended from VWAP")

    if score < 60:
        label = "Weak Breakout" if direction == "bullish" else "Weak Breakdown"
    if any("Do Not Chase" in warning for warning in warnings):
        label = "Do Not Chase"
    if not volume_ok and not ((direction == "bullish" and above_vwap) or (direction == "bearish" and below_vwap)):
        label = "Possible Fakeout"

    final = clamp_score(score)
    return StrategyResult(
        strategy="breakout",
        label=label,
        direction=direction,
        active=True,
        score=final,
        confidence_label=confidence_label(final),
        reasons=reasons,
        warnings=warnings,
        levels={level_name: level, "vwap": price_vwap, "ema9": price_ema9},
    )
