from __future__ import annotations

from typing import Dict

from .base import StrategyContext, StrategyResult, clamp_score, confidence_label, is_volume_confirmed, market_opposes, recent_swing_high, recent_swing_low


def _levels(ctx: StrategyContext, candles: int) -> tuple[Dict[str, float], Dict[str, float]]:
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
    # Swing levels must come from before the sweep/reclaim window; otherwise
    # the sweep candle can manufacture its own "recent swing" level.
    prior_bars = ctx.bars[:-candles] if len(ctx.bars) > candles else []
    swing_high = recent_swing_high(prior_bars)
    swing_low = recent_swing_low(prior_bars)
    if swing_high:
        highs["recent_swing_high"] = swing_high
    if swing_low:
        lows["recent_swing_low"] = swing_low
    return highs, lows


def evaluate(ctx: StrategyContext) -> StrategyResult:
    candles = max(1, int(ctx.config.get("strategy_engine", {}).get("sweep_reclaim_candles", 3)))
    if len(ctx.bars) < candles + 1:
        return StrategyResult(strategy="liquidity_sweep", label="No Liquidity Sweep")
    window = ctx.bars[-candles:]
    latest = ctx.latest
    highs, lows = _levels(ctx, candles)
    volume_ok = is_volume_confirmed(ctx)

    for name, level in lows.items():
        swept = any(bar.l < level for bar in window)
        reclaimed = latest.c > level
        if swept and reclaimed:
            score = 58.0
            reasons = [f"Swept below {name.replace('_', ' ')} and reclaimed"]
            warnings = []
            if latest.c > latest.o:
                score += 8
                reasons.append("Reclaim candle closed green")
            else:
                warnings.append("Reclaim candle is weak")
            if volume_ok:
                score += 14
                reasons.append("Volume expanded on sweep/reclaim")
            else:
                score -= 10
                warnings.append("Reclaim volume is light")
            if market_opposes(ctx, "bullish"):
                score -= 12
                warnings.append("SPY/QQQ are breaking down against the reclaim")
            elif ctx.market_alignment in {"ALIGNED", "MIXED", "UNKNOWN"}:
                score += 6
                reasons.append("SPY/QQQ not breaking down hard")
            final = clamp_score(score)
            label = "Bullish Liquidity Sweep Reclaim" if final >= 60 else "Possible Sweep - Wait For Confirmation"
            if final < 50:
                warnings.append("Fakeout Risk")
            return StrategyResult(
                strategy="liquidity_sweep",
                label=label,
                direction="bullish",
                active=True,
                score=final,
                confidence_label=confidence_label(final),
                reasons=reasons,
                warnings=warnings,
                levels={"swept_level": level, name: level},
            )

    for name, level in highs.items():
        swept = any(bar.h > level for bar in window)
        rejected = latest.c < level
        if swept and rejected:
            score = 58.0
            reasons = [f"Swept above {name.replace('_', ' ')} and rejected"]
            warnings = []
            if latest.c < latest.o:
                score += 8
                reasons.append("Rejection candle closed red")
            else:
                warnings.append("Rejection candle is weak")
            if volume_ok:
                score += 14
                reasons.append("Volume expanded on sweep/rejection")
            else:
                score -= 10
                warnings.append("Rejection volume is light")
            if market_opposes(ctx, "bearish"):
                score -= 12
                warnings.append("SPY/QQQ are strongly bullish against the rejection")
            elif ctx.market_alignment in {"ALIGNED", "MIXED", "UNKNOWN"}:
                score += 6
                reasons.append("SPY/QQQ not strongly bullish")
            final = clamp_score(score)
            label = "Bearish Liquidity Sweep Rejection" if final >= 60 else "Possible Sweep - Wait For Confirmation"
            if final < 50:
                warnings.append("Fakeout Risk")
            return StrategyResult(
                strategy="liquidity_sweep",
                label=label,
                direction="bearish",
                active=True,
                score=final,
                confidence_label=confidence_label(final),
                reasons=reasons,
                warnings=warnings,
                levels={"swept_level": level, name: level},
            )

    return StrategyResult(strategy="liquidity_sweep", label="No Liquidity Sweep")
