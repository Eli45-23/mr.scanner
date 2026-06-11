from __future__ import annotations

from typing import Any, Dict, Optional

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


def _engine_levels(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source_of_truth": "scanner_liquidity_sweep_engine",
        "liquidity_sweep_source": "engine",
        "sweep_status": context.get("sweep_status"),
        "sweep_direction": context.get("sweep_direction"),
        "trap_bias": context.get("trap_bias"),
        "level_source": context.get("level_source"),
        "sweep_level": context.get("sweep_level"),
        "sweep_zone_low": context.get("sweep_zone_low"),
        "sweep_zone_high": context.get("sweep_zone_high"),
        "can_approve_trades": False,
    }


def adapt_engine_result(context: Dict[str, Any]) -> StrategyResult:
    status = str(context.get("sweep_status") or "NO_ACTIVE_SWEEP").upper()
    sweep_direction = str(context.get("sweep_direction") or "NONE").upper()
    trap_bias = str(context.get("trap_bias") or "NEUTRAL").upper()
    raw_score = clamp_score(float(context.get("score") or 0))
    reasons = [text for text in (context.get("reason"), context.get("meaning")) if text]
    levels = _engine_levels(context)

    if status == "SWEEP_CONFIRMED" and sweep_direction == "BELOW_LEVEL" and trap_bias == "BULLISH":
        score = min(75, raw_score)
        return StrategyResult(
            strategy="liquidity_sweep",
            label="Bullish Liquidity Sweep Reclaim",
            direction="bullish",
            active=True,
            score=score,
            confidence_label=confidence_label(score),
            reasons=reasons,
            warnings=["Liquidity sweep is context-only; wait for confirmation"],
            levels=levels,
        )
    if status == "SWEEP_CONFIRMED" and sweep_direction == "ABOVE_LEVEL" and trap_bias == "BEARISH":
        score = min(75, raw_score)
        return StrategyResult(
            strategy="liquidity_sweep",
            label="Bearish Liquidity Sweep Rejection",
            direction="bearish",
            active=True,
            score=score,
            confidence_label=confidence_label(score),
            reasons=reasons,
            warnings=["Liquidity sweep is context-only; wait for confirmation"],
            levels=levels,
        )
    if status == "SWEEP_FORMING":
        score = min(55, raw_score)
        direction = "bullish" if trap_bias == "BULLISH" else "bearish" if trap_bias == "BEARISH" else "neutral"
        return StrategyResult(
            strategy="liquidity_sweep",
            label="Liquidity Sweep Forming - Wait For Candle Close",
            direction=direction,
            active=False,
            score=score,
            confidence_label=confidence_label(score),
            reasons=reasons,
            warnings=["Sweep forming only; candle has not closed."],
            levels=levels,
        )
    if status == "SWEEP_WATCH":
        return StrategyResult(
            strategy="liquidity_sweep",
            label="Liquidity Sweep Watch",
            active=False,
            score=min(45, raw_score),
            reasons=reasons,
            warnings=["Sweep watch only; no trap confirmed."],
            levels=levels,
        )
    if status == "SWEEP_FAILED_HELD":
        return StrategyResult(
            strategy="liquidity_sweep",
            label="Liquidity Sweep Failed / Break Held",
            active=False,
            score=min(35, raw_score),
            reasons=reasons,
            warnings=["Break held beyond the level; do not label this as a trap."],
            levels=levels,
        )
    return StrategyResult(
        strategy="liquidity_sweep",
        label="No Liquidity Sweep",
        levels=levels,
    )


def _legacy_evaluate(ctx: StrategyContext) -> StrategyResult:
    candles = max(1, int(ctx.config.get("strategy_engine", {}).get("sweep_reclaim_candles", 3)))
    if len(ctx.bars) < candles + 1:
        return StrategyResult(
            strategy="liquidity_sweep",
            label="No Liquidity Sweep",
            levels={"source_of_truth": "legacy_fallback", "liquidity_sweep_source": "legacy_fallback", "can_approve_trades": False},
        )
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
                levels={
                    "swept_level": level,
                    name: level,
                    "source_of_truth": "legacy_fallback",
                    "liquidity_sweep_source": "legacy_fallback",
                    "can_approve_trades": False,
                },
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
                levels={
                    "swept_level": level,
                    name: level,
                    "source_of_truth": "legacy_fallback",
                    "liquidity_sweep_source": "legacy_fallback",
                    "can_approve_trades": False,
                },
            )

    return StrategyResult(
        strategy="liquidity_sweep",
        label="No Liquidity Sweep",
        levels={"source_of_truth": "legacy_fallback", "liquidity_sweep_source": "legacy_fallback", "can_approve_trades": False},
    )


def evaluate(ctx: StrategyContext, liquidity_sweep_context: Optional[Dict[str, Any]] = None) -> StrategyResult:
    engine_context = liquidity_sweep_context if liquidity_sweep_context is not None else ctx.liquidity_sweep_context
    if engine_context is not None:
        return adapt_engine_result(engine_context)
    if not ctx.config.get("strategy_engine", {}).get("liquidity_sweep_strategy_legacy_fallback", True):
        return StrategyResult(
            strategy="liquidity_sweep",
            label="No Liquidity Sweep",
            levels={"source_of_truth": "none", "liquidity_sweep_source": "none", "can_approve_trades": False},
        )
    return _legacy_evaluate(ctx)
