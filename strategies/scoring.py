from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import StrategyContext, confidence_label, ema, pct_change, recent_volume_multiplier, vwap
from . import breakout, liquidity_sweep, opening_range, vwap_reclaim
from .confirmation import (
    evaluate_candle_strength,
    evaluate_extension_exhaustion,
    evaluate_market_regime,
    evaluate_pressure_score,
    evaluate_relative_strength,
    evaluate_retest_hold,
    evaluate_volume_quality,
)
from .scenario import evaluate_scenario_suite
from .setup_classifier import classify_professional_setup


def _risk_label(score: int, warnings: List[str]) -> str:
    if any("Do Not Chase" in warning for warning in warnings):
        return "DO_NOT_CHASE"
    if any("Fakeout" in warning for warning in warnings):
        return "HIGH"
    if score >= 80:
        return "LOW"
    if score >= 60:
        return "MEDIUM"
    return "HIGH"


def _warning_priority(warning: str) -> int:
    text = warning.lower()
    if "do not chase" in text:
        return 0
    if "market regime is opposing" in text or "market" in text and "oppos" in text:
        return 1
    if "relative strength" in text or "relative weakness" in text:
        return 2
    if "fakeout" in text:
        return 3
    if "weak volume" in text or "low volume" in text or "rvol" in text or "volume confirmation" in text:
        return 4
    if "rejection" in text or "indecision" in text or "churn" in text or "wick" in text:
        return 5
    if "spread" in text or "pressure" in text or "top-of-book" in text:
        return 6
    return 7


def _prioritized_warnings(warnings: List[str]) -> List[str]:
    indexed = list(enumerate(warnings))
    indexed.sort(key=lambda item: (_warning_priority(item[1]), item[0]))
    return [warning for _, warning in indexed]


def _combined_score(active: List[Dict[str, Any]], warnings: List[str]) -> int:
    if not active:
        return 0
    score = max(int(item.get("score") or 0) for item in active)
    if len(active) > 1:
        score += min(15, (len(active) - 1) * 7)
    directions = {item.get("direction") for item in active if item.get("direction") in {"bullish", "bearish"}}
    if len(directions) > 1:
        score -= 20
        warnings.append("Contradicting strategies are active")
    return int(max(0, min(100, score)))


def evaluate_strategy_suite(
    symbol: str,
    bars: List[Any],
    latest: Any,
    config: Dict[str, Any],
    levels: Dict[str, Optional[float]],
    relative_volume: Optional[float],
    market_alignment: str,
    market_bars: Optional[Dict[str, List[Any]]] = None,
    pressure_data: Optional[Dict[str, Any]] = None,
    option_context: Optional[Dict[str, Any]] = None,
    phase1_summary: Optional[Dict[str, Any]] = None,
    phase2_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ctx = StrategyContext(
        symbol=symbol,
        bars=bars,
        latest=latest,
        config=config,
        levels=levels,
        relative_volume=relative_volume,
        market_alignment=market_alignment,
    )
    cfg = config.get("strategy_engine", {})
    results = [breakout.evaluate(ctx)]
    if cfg.get("enable_liquidity_sweep", True):
        results.append(liquidity_sweep.evaluate(ctx))
    if cfg.get("enable_vwap_reclaim", True):
        results.append(vwap_reclaim.evaluate(ctx))
    if cfg.get("enable_opening_range", True):
        results.extend(opening_range.evaluate(ctx))

    result_dicts = [result.to_dict() for result in results]
    active = [item for item in result_dicts if item.get("active")]
    active.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    reasons: List[str] = []
    warnings: List[str] = []
    combined_levels: Dict[str, float] = {}
    for item in active:
        for reason in item.get("reasons") or []:
            if reason not in reasons:
                reasons.append(reason)
        for warning in item.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
        for key, value in (item.get("levels") or {}).items():
            if isinstance(value, (int, float)):
                combined_levels[key] = value

    current_vwap = vwap(bars)
    current_ema9 = ema([bar.c for bar in bars], 9)
    if current_vwap:
        combined_levels.setdefault("vwap", current_vwap)
    if current_ema9:
        combined_levels.setdefault("ema9", current_ema9)
    for key, value in levels.items():
        if isinstance(value, (int, float)):
            combined_levels.setdefault(key, value)

    if latest and current_vwap:
        max_vwap_ext = float(cfg.get("max_extension_from_vwap_pct", 0.6))
        if abs(pct_change(latest.c, current_vwap)) > max_vwap_ext:
            warning = "Do Not Chase: price is too far extended from VWAP"
            if warning not in warnings:
                warnings.append(warning)
    if latest and current_ema9:
        max_ema_ext = float(cfg.get("max_extension_from_ema9_pct", 0.4))
        if abs(pct_change(latest.c, current_ema9)) > max_ema_ext:
            warning = "Do Not Chase: price is too far extended from EMA9"
            if warning not in warnings:
                warnings.append(warning)
    if (recent_volume_multiplier(bars) or 0) < float(cfg.get("volume_confirm_multiplier", 1.5)):
        warning = "Volume confirmation is below strategy threshold"
        if active and warning not in warnings:
            warnings.append(warning)

    score = _combined_score(active, warnings)
    primary = active[0] if active else None
    secondary = [item["label"] for item in active[1:]]
    direction = primary.get("direction") if primary else "neutral"
    confirmation_cfg = config.get("confirmation", {})
    volume_enabled = cfg.get("enable_volume_quality", True) and confirmation_cfg.get("volume_quality", {}).get("enabled", True)
    candle_enabled = cfg.get("enable_candle_strength", True) and confirmation_cfg.get("candle_strength", {}).get("enabled", True)
    retest_enabled = cfg.get("enable_retest_hold", True) and confirmation_cfg.get("retest_hold", {}).get("enabled", True)
    extension_enabled = cfg.get("enable_extension_exhaustion", True) and confirmation_cfg.get("extension_exhaustion", {}).get("enabled", True)
    relative_strength_enabled = cfg.get("enable_relative_strength", True) and confirmation_cfg.get("relative_strength", {}).get("enabled", True)
    market_regime_enabled = cfg.get("enable_market_regime", True) and confirmation_cfg.get("market_regime", {}).get("enabled", True)
    pressure_enabled = cfg.get("enable_pressure_score", False) and confirmation_cfg.get("pressure_score", {}).get("enabled", False)
    volume_quality = evaluate_volume_quality(
        bars,
        config,
        relative_volume=relative_volume,
        direction=direction,
        setup_label=primary.get("label") if primary else None,
    ) if volume_enabled else {
        "volume_score": 0,
        "volume_label": "UNKNOWN",
        "rvol": relative_volume or 0.0,
        "is_volume_confirmed": False,
        "is_volume_exhausted": False,
        "reasons": [],
        "warnings": [],
    }
    if active and volume_enabled:
        if volume_quality.get("is_volume_confirmed"):
            score = min(100, score + 6)
        else:
            score = max(0, score - 8)
        if volume_quality.get("is_volume_exhausted"):
            score = max(0, score - 10)
        for reason in volume_quality.get("reasons") or []:
            if reason not in reasons:
                reasons.append(reason)
        for warning in volume_quality.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
    candle_strength = evaluate_candle_strength(
        bars,
        config,
        direction=direction,
        volume_quality=volume_quality,
        levels=combined_levels,
        setup_label=primary.get("label") if primary else None,
    ) if candle_enabled else {
        "candle_score": 0,
        "candle_label": "UNKNOWN",
        "close_position_pct": 0.0,
        "body_pct_of_range": 0.0,
        "upper_wick_pct": 0.0,
        "lower_wick_pct": 0.0,
        "reasons": [],
        "warnings": [],
    }
    if active and candle_enabled:
        candle_label = candle_strength.get("candle_label")
        if (direction == "bullish" and candle_label == "BUYER_CONTROL") or (direction == "bearish" and candle_label == "SELLER_CONTROL"):
            score = min(100, score + 6)
        elif candle_label in {"INDECISION", "REJECTION"}:
            score = max(0, score - 8)
        elif (direction == "bullish" and candle_label == "SELLER_CONTROL") or (direction == "bearish" and candle_label == "BUYER_CONTROL"):
            score = max(0, score - 10)
        for reason in candle_strength.get("reasons") or []:
            if reason not in reasons:
                reasons.append(reason)
        for warning in candle_strength.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
    retest_hold = evaluate_retest_hold(
        bars,
        config,
        combined_levels,
        direction=direction,
    ) if retest_enabled else {
        "retest_active": False,
        "retest_type": "NONE",
        "level_name": None,
        "level_price": None,
        "distance_from_level_pct": 0.0,
        "score": 0,
        "entry_quality_label": "UNKNOWN",
        "reasons": [],
        "warnings": [],
    }
    entry_quality_label = retest_hold.get("entry_quality_label", "UNKNOWN")
    if active and retest_enabled:
        if retest_hold.get("retest_active"):
            score = min(100, score + 10)
            retest_label = retest_hold.get("label")
            if retest_label and retest_label != (primary.get("label") if primary else None) and retest_label not in secondary:
                secondary.insert(0, retest_label)
        elif entry_quality_label == "LATE":
            score = max(0, score - 5)
        for reason in retest_hold.get("reasons") or []:
            if reason not in reasons:
                reasons.append(reason)
        for warning in retest_hold.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
    extension_exhaustion = evaluate_extension_exhaustion(
        bars,
        config,
        combined_levels,
        direction=direction,
        volume_quality=volume_quality,
        candle_strength=candle_strength,
    ) if extension_enabled else {
        "extension_score": 0,
        "extension_label": "UNKNOWN",
        "distance_from_vwap_pct": 0.0,
        "distance_from_ema9_pct": 0.0,
        "distance_from_key_level_pct": 0.0,
        "consecutive_large_candles": 0,
        "reasons": [],
        "warnings": [],
    }
    if active and extension_enabled:
        extension_label = extension_exhaustion.get("extension_label")
        extension_score = int(extension_exhaustion.get("extension_score") or 0)
        if extension_label == "DO_NOT_CHASE":
            if extension_score >= 95:
                score = max(0, score - 15)
            entry_quality_label = "DO_NOT_CHASE"
        elif extension_label == "VERY_EXTENDED":
            if extension_score >= 75:
                score = max(0, score - 10)
            if entry_quality_label == "UNKNOWN":
                entry_quality_label = "LATE"
        elif extension_label == "EXTENDED":
            if entry_quality_label == "UNKNOWN":
                entry_quality_label = "LATE"
        for reason in extension_exhaustion.get("reasons") or []:
            if reason not in reasons:
                reasons.append(reason)
        for warning in extension_exhaustion.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
    relative_strength = evaluate_relative_strength(
        symbol,
        bars,
        config,
        market_bars,
        direction=direction,
    ) if relative_strength_enabled else {
        "relative_strength_score": 50,
        "relative_strength_label": "UNKNOWN",
        "symbol_change_pct": 0.0,
        "spy_change_pct": 0.0,
        "qqq_change_pct": 0.0,
        "symbol_vs_spy": 0.0,
        "symbol_vs_qqq": 0.0,
        "reasons": [],
        "warnings": [],
    }
    if active and relative_strength_enabled:
        rs_label = relative_strength.get("relative_strength_label")
        if (direction == "bullish" and rs_label == "STRONG") or (direction == "bearish" and rs_label == "WEAK"):
            score = min(100, score + 6)
        elif (direction == "bullish" and rs_label == "WEAK") or (direction == "bearish" and rs_label == "STRONG"):
            score = max(0, score - 8)
        for reason in relative_strength.get("reasons") or []:
            if reason not in reasons:
                reasons.append(reason)
        for warning in relative_strength.get("warnings") or []:
            if warning not in warnings:
                if "relative strength" in warning.lower():
                    warnings.insert(0, warning)
                else:
                    warnings.append(warning)
    market_regime = evaluate_market_regime(
        market_bars,
        config,
        aapl_bars=bars if symbol == "AAPL" else None,
    ) if market_regime_enabled else {
        "market_regime": "UNKNOWN",
        "regime_score": 0,
        "market_score": 0,
        "regime_reason": "Market regime detector disabled",
        "spy_alignment": "UNKNOWN",
        "qqq_alignment": "UNKNOWN",
        "aapl_relative_strength": "UNKNOWN",
        "volume_state": "UNKNOWN",
        "volatility_state": "UNKNOWN",
        "spy_state": {},
        "qqq_state": {},
        "reasons": [],
        "warnings": [],
    }
    if active and market_regime_enabled:
        regime = market_regime.get("market_regime")
        bullish_regimes = {"TRENDING_UP", "OPENING_DRIVE_UP", "BULL_TREND"}
        bearish_regimes = {"TRENDING_DOWN", "OPENING_DRIVE_DOWN", "BEAR_TREND"}
        if (direction == "bullish" and regime in bullish_regimes) or (direction == "bearish" and regime in bearish_regimes):
            score = min(100, score + 6)
        elif regime == "CHOPPY":
            score = max(0, score - 8)
        elif regime in {"RANGE_BOUND", "REVERSAL_ATTEMPT", "LOW_VOLUME_FAKE_MOVE"}:
            score = max(0, score - 4)
        elif (direction == "bullish" and regime in bearish_regimes) or (direction == "bearish" and regime in bullish_regimes):
            score = max(0, score - 8)
            warning = "Market regime is opposing the setup"
            if warning not in warnings:
                warnings.insert(0, warning)
        for reason in market_regime.get("reasons") or []:
            if reason not in reasons:
                reasons.append(reason)
        for warning in market_regime.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
    pressure_score = evaluate_pressure_score(
        pressure_data,
        config,
        direction=direction,
    ) if pressure_enabled else {
        "pressure_score": 50,
        "pressure_label": "UNKNOWN",
        "bid_size": None,
        "ask_size": None,
        "spread": None,
        "trade_near_ask_count": 0,
        "trade_near_bid_count": 0,
        "large_print_count": 0,
        "reasons": [],
        "warnings": [],
    }
    if active and pressure_enabled and pressure_score.get("pressure_label") != "UNKNOWN":
        pressure_label = pressure_score.get("pressure_label")
        if (direction == "bullish" and pressure_label == "BUYERS_ACTIVE") or (direction == "bearish" and pressure_label == "SELLERS_ACTIVE"):
            score = min(100, score + 5)
        elif (direction == "bullish" and pressure_label == "SELLERS_ACTIVE") or (direction == "bearish" and pressure_label == "BUYERS_ACTIVE"):
            score = max(0, score - 6)
        for reason in pressure_score.get("reasons") or []:
            if reason not in reasons:
                reasons.append(reason)
        for warning in pressure_score.get("warnings") or []:
            if warning not in warnings:
                warnings.append(warning)
    confirmation_parts = []
    if volume_enabled:
        confirmation_parts.append(int(volume_quality.get("volume_score", 0)))
    if candle_enabled:
        confirmation_parts.append(int(candle_strength.get("candle_score", 0)))
    if retest_enabled and retest_hold.get("retest_active"):
        confirmation_parts.append(int(retest_hold.get("score", 0)))
    if extension_enabled:
        extension_label = extension_exhaustion.get("extension_label")
        if extension_label not in {"UNKNOWN", None}:
            confirmation_parts.append(max(0, 100 - int(extension_exhaustion.get("extension_score", 0))))
    if relative_strength_enabled and relative_strength.get("relative_strength_label") != "UNKNOWN":
        confirmation_parts.append(int(relative_strength.get("relative_strength_score", 50)))
    if market_regime_enabled and market_regime.get("market_regime") != "UNKNOWN":
        confirmation_parts.append(int(market_regime.get("market_score", 0)))
    if pressure_enabled and pressure_score.get("pressure_label") != "UNKNOWN":
        confirmation_parts.append(int(pressure_score.get("pressure_score", 50)))
    confirmation_score = int(round(sum(confirmation_parts) / len(confirmation_parts))) if confirmation_parts else 0
    confirmation_label = "STRONG" if confirmation_score >= 70 else "WEAK" if confirmation_score < 45 else "NORMAL"
    prioritized_warnings = _prioritized_warnings(warnings)
    final_risk_label = _risk_label(score, prioritized_warnings)
    if active and entry_quality_label == "UNKNOWN":
        extension_label = extension_exhaustion.get("extension_label")
        if final_risk_label == "DO_NOT_CHASE":
            entry_quality_label = "DO_NOT_CHASE"
        elif extension_label in {"EXTENDED", "VERY_EXTENDED"}:
            entry_quality_label = "LATE"
        elif retest_hold.get("retest_active"):
            entry_quality_label = "GOOD_POSITION"
        else:
            entry_quality_label = "EARLY"

    scenario_summary = evaluate_scenario_suite(
        symbol,
        bars,
        latest,
        config,
        levels,
        relative_volume,
        market_alignment,
        market_bars,
        option_context=option_context,
        phase1_summary=phase1_summary
        or {
            "confidence_score": score,
            "confidence_label": confidence_label(score),
            "risk_label": final_risk_label,
            "direction": direction,
        },
        phase2_summary=phase2_summary
        or {
            "confirmation_score": confirmation_score,
            "confirmation_label": confirmation_label,
            "candle_label": candle_strength.get("candle_label"),
            "volume_label": volume_quality.get("volume_label"),
            "market_regime": market_regime.get("market_regime"),
            "entry_quality_label": entry_quality_label,
        },
    )
    phase1_for_classifier = {
        "primary_setup": primary.get("label") if primary else None,
        "direction": direction,
        "confidence_score": score,
        "confidence_label": confidence_label(score),
        "entry_quality_label": entry_quality_label,
        "risk_label": final_risk_label,
        "reasons": reasons,
    }
    professional_setup = classify_professional_setup(
        phase1_for_classifier,
        scenario_summary,
        market_alignment=market_alignment,
    )
    return {
        "symbol": symbol,
        "primary_setup": primary.get("label") if primary else None,
        "secondary_setups": secondary,
        "direction": direction,
        "confidence_score": score,
        "confidence_label": confidence_label(score),
        "risk_label": final_risk_label,
        "confirmation_score": confirmation_score,
        "confirmation_label": confirmation_label,
        "entry_quality_label": entry_quality_label,
        "volume_label": volume_quality.get("volume_label", "UNKNOWN"),
        "rvol": volume_quality.get("rvol", relative_volume or 0.0),
        "candle_label": candle_strength.get("candle_label", "UNKNOWN"),
        "candle_score": candle_strength.get("candle_score", 0),
        "extension_label": extension_exhaustion.get("extension_label", "UNKNOWN"),
        "extension_score": extension_exhaustion.get("extension_score", 0),
        "relative_strength_label": relative_strength.get("relative_strength_label", "UNKNOWN"),
        "relative_strength_score": relative_strength.get("relative_strength_score", 50),
        "market_regime": market_regime.get("market_regime", "UNKNOWN"),
        "regime_score": market_regime.get("regime_score", market_regime.get("market_score", 0)),
        "market_score": market_regime.get("market_score", 0),
        "regime_reason": market_regime.get("regime_reason"),
        "spy_alignment": market_regime.get("spy_alignment", "UNKNOWN"),
        "qqq_alignment": market_regime.get("qqq_alignment", "UNKNOWN"),
        "aapl_relative_strength": market_regime.get("aapl_relative_strength", "UNKNOWN"),
        "volume_state": market_regime.get("volume_state", "UNKNOWN"),
        "volatility_state": market_regime.get("volatility_state", "UNKNOWN"),
        "pressure_label": pressure_score.get("pressure_label", "UNKNOWN"),
        "pressure_score": pressure_score.get("pressure_score", 50),
        "scenario_top": scenario_summary.get("top_scenario"),
        "scenario_second": scenario_summary.get("second_scenario"),
        "scenario_score": scenario_summary.get("scenario_score"),
        "scenario_stage": scenario_summary.get("scenario_stage"),
        "scenario_direction": scenario_summary.get("scenario_direction"),
        "scenario_confidence_label": scenario_summary.get("scenario_confidence_label"),
        "scenario_entry_quality_label": scenario_summary.get("scenario_entry_quality_label"),
        "scenario_risk_label": scenario_summary.get("scenario_risk_label"),
        "scenario_alert_tier": scenario_summary.get("scenario_alert_tier"),
        "scenario_alert_block_reason": scenario_summary.get("scenario_alert_block_reason"),
        "scenario_reasons": scenario_summary.get("scenario_reasons", []),
        "scenario_warnings": scenario_summary.get("scenario_warnings", []),
        "scenario_levels": scenario_summary.get("scenario_levels", {}),
        "vwap": scenario_summary.get("vwap"),
        "ema9": scenario_summary.get("ema9"),
        "ema20": scenario_summary.get("ema20"),
        "bullish_score": scenario_summary.get("bullish_score", 0),
        "bearish_score": scenario_summary.get("bearish_score", 0),
        "chop_score": scenario_summary.get("chop_score", 0),
        "fakeout_score": scenario_summary.get("fakeout_score", 0),
        "scenario_conflict": scenario_summary.get("scenario_conflict", False),
        "all_scenarios": scenario_summary.get("all_scenarios", []),
        "stock_setup_score": scenario_summary.get("stock_setup_score", score),
        "stock_setup_score_reason": scenario_summary.get("stock_setup_score_reason"),
        "stock_setup_valid": scenario_summary.get("stock_setup_valid", score >= 60),
        "option_tradability_score": scenario_summary.get("option_tradability_score"),
        "option_feed_status": scenario_summary.get("option_feed_status", "UNAVAILABLE"),
        "option_tradable": scenario_summary.get("option_tradable", False),
        "sms_allowed_by_stock": scenario_summary.get("sms_allowed_by_stock", False),
        "sms_allowed_by_options": scenario_summary.get("sms_allowed_by_options", True),
        "sms_block_reason": scenario_summary.get("sms_block_reason", ""),
        "scenario_alert_eligible": scenario_summary.get("scenario_alert_eligible", False),
        "scenario_would_sms": scenario_summary.get("scenario_would_sms", False),
        "scenario_sms_allowed": scenario_summary.get("scenario_sms_allowed", False),
        "scenario_sms_block_reason": scenario_summary.get("scenario_sms_block_reason", ""),
        "professional_setup": professional_setup,
        "setup_name": professional_setup.get("setup_name"),
        "setup_code": professional_setup.get("setup_code"),
        "setup_stage": professional_setup.get("stage"),
        "setup_score": professional_setup.get("score"),
        "setup_confidence": professional_setup.get("confidence"),
        "setup_reason": professional_setup.get("reason"),
        "setup_invalidation_level": professional_setup.get("invalidation_level"),
        "setup_entry_quality": professional_setup.get("entry_quality"),
        "setup_risk_label": professional_setup.get("risk_label"),
        "setup_watch_text": professional_setup.get("watch_text"),
        "setup_block_reason": professional_setup.get("block_reason"),
        "setup_direction": professional_setup.get("direction"),
        "candle_strength": candle_strength,
        "extension_exhaustion": extension_exhaustion,
        "market_regime_detail": market_regime,
        "pressure_detail": pressure_score,
        "relative_strength": relative_strength,
        "retest_hold": retest_hold,
        "volume_quality": volume_quality,
        "reasons": reasons[:8],
        "warnings": prioritized_warnings[:8],
        "levels": combined_levels,
        "strategy_results": result_dicts,
    }
