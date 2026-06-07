from __future__ import annotations

from .base import StrategyContext, StrategyResult, clamp_score, confidence_label, crossed_above, crossed_below, ema, is_volume_confirmed, market_opposes, vwap
from .base import pct_change


def evaluate(ctx: StrategyContext) -> StrategyResult:
    if len(ctx.bars) < 3:
        return StrategyResult(strategy="vwap", label="No VWAP Setup")
    current_vwap = vwap(ctx.bars)
    if current_vwap is None:
        return StrategyResult(strategy="vwap", label="No VWAP Setup")
    latest = ctx.latest
    prev = ctx.bars[-2]
    current_ema9 = ema([bar.c for bar in ctx.bars], 9)
    prev_ema9 = ema([bar.c for bar in ctx.bars[:-1]], 9)
    volume_ok = is_volume_confirmed(ctx)
    distance_from_vwap = abs(pct_change(latest.c, current_vwap))
    if distance_from_vwap < 0.05 and not volume_ok:
        return StrategyResult(
            strategy="vwap",
            label="No VWAP Setup",
            levels={"vwap": current_vwap, "ema9": current_ema9},
        )

    reasons = []
    warnings = []
    score = 0.0
    label = "No VWAP Setup"
    direction = "neutral"
    active = False

    if crossed_above(prev.c, latest.c, current_vwap):
        active = True
        direction = "bullish"
        label = "VWAP Reclaim"
        score = 55
        reasons.append("Closed back above VWAP")
        if current_ema9 is not None and latest.c > current_ema9:
            score += 10
            reasons.append("Price above EMA9")
        if current_ema9 is not None and prev_ema9 is not None and current_ema9 >= prev_ema9:
            score += 6
            reasons.append("EMA9 is curling up")
        if volume_ok:
            score += 14
            reasons.append("Volume confirms VWAP reclaim")
        else:
            score -= 8
            warnings.append("VWAP reclaim volume is light")
        if market_opposes(ctx, direction):
            score -= 12
            warnings.append("SPY/QQQ do not support upside")
    elif crossed_below(prev.c, latest.c, current_vwap):
        active = True
        direction = "bearish"
        label = "VWAP Loss"
        score = 55
        reasons.append("Closed below VWAP")
        if current_ema9 is not None and latest.c < current_ema9:
            score += 10
            reasons.append("Price below EMA9")
        if volume_ok:
            score += 14
            reasons.append("Volume confirms VWAP loss")
        else:
            score -= 8
            warnings.append("VWAP loss volume is light")
        if market_opposes(ctx, direction):
            score -= 12
            warnings.append("SPY/QQQ do not support downside")
    elif prev.h >= current_vwap and latest.c < current_vwap and latest.c < latest.o:
        active = True
        direction = "bearish"
        label = "VWAP Rejection"
        score = 58
        reasons.append("Approached VWAP and rejected below it")
        if current_ema9 is not None and latest.c < current_ema9:
            score += 8
            reasons.append("Price remains below EMA9")
        if volume_ok:
            score += 12
            reasons.append("Sellers stepped in with volume")
        else:
            warnings.append("VWAP rejection volume is light")
        if market_opposes(ctx, direction):
            score -= 12
            warnings.append("SPY/QQQ are not weak enough")
    elif latest.l <= current_vwap <= latest.c:
        active = True
        direction = "bullish"
        label = "VWAP Hold"
        score = 52
        reasons.append("VWAP held as support")
        if volume_ok:
            score += 8
            reasons.append("Volume supports VWAP hold")

    final = clamp_score(score)
    return StrategyResult(
        strategy="vwap",
        label=label,
        direction=direction,
        active=active,
        score=final,
        confidence_label=confidence_label(final),
        reasons=reasons,
        warnings=warnings,
        levels={"vwap": current_vwap, "ema9": current_ema9},
    )
