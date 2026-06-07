from __future__ import annotations

from .base import StrategyContext, StrategyResult, clamp_score, confidence_label, ema, is_volume_confirmed, market_opposes, opening_range_complete, opening_range_levels, pct_change, vwap


def _evaluate_minutes(ctx: StrategyContext, minutes: int) -> StrategyResult:
    market_open = str(ctx.config.get("market_open", "09:30"))
    if not opening_range_complete(ctx.bars, market_open, minutes):
        return StrategyResult(strategy="opening_range", label=f"{minutes}-Min OR Not Formed")
    levels = opening_range_levels(ctx.bars, market_open, minutes)
    high = levels.get("high")
    low = levels.get("low")
    latest = ctx.latest
    if not high or not low:
        return StrategyResult(strategy="opening_range", label=f"{minutes}-Min OR Not Formed")
    direction = "neutral"
    label = "No ORB"
    active = False
    trigger = None
    if latest.c > high:
        direction = "bullish"
        label = f"{minutes}-Min ORB Long"
        active = True
        trigger = high
    elif latest.c < low:
        direction = "bearish"
        label = f"{minutes}-Min ORB Short"
        active = True
        trigger = low
    else:
        return StrategyResult(strategy="opening_range", label="No ORB", levels={f"or_{minutes}_high": high, f"or_{minutes}_low": low})

    current_vwap = vwap(ctx.bars)
    current_ema9 = ema([bar.c for bar in ctx.bars], 9)
    volume_ok = is_volume_confirmed(ctx)
    score = 52.0
    reasons = [f"Closed beyond {minutes}-minute opening range"]
    warnings = []
    if volume_ok:
        score += 15
        reasons.append("Volume confirms ORB")
    else:
        score -= 8
        warnings.append("ORB volume is light")
    if direction == "bullish":
        if current_vwap is not None and latest.c > current_vwap:
            score += 8
            reasons.append("Price above VWAP")
        if current_ema9 is not None and latest.c > current_ema9:
            score += 6
            reasons.append("Price above EMA9")
    else:
        if current_vwap is not None and latest.c < current_vwap:
            score += 8
            reasons.append("Price below VWAP")
        if current_ema9 is not None and latest.c < current_ema9:
            score += 6
            reasons.append("Price below EMA9")
    if market_opposes(ctx, direction):
        score -= 12
        warnings.append("SPY/QQQ are not confirming ORB")
    elif ctx.market_alignment == "ALIGNED":
        score += 8
        reasons.append("SPY/QQQ confirmation preferred and present")
    if trigger:
        distance = abs(pct_change(latest.c, trigger))
        if distance > 0.75:
            score -= 20
            warnings.append("ORB Fakeout Risk: entry is late/extended from range")
    final = clamp_score(score)
    return StrategyResult(
        strategy="opening_range",
        label=label,
        direction=direction,
        active=active,
        score=final,
        confidence_label=confidence_label(final),
        reasons=reasons,
        warnings=warnings,
        levels={f"or_{minutes}_high": high, f"or_{minutes}_low": low},
    )


def evaluate(ctx: StrategyContext) -> list[StrategyResult]:
    cfg = ctx.config.get("strategy_engine", {})
    primary = int(cfg.get("opening_range_minutes_primary", 5))
    secondary = int(cfg.get("opening_range_minutes_secondary", 15))
    results = [_evaluate_minutes(ctx, primary)]
    if secondary != primary:
        results.append(_evaluate_minutes(ctx, secondary))
    return results
