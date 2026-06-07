from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from ..base import StrategyContext, confidence_label, ema, pct_change, recent_swing_high, recent_swing_low, recent_volume_multiplier, vwap
from .scenario_types import ScenarioResult
from .staging import entry_quality_from_stage, risk_from_stage, stage_from_score


def _bar_times_in_minutes_since_open(latest: Any) -> Optional[float]:
    if not latest or not getattr(latest, "t", None):
        return None
    local = latest.t.astimezone()
    open_dt = local.replace(hour=9, minute=30, second=0, microsecond=0)
    return (local - open_dt).total_seconds() / 60.0


def _bar_times_in_minutes_since_close(latest: Any) -> Optional[float]:
    if not latest or not getattr(latest, "t", None):
        return None
    local = latest.t.astimezone()
    close_dt = local.replace(hour=16, minute=0, second=0, microsecond=0)
    return (close_dt - local).total_seconds() / 60.0


def _is_bullish_close(bar: Any) -> bool:
    return bar.c >= bar.o


def _is_bearish_close(bar: Any) -> bool:
    return bar.c <= bar.o


def _range_position_pct(bar: Any) -> float:
    rng = max((bar.h - bar.l), 1e-9)
    return ((bar.c - bar.l) / rng) * 100.0


def _wick_stats(bar: Any) -> tuple[float, float, float]:
    rng = max((bar.h - bar.l), 1e-9)
    upper = max(bar.h - max(bar.o, bar.c), 0.0) / rng * 100.0
    lower = max(min(bar.o, bar.c) - bar.l, 0.0) / rng * 100.0
    body = abs(bar.c - bar.o) / rng * 100.0
    return body, upper, lower


def _volume_label(score: float) -> str:
    if score >= 3.5:
        return "CLIMAX"
    if score >= 2.0:
        return "STRONG"
    if score >= 1.2:
        return "NORMAL"
    return "WEAK"


def _extension_from_level(price: float, level: Optional[float]) -> float:
    if level is None or level <= 0:
        return 0.0
    return abs(pct_change(price, level))


def _current_extension_pct(latest: Any, level: Optional[float]) -> float:
    if latest is None or level is None or level <= 0:
        return 0.0
    price = getattr(latest, "c", None)
    if price is None or price <= 0:
        return 0.0
    return abs(pct_change(price, level))


def _stock_setup_score_reason(
    primary: Optional[ScenarioResult],
    active: List[ScenarioResult],
    score: int,
    stage: str,
    warnings: List[str],
) -> str:
    if not active:
        return "No active stock setup"
    primary_name = primary.scenario_name if primary else "Stock setup"
    if score >= 75:
        return f"{primary_name} is strong"
    if score >= 60:
        return f"{primary_name} is developing but still needs cleaner confirmation"
    if score >= 40:
        return f"{primary_name} is building, but confirmation is mixed"
    detail_bits: List[str] = []
    for item in active:
        detail_bits.extend((item.reasons or [])[:2])
        detail_bits.extend((item.warnings or [])[:2])
        if len(detail_bits) >= 4:
            break
    if not detail_bits:
        detail_bits.append(f"{primary_name} remains weak")
    if stage in {"GOOD_POSITION", "CONFIRMED"}:
        detail_bits.append(f"stage is {stage.lower()} but broader stock evidence is still thin")
    return "; ".join(dict.fromkeys(detail_bits))[:220]


def _level_list(*groups: Optional[Dict[str, Optional[float]]]) -> List[float]:
    values: List[float] = []
    for group in groups:
        if not group:
            continue
        for value in group.values():
            if isinstance(value, (int, float)) and value > 0:
                values.append(float(value))
    return values


def _near_any_level(price: float, levels: List[float], pct: float = 0.35) -> bool:
    return any(_extension_from_level(price, level) <= pct for level in levels)


def _fresh_pullback_context(
    top: ScenarioResult,
    latest: Any,
    *,
    levels: Dict[str, Optional[float]],
    current_levels: Dict[str, Optional[float]],
) -> bool:
    if not latest or top.direction not in {"bullish", "bearish"}:
        return False
    price = getattr(latest, "c", None)
    if price is None or price <= 0:
        return False
    level_values = _level_list(levels, current_levels, top.levels)
    if not level_values or not _near_any_level(price, level_values):
        return False
    text_bits = " ".join([top.scenario_name, *top.reasons, *top.warnings]).lower()
    if not any(token in text_bits for token in ("pullback", "reclaim", "rejection", "retest", "hold", "breakout", "breakdown")):
        return False
    if top.direction == "bullish":
        candle_ok = latest.c >= latest.o or _is_bullish_close(latest)
    else:
        candle_ok = latest.c <= latest.o or _is_bearish_close(latest)
    return candle_ok


def _apply_phase2_stage_consistency(
    top: ScenarioResult,
    *,
    latest: Any,
    levels: Dict[str, Optional[float]],
    current_levels: Dict[str, Optional[float]],
    phase2_entry_quality: str,
) -> Optional[str]:
    phase2_entry_quality = phase2_entry_quality.upper()
    if phase2_entry_quality == "DO_NOT_CHASE":
        if top.stage != "DO_NOT_CHASE":
            top.stage = "DO_NOT_CHASE"
            top.entry_quality_label = entry_quality_from_stage(top.stage)
            top.risk_label = risk_from_stage(top.stage, top.warnings)
            top.warnings = list(dict.fromkeys(top.warnings + ["Stage forced to DO_NOT_CHASE because Phase 2 entry quality is DO_NOT_CHASE"]))
            return "Stage forced to DO_NOT_CHASE because Phase 2 entry quality is DO_NOT_CHASE"
        return None

    if phase2_entry_quality != "LATE":
        return None

    fresh_pullback = _fresh_pullback_context(top, latest, levels=levels, current_levels=current_levels)
    new_stage = top.stage
    if top.stage == "GOOD_POSITION":
        new_stage = "CONFIRMED" if fresh_pullback else "LATE"
    elif top.stage == "CONFIRMED" and not fresh_pullback:
        new_stage = "LATE"
    if new_stage != top.stage:
        top.stage = new_stage
        top.entry_quality_label = entry_quality_from_stage(top.stage)
        top.risk_label = risk_from_stage(top.stage, top.warnings)
        warning = "Stage downgraded because Phase 2 entry quality is LATE"
        top.warnings = list(dict.fromkeys(top.warnings + [warning]))
        return warning
    return None


def _phase3_alert_tier(
    *,
    top: ScenarioResult,
    scenario_conflict: bool,
    direction_conflict: bool,
    phase2_candle_label: str,
    phase2_confirmation_score: int,
    phase2_entry_quality: str,
    extended_from_vwap: bool,
    extended_from_ema: bool,
    would_sms: bool,
) -> str:
    if top.stage in {"WATCHING", "FORMING"}:
        return "WATCH_ONLY"
    blocked = (
        scenario_conflict
        or direction_conflict
        or top.stage in {"LATE", "DO_NOT_CHASE", "INVALIDATED"}
        or top.risk_label in {"HIGH", "DO_NOT_CHASE"}
        or top.entry_quality_label in {"LATE", "DO_NOT_CHASE"}
        or phase2_confirmation_score < 60
        or phase2_entry_quality in {"LATE", "DO_NOT_CHASE"}
        or extended_from_vwap
        or extended_from_ema
    )
    if blocked:
        return "BLOCKED"
    if would_sms:
        return "WOULD_SMS"
    if top.stage in {"CONFIRMED", "GOOD_POSITION"}:
        return "DASHBOARD_ALERT"
    return "WATCH_ONLY"


def _phase3_alert_block_reason(
    *,
    tier: str,
    top: ScenarioResult,
    scenario_conflict: bool,
    direction_conflict: bool,
    stage_downgrade_reason: str,
    phase2_candle_label: str,
    phase2_confirmation_score: int,
    phase2_entry_quality: str,
    extended_from_vwap: bool,
    extended_from_ema: bool,
) -> str:
    reasons: List[str] = []
    if tier == "WOULD_SMS":
        return ""
    if tier == "WATCH_ONLY":
        reasons.append(f"stage is {top.stage}")
    if scenario_conflict:
        reasons.append("scenario conflict")
    if direction_conflict:
        reasons.append("Scenario conflict: current direction does not match top scenario")
    if stage_downgrade_reason:
        reasons.append(stage_downgrade_reason)
    if top.stage in {"LATE", "DO_NOT_CHASE", "INVALIDATED"}:
        reasons.append(f"stage is {top.stage}")
    if top.risk_label in {"HIGH", "DO_NOT_CHASE"}:
        reasons.append(f"risk is {top.risk_label}")
    if top.entry_quality_label in {"LATE", "DO_NOT_CHASE"}:
        reasons.append(f"entry quality is {top.entry_quality_label}")
    if phase2_confirmation_score < 60:
        reasons.append("confirmation score below 60")
    if phase2_entry_quality in {"LATE", "DO_NOT_CHASE"}:
        reasons.append(f"phase 2 entry quality is {phase2_entry_quality}")
    if phase2_candle_label in {"INDECISION", "REJECTION"}:
        reasons.append(f"candle is {phase2_candle_label}")
    if extended_from_vwap or extended_from_ema:
        reasons.append("price extended from VWAP/EMA9")
    if "reclaim" in top.scenario_name.lower() and any("reject" in warning.lower() for warning in top.warnings):
        reasons.append("Bullish reclaim rejected: candle closed against setup")
    if tier == "DASHBOARD_ALERT" and not reasons:
        return "Dashboard alert only"
    return "; ".join(dict.fromkeys(reasons))


def _scenario(direction: str, name: str, score: int, reasons: List[str], warnings: List[str], levels: Dict[str, float], *, invalidation_level: Optional[float] = None, invalidation_reason: str = "", confirmed: bool = False, good_position: bool = False, late: bool = False, do_not_chase: bool = False, invalidated: bool = False) -> ScenarioResult:
    stage = stage_from_score(score, confirmed=confirmed, good_position=good_position, late=late, do_not_chase=do_not_chase, invalidated=invalidated)
    entry_quality = entry_quality_from_stage(stage)
    risk = risk_from_stage(stage, warnings)
    return ScenarioResult(
        scenario_name=name,
        direction=direction,
        score=int(max(0, min(100, round(score)))),
        stage=stage,
        confidence_label=confidence_label(score),
        entry_quality_label=entry_quality,
        risk_label=risk,
        reasons=reasons[:6],
        warnings=warnings[:6],
        invalidation_level=invalidation_level,
        invalidation_reason=invalidation_reason,
        levels={k: v for k, v in levels.items() if isinstance(v, (int, float))},
    )


def _base_metrics(ctx: StrategyContext) -> Dict[str, Any]:
    latest = ctx.latest
    closes = [bar.c for bar in ctx.bars]
    vw = vwap(ctx.bars)
    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    rv = ctx.relative_volume or recent_volume_multiplier(ctx.bars) or 0.0
    body = upper = lower = 0.0
    if latest:
        body, upper, lower = _wick_stats(latest)
    return {
        "latest": latest,
        "vwap": vw,
        "ema9": ema9,
        "ema20": ema20,
        "rv": rv,
        "body_pct": body,
        "upper_wick_pct": upper,
        "lower_wick_pct": lower,
        "close_pos_pct": _range_position_pct(latest) if latest else 0.0,
        "minutes_from_open": _bar_times_in_minutes_since_open(latest),
        "minutes_to_close": _bar_times_in_minutes_since_close(latest),
        "recent_high": recent_swing_high(ctx.bars),
        "recent_low": recent_swing_low(ctx.bars),
    }


def evaluate_bullish_trend_continuation(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Bullish Trend Continuation", 0, [], [], {})
    score = 18
    reasons: List[str] = []
    warnings: List[str] = []
    levels = {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}
    above_vwap = m["vwap"] is not None and latest.c >= m["vwap"]
    above_ema9 = m["ema9"] is not None and latest.c >= m["ema9"]
    ema_rising = len(ctx.bars) >= 3 and (ema([b.c for b in ctx.bars[-3:]], 3) or 0) >= (ema([b.c for b in ctx.bars[-5:-2]], 3) or 0)
    higher_low = latest.l >= min((b.l for b in ctx.bars[-5:-1]), default=latest.l)
    higher_high = latest.h >= max((b.h for b in ctx.bars[-5:-1]), default=latest.h)
    if above_vwap:
        score += 15
        reasons.append("Price above VWAP")
    else:
        warnings.append("Price is below VWAP")
    if above_ema9:
        score += 14
        reasons.append("Price above EMA9")
    else:
        warnings.append("Price is below EMA9")
    if ema_rising:
        score += 10
        reasons.append("EMA9 is rising")
    if higher_low:
        score += 12
        reasons.append("Higher low forming")
    if higher_high:
        score += 6
        reasons.append("Price pressing recent highs")
    if m["rv"] >= 1.5:
        score += 8
        reasons.append("Volume confirms the continuation")
    elif m["rv"] < 1.2:
        score -= 8
        warnings.append("Volume confirmation is weak")
    if market_context in {"OPPOSED"}:
        score -= 12
        warnings.append("Market context is opposing the setup")
    elif market_context in {"ALIGNED", "MIXED"}:
        reasons.append("SPY/QQQ not opposing")
    ext_vwap = _extension_from_level(latest.c, m["vwap"])
    ext_ema = _extension_from_level(latest.c, m["ema9"])
    if ext_vwap > 0.6 or ext_ema > 0.4:
        score -= 12
        warnings.append("Move extended from VWAP/EMA9")
    if m["close_pos_pct"] >= 65 and _is_bullish_close(latest):
        score += 6
        reasons.append("Candle closed near the highs")
    if latest.l <= (m["ema9"] or latest.l) <= latest.h or latest.l <= (m["vwap"] or latest.l) <= latest.h:
        score += 6
        reasons.append("Pullback held a logical support level")
    good_position = bool((above_vwap or above_ema9) and (higher_low or latest.l <= max(m["ema9"] or latest.l, m["vwap"] or latest.l) * 1.002))
    if good_position:
        reasons.append("Entry remains near support")
    late = ext_vwap > 0.6 or ext_ema > 0.4 or m["rv"] >= 3.0
    do_not_chase = ext_vwap > 1.2 or ext_ema > 0.9
    invalidated = latest.c < min(filter(None, [m["vwap"], m["ema9"], m["recent_low"]]), default=latest.c)
    return _scenario("bullish", "Bullish Trend Continuation", score, reasons, warnings, levels, invalidation_level=min(filter(None, [m["vwap"], m["ema9"], m["recent_low"]]), default=None), invalidation_reason="Lost VWAP/EMA9 or recent swing low", confirmed=above_vwap and above_ema9 and _is_bullish_close(latest), good_position=good_position, late=late, do_not_chase=do_not_chase, invalidated=invalidated)


def evaluate_bearish_trend_continuation(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Bearish Trend Continuation", 0, [], [], {})
    score = 18
    reasons: List[str] = []
    warnings: List[str] = []
    levels = {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}
    below_vwap = m["vwap"] is not None and latest.c <= m["vwap"]
    below_ema9 = m["ema9"] is not None and latest.c <= m["ema9"]
    ema_falling = len(ctx.bars) >= 3 and (ema([b.c for b in ctx.bars[-3:]], 3) or 0) <= (ema([b.c for b in ctx.bars[-5:-2]], 3) or 0)
    lower_high = latest.h <= max((b.h for b in ctx.bars[-5:-1]), default=latest.h)
    lower_low = latest.l <= min((b.l for b in ctx.bars[-5:-1]), default=latest.l)
    if below_vwap:
        score += 15
        reasons.append("Price below VWAP")
    else:
        warnings.append("Price is above VWAP")
    if below_ema9:
        score += 14
        reasons.append("Price below EMA9")
    else:
        warnings.append("Price is above EMA9")
    if ema_falling:
        score += 10
        reasons.append("EMA9 is falling")
    if lower_high:
        score += 12
        reasons.append("Lower high forming")
    if lower_low:
        score += 6
        reasons.append("Price pressing recent lows")
    if m["rv"] >= 1.5:
        score += 8
        reasons.append("Volume confirms the continuation")
    elif m["rv"] < 1.2:
        score -= 8
        warnings.append("Volume confirmation is weak")
    if market_context in {"OPPOSED"}:
        score -= 12
        warnings.append("Market context is opposing the setup")
    elif market_context in {"ALIGNED", "MIXED"}:
        reasons.append("SPY/QQQ not opposing")
    ext_vwap = _extension_from_level(latest.c, m["vwap"])
    ext_ema = _extension_from_level(latest.c, m["ema9"])
    if ext_vwap > 0.6 or ext_ema > 0.4:
        score -= 12
        warnings.append("Move extended from VWAP/EMA9")
    if m["close_pos_pct"] <= 35 and _is_bearish_close(latest):
        score += 6
        reasons.append("Candle closed near the lows")
    if latest.h >= (m["ema9"] or latest.h) >= latest.l or latest.h >= (m["vwap"] or latest.h) >= latest.l:
        score += 6
        reasons.append("Pullback rejected a logical resistance level")
    good_position = bool((below_vwap or below_ema9) and (lower_high or latest.h >= min(m["ema9"] or latest.h, m["vwap"] or latest.h) * 0.998))
    if good_position:
        reasons.append("Entry remains near resistance")
    late = ext_vwap > 0.6 or ext_ema > 0.4 or m["rv"] >= 3.0
    do_not_chase = ext_vwap > 1.2 or ext_ema > 0.9
    invalidated = latest.c > max(filter(None, [m["vwap"], m["ema9"], m["recent_high"]]), default=latest.c)
    return _scenario("bearish", "Bearish Trend Continuation", score, reasons, warnings, levels, invalidation_level=max(filter(None, [m["vwap"], m["ema9"], m["recent_high"]]), default=None), invalidation_reason="Lost VWAP/EMA9 or recent swing high", confirmed=below_vwap and below_ema9 and _is_bearish_close(latest), good_position=good_position, late=late, do_not_chase=do_not_chase, invalidated=invalidated)


def evaluate_bullish_vwap_reclaim_continuation(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest or m["vwap"] is None:
        return _scenario("neutral", "Bullish VWAP/EMA Reclaim Continuation", 0, [], [], {})
    prev = ctx.bars[-2] if len(ctx.bars) >= 2 else latest
    score = 28
    reasons: List[str] = []
    warnings: List[str] = []
    levels = {"vwap": m["vwap"], "ema9": m["ema9"] or 0.0}
    reclaimed = prev.c < m["vwap"] and latest.c >= m["vwap"]
    body_pct, upper_wick_pct, lower_wick_pct = _wick_stats(latest)
    bullish_close = _is_bullish_close(latest)
    strong_bullish_close = bullish_close and _range_position_pct(latest) >= 55 and body_pct >= 35 and upper_wick_pct <= 35
    rejected = not strong_bullish_close or (market_context == "OPPOSED") or (m["rv"] < 1.2)
    if reclaimed and rejected:
        return _scenario(
            "neutral",
            "Bullish VWAP/EMA Reclaim Continuation",
            0,
            [],
            ["Bullish reclaim rejected: candle closed against setup"],
            levels,
        )
    if reclaimed:
        score += 22
        reasons.append("Price reclaimed VWAP")
    if latest.c < latest.o:
        score -= 12
        warnings.append("Bullish reclaim rejected: candle closed against setup")
    if bullish_close and latest.c >= (m["ema9"] or latest.c):
        score += 10
        reasons.append("Price holds above EMA9")
    if latest.c >= (m["ema20"] or latest.c):
        score += 4
        reasons.append("Price holds above EMA20")
    if m["ema9"] and len(ctx.bars) >= 4 and ema([b.c for b in ctx.bars[-4:]], 3) >= ema([b.c for b in ctx.bars[-6:-2]], 3):
        score += 8
        reasons.append("EMA9 is curling upward")
    if bullish_close and latest.l >= prev.l * 0.999:
        score += 8
        reasons.append("Higher low formed after reclaim")
    if bullish_close and latest.c > (m["vwap"] or latest.c) and latest.c > (m["ema9"] or latest.c):
        score += 6
        reasons.append("VWAP and EMA9 are both reclaimed")
    if m["rv"] >= 1.5:
        score += 8
        reasons.append("Volume improves on the reclaim")
    else:
        warnings.append("Volume confirmation is not strong enough")
    if market_context == "OPPOSED":
        score -= 10
        warnings.append("Market context is opposing the reclaim")
    if latest.c < (m["recent_high"] or latest.c) and latest.h >= (m["recent_high"] or latest.h):
        score -= 8
        warnings.append("Failed reclaim above recent high")
    if _extension_from_level(latest.c, m["vwap"]) > 0.6:
        score -= 8
        warnings.append("Move extended from VWAP")
    if bullish_close and latest.c >= m["vwap"] and latest.c >= (m["ema9"] or latest.c):
        reasons.append("Entry still near logical support")
    return _scenario("bullish", "Bullish VWAP/EMA Reclaim Continuation", score, reasons, warnings, levels, invalidation_level=m["vwap"], invalidation_reason="Lost VWAP reclaim support", confirmed=reclaimed and strong_bullish_close, good_position=reclaimed and strong_bullish_close and latest.c <= (m["vwap"] or latest.c) * 1.01, late=_extension_from_level(latest.c, m["vwap"]) > 0.7, do_not_chase=_extension_from_level(latest.c, m["vwap"]) > 1.2)


def evaluate_bullish_reclaim_attempt_failing(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest or m["vwap"] is None:
        return _scenario("bearish", "Failed VWAP/EMA Reclaim", 0, [], [], {})
    prev = ctx.bars[-2] if len(ctx.bars) >= 2 else latest
    body_pct, upper_wick_pct, lower_wick_pct = _wick_stats(latest)
    recent_high = m["recent_high"] or None
    attempted_reclaim = prev.c < m["vwap"] and latest.h >= m["vwap"]
    if recent_high:
        attempted_reclaim = attempted_reclaim or latest.h >= recent_high * 0.999
    upper_wick_rejection = upper_wick_pct >= 35 and (
        latest.c < latest.o
        or latest.c < m["vwap"]
        or market_context == "OPPOSED"
        or m["rv"] < 1.2
    )
    failure_signals = (
        latest.c < latest.o
        or upper_wick_rejection
        or latest.c < m["vwap"]
        or market_context == "OPPOSED"
        or m["rv"] < 1.2
    )
    if not attempted_reclaim or not failure_signals:
        return _scenario("bearish", "Failed VWAP/EMA Reclaim", 0, [], [], {})
    score = 76
    reasons: List[str] = []
    warnings: List[str] = []
    levels = {"vwap": m["vwap"], "ema9": m["ema9"] or 0.0, "recent_high": recent_high or 0.0}
    if latest.c < latest.o:
        score += 10
        reasons.append("Candle closed red against the reclaim")
    if upper_wick_pct >= 35:
        score += 12
        warnings.append("Upper wick shows rejection")
    if latest.c < m["vwap"]:
        score += 10
        warnings.append("Price lost VWAP after the reclaim attempt")
    if recent_high and latest.c < recent_high:
        score += 8
        reasons.append("Sweep above recent high failed")
    if m["rv"] < 1.2:
        score += 8
        warnings.append("Volume did not confirm the reclaim")
    if market_context == "OPPOSED":
        score += 8
        warnings.append("SPY/QQQ are not confirming the reclaim")
    if latest.c < (m["ema9"] or latest.c):
        warnings.append("Price lost EMA9 during the attempt")
    if body_pct < 30:
        warnings.append("Candle body is too weak to confirm the reclaim")
    if "Bullish reclaim rejected: candle closed against setup" not in warnings:
        warnings.append("Bullish reclaim rejected: candle closed against setup")
    score = min(score, 84)
    return _scenario("bearish", "Failed VWAP/EMA Reclaim", score, reasons or ["Reclaim attempt failed to hold the level"], list(dict.fromkeys(warnings)), levels, confirmed=False, good_position=False, late=False)


def evaluate_bearish_vwap_rejection_continuation(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest or m["vwap"] is None:
        return _scenario("neutral", "Bearish VWAP/EMA Rejection Continuation", 0, [], [], {})
    prev = ctx.bars[-2] if len(ctx.bars) >= 2 else latest
    score = 28
    reasons: List[str] = []
    warnings: List[str] = []
    levels = {"vwap": m["vwap"], "ema9": m["ema9"] or 0.0}
    rejected = prev.c > m["vwap"] and latest.c <= m["vwap"]
    if rejected:
        score += 22
        reasons.append("Price failed at VWAP")
    if latest.c <= (m["ema9"] or latest.c):
        score += 10
        reasons.append("Price remains below EMA9")
    if latest.c <= (m["ema20"] or latest.c):
        score += 4
        reasons.append("Price remains below EMA20")
    if m["ema9"] and len(ctx.bars) >= 4 and ema([b.c for b in ctx.bars[-4:]], 3) <= ema([b.c for b in ctx.bars[-6:-2]], 3):
        score += 8
        reasons.append("EMA9 is curling downward")
    if latest.h <= prev.h * 1.001:
        score += 8
        reasons.append("Lower high formed after rejection")
    if m["rv"] >= 1.5:
        score += 8
        reasons.append("Volume confirms the rejection")
    else:
        warnings.append("Volume confirmation is not strong enough")
    if market_context == "OPPOSED":
        score -= 10
        warnings.append("Market context is opposing the rejection")
    if _extension_from_level(latest.c, m["vwap"]) > 0.6:
        score -= 8
        warnings.append("Move extended from VWAP")
    if latest.c <= m["vwap"] and latest.c <= (m["ema9"] or latest.c):
        reasons.append("Entry still near logical resistance")
    return _scenario("bearish", "Bearish VWAP/EMA Rejection Continuation", score, reasons, warnings, levels, invalidation_level=m["vwap"], invalidation_reason="Lost rejection at VWAP", confirmed=rejected and _is_bearish_close(latest), good_position=rejected and latest.c >= (m["vwap"] or latest.c) * 0.99, late=_extension_from_level(latest.c, m["vwap"]) > 0.7, do_not_chase=_extension_from_level(latest.c, m["vwap"]) > 1.2)


def evaluate_opening_drive_up(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Opening Drive Up", 0, [], [], {})
    mins = m["minutes_from_open"] or 999.0
    score = 15 if mins <= 30 else 5
    reasons: List[str] = []
    warnings: List[str] = []
    if mins <= 30:
        reasons.append("Within opening drive window")
    if latest.c >= (m["vwap"] or latest.c):
        score += 14
        reasons.append("Price above VWAP")
    if latest.c >= (m["ema9"] or latest.c):
        score += 12
        reasons.append("Price above EMA9")
    if m["rv"] >= 1.5:
        score += 12
        reasons.append("Relative volume is supportive")
    if _is_bullish_close(latest):
        score += 8
        reasons.append("Candle closed strong")
    if market_context == "OPPOSED":
        score -= 10
        warnings.append("Market context is not confirming")
    if _extension_from_level(latest.c, m["vwap"]) > 0.8:
        score -= 8
        warnings.append("Opening move is extended")
    return _scenario("bullish", "Opening Drive Up", score, reasons, warnings, {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, confirmed=False, good_position=mins <= 30 and m["rv"] >= 1.5 and _is_bullish_close(latest), late=_extension_from_level(latest.c, m["vwap"]) > 0.8, do_not_chase=_extension_from_level(latest.c, m["vwap"]) > 1.2)


def evaluate_opening_dump(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Opening Dump", 0, [], [], {})
    mins = m["minutes_from_open"] or 999.0
    score = 15 if mins <= 30 else 5
    reasons: List[str] = []
    warnings: List[str] = []
    if mins <= 30:
        reasons.append("Within opening drive window")
    if latest.c <= (m["vwap"] or latest.c):
        score += 14
        reasons.append("Price below VWAP")
    if latest.c <= (m["ema9"] or latest.c):
        score += 12
        reasons.append("Price below EMA9")
    if m["rv"] >= 1.5:
        score += 12
        reasons.append("Relative volume is supportive")
    if _is_bearish_close(latest):
        score += 8
        reasons.append("Candle closed weak")
    if market_context == "OPPOSED":
        score -= 10
        warnings.append("Market context is not confirming")
    if _extension_from_level(latest.c, m["vwap"]) > 0.8:
        score -= 8
        warnings.append("Opening move is extended")
    return _scenario("bearish", "Opening Dump", score, reasons, warnings, {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, confirmed=False, good_position=mins <= 30 and m["rv"] >= 1.5 and _is_bearish_close(latest), late=_extension_from_level(latest.c, m["vwap"]) > 0.8, do_not_chase=_extension_from_level(latest.c, m["vwap"]) > 1.2)


def evaluate_opening_fakeout_risk(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Opening Fakeout Risk", 0, [], [], {})
    score = 20
    reasons: List[str] = []
    warnings: List[str] = []
    if m["upper_wick_pct"] >= 35 or m["lower_wick_pct"] >= 35:
        score += 18
        warnings.append("Large wick rejection near the opening move")
    if m["rv"] >= 1.5:
        score += 10
        warnings.append("Volume spike can still fail if follow-through is absent")
    if market_context == "OPPOSED":
        score += 8
        warnings.append("Market is not confirming the opening push")
    if latest.c < (m["vwap"] or latest.c) and latest.c > (m["ema9"] or latest.c):
        reasons.append("Price slipped back inside the opening drive area")
    return _scenario("neutral", "Opening Fakeout Risk", score, reasons, warnings, {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, confirmed=False, good_position=False, late=True)


def evaluate_closing_ramp(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Closing Ramp", 0, [], [], {})
    mins_to_close = m["minutes_to_close"] or 999.0
    score = 10 if mins_to_close <= 60 else 0
    reasons: List[str] = []
    warnings: List[str] = []
    if mins_to_close <= 60:
        reasons.append("Late-day window")
    if latest.c >= (m["vwap"] or latest.c):
        score += 14
        reasons.append("Price above VWAP")
    if latest.c >= (m["ema9"] or latest.c):
        score += 12
        reasons.append("Price above EMA9")
    if m["rv"] >= 1.3:
        score += 10
        reasons.append("Volume supports the close")
    if _is_bullish_close(latest):
        score += 8
        reasons.append("Candle closed strong")
    if _extension_from_level(latest.c, m["vwap"]) > 0.8:
        warnings.append("Move may be late in the session")
    return _scenario("bullish", "Closing Ramp", score, reasons, warnings, {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, confirmed=mins_to_close <= 60 and _is_bullish_close(latest), good_position=mins_to_close <= 30 and _is_bullish_close(latest), late=_extension_from_level(latest.c, m["vwap"]) > 0.8, do_not_chase=_extension_from_level(latest.c, m["vwap"]) > 1.2)


def evaluate_closing_dump(ctx: StrategyContext, market_context: str = "UNKNOWN") -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Closing Dump", 0, [], [], {})
    mins_to_close = m["minutes_to_close"] or 999.0
    score = 10 if mins_to_close <= 60 else 0
    reasons: List[str] = []
    warnings: List[str] = []
    if mins_to_close <= 60:
        reasons.append("Late-day window")
    if latest.c <= (m["vwap"] or latest.c):
        score += 14
        reasons.append("Price below VWAP")
    if latest.c <= (m["ema9"] or latest.c):
        score += 12
        reasons.append("Price below EMA9")
    if m["rv"] >= 1.3:
        score += 10
        reasons.append("Volume supports the close")
    if _is_bearish_close(latest):
        score += 8
        reasons.append("Candle closed weak")
    if _extension_from_level(latest.c, m["vwap"]) > 0.8:
        warnings.append("Move may be late in the session")
    return _scenario("bearish", "Closing Dump", score, reasons, warnings, {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, confirmed=mins_to_close <= 60 and _is_bearish_close(latest), good_position=mins_to_close <= 30 and _is_bearish_close(latest), late=_extension_from_level(latest.c, m["vwap"]) > 0.8, do_not_chase=_extension_from_level(latest.c, m["vwap"]) > 1.2)


def evaluate_breakout_continuation(ctx: StrategyContext, direction: str) -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Breakout Continuation", 0, [], [], {})
    score = 20
    reasons: List[str] = []
    warnings: List[str] = []
    key_level = max(v for v in [ctx.levels.get("pmh"), ctx.levels.get("pdh"), ctx.levels.get("opening_range_high"), m["recent_high"]] if isinstance(v, (int, float))) if any(isinstance(v, (int, float)) for v in [ctx.levels.get("pmh"), ctx.levels.get("pdh"), ctx.levels.get("opening_range_high"), m["recent_high"]]) else None
    if direction == "bullish":
        if key_level and latest.c > key_level:
            score += 30
            reasons.append("Price broke above a key high")
        if latest.c >= (m["vwap"] or latest.c):
            score += 10
        if latest.c >= (m["ema9"] or latest.c):
            score += 10
        if _is_bullish_close(latest):
            score += 8
        if m["rv"] >= 1.5:
            score += 10
        if _extension_from_level(latest.c, key_level) > 0.8:
            warnings.append("Breakout may already be extended")
        return _scenario("bullish", "Breakout Continuation", score, reasons, warnings, {"key_level": key_level or 0.0, "vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, invalidation_level=(m["vwap"] or key_level or latest.l), invalidation_reason="Lost breakout level", confirmed=score >= 70 and _is_bullish_close(latest), good_position=score >= 75 and _extension_from_level(latest.c, key_level) <= 0.3, late=_extension_from_level(latest.c, key_level) > 0.6, do_not_chase=_extension_from_level(latest.c, key_level) > 1.0)
    key_level = min(v for v in [ctx.levels.get("pml"), ctx.levels.get("pdl"), ctx.levels.get("opening_range_low"), m["recent_low"]] if isinstance(v, (int, float))) if any(isinstance(v, (int, float)) for v in [ctx.levels.get("pml"), ctx.levels.get("pdl"), ctx.levels.get("opening_range_low"), m["recent_low"]]) else None
    if key_level and latest.c < key_level:
        score += 30
        reasons.append("Price broke below a key low")
    if latest.c <= (m["vwap"] or latest.c):
        score += 10
    if latest.c <= (m["ema9"] or latest.c):
        score += 10
    if _is_bearish_close(latest):
        score += 8
    if m["rv"] >= 1.5:
        score += 10
    if _extension_from_level(latest.c, key_level) > 0.8:
        warnings.append("Breakdown may already be extended")
    return _scenario("bearish", "Breakdown Continuation", score, reasons, warnings, {"key_level": key_level or 0.0, "vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, invalidation_level=(m["vwap"] or key_level or latest.h), invalidation_reason="Lost breakdown level", confirmed=score >= 70 and _is_bearish_close(latest), good_position=score >= 75 and _extension_from_level(latest.c, key_level) <= 0.3, late=_extension_from_level(latest.c, key_level) > 0.6, do_not_chase=_extension_from_level(latest.c, key_level) > 1.0)


def evaluate_pullback_holding(ctx: StrategyContext, direction: str) -> ScenarioResult:
    base = evaluate_bullish_trend_continuation(ctx) if direction == "bullish" else evaluate_bearish_trend_continuation(ctx)
    if base.score < 50:
        base.scenario_name = "Pullback Holding" if direction == "bullish" else "Pullback Rejecting"
    else:
        base.scenario_name = "Pullback Holding" if direction == "bullish" else "Pullback Rejecting"
    base.score = min(100, base.score + 6)
    if base.entry_quality_label == "UNKNOWN":
        base.entry_quality_label = "GOOD_POSITION" if base.score >= 70 else "EARLY"
    return base


def evaluate_fakeout_risk(ctx: StrategyContext, direction: str) -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Fakeout Risk", 0, [], [], {})
    score = 25
    reasons: List[str] = []
    warnings: List[str] = []
    if direction == "bullish" and m["upper_wick_pct"] >= 35:
        score += 25
        warnings.append("Upper wick rejection near breakout level")
    elif direction == "bearish" and m["lower_wick_pct"] >= 35:
        score += 25
        warnings.append("Lower wick rejection near breakdown level")
    if m["rv"] >= 1.5:
        score += 10
    if direction == "bullish" and latest.c < (m["vwap"] or latest.c):
        warnings.append("Bullish fakeout risk if VWAP is lost")
    if direction == "bearish" and latest.c > (m["vwap"] or latest.c):
        warnings.append("Bearish fakeout risk if VWAP is reclaimed")
    return _scenario("neutral", "Fakeout Risk", score, reasons, warnings, {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, late=True)


def evaluate_chop_no_trade(ctx: StrategyContext) -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Chop / No Trade", 0, [], [], {})
    score = 35
    reasons: List[str] = []
    warnings: List[str] = []
    if m["rv"] < 1.2:
        score += 20
        warnings.append("Low relative volume")
    if m["body_pct"] < 25:
        score += 15
        warnings.append("Small body / indecision candle")
    if m["vwap"] and abs(pct_change(latest.c, m["vwap"])) < 0.25:
        score += 12
        reasons.append("Price is churning around VWAP")
    if len(ctx.bars) >= 6:
        crosses = 0
        prev_side = ctx.bars[-6].c >= (m["vwap"] or ctx.bars[-6].c)
        for bar in ctx.bars[-5:]:
            side = bar.c >= (m["vwap"] or bar.c)
            if side != prev_side:
                crosses += 1
            prev_side = side
        if crosses >= 3:
            score += 15
            warnings.append("Repeated VWAP crosses")
    return _scenario("neutral", "Chop / No Trade", score, reasons, warnings, {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, late=True)


def evaluate_do_not_chase(ctx: StrategyContext) -> ScenarioResult:
    m = _base_metrics(ctx)
    latest = m["latest"]
    if not latest:
        return _scenario("neutral", "Do Not Chase", 0, [], [], {})
    score = 40
    warnings: List[str] = []
    reasons: List[str] = []
    ext_vwap = _extension_from_level(latest.c, m["vwap"])
    ext_ema = _extension_from_level(latest.c, m["ema9"])
    if ext_vwap > 0.6:
        score += 18
        warnings.append("Setup is valid but price is extended from VWAP")
    if ext_ema > 0.4:
        score += 18
        warnings.append("Setup is valid but price is extended from EMA9")
    if m["rv"] >= 3.0:
        score += 12
        warnings.append("Volume climax after an extended move")
    if len(ctx.bars) >= 3 and all(abs(ctx.bars[-i].c - ctx.bars[-i].o) / max(ctx.bars[-i].h - ctx.bars[-i].l, 1e-9) > 0.45 for i in range(1, 4)):
        score += 10
        warnings.append("Multiple large candles already printed")
    if score >= 80:
        reasons.append("Move is real, but the entry is late")
    return _scenario("neutral", "Do Not Chase", score, reasons, warnings, {"vwap": m["vwap"] or 0.0, "ema9": m["ema9"] or 0.0}, do_not_chase=True)


def _merge_scenario_scores(scenarios: List[ScenarioResult]) -> tuple[int, int, int, int]:
    bullish = max((s.score for s in scenarios if s.direction == "bullish"), default=0)
    bearish = max((s.score for s in scenarios if s.direction == "bearish"), default=0)
    chop = next((s.score for s in scenarios if s.scenario_name == "Chop / No Trade"), 0)
    fakeout = max((s.score for s in scenarios if "Fakeout" in s.scenario_name), default=0)
    return bullish, bearish, chop, fakeout


def evaluate_scenario_suite(
    symbol: str,
    bars: List[Any],
    latest: Any,
    config: Dict[str, Any],
    levels: Dict[str, Optional[float]],
    relative_volume: Optional[float],
    market_alignment: str,
    market_bars: Optional[Dict[str, List[Any]]] = None,
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
    scenarios: List[ScenarioResult] = [
        evaluate_bullish_trend_continuation(ctx, market_alignment),
        evaluate_bearish_trend_continuation(ctx, market_alignment),
        evaluate_bullish_vwap_reclaim_continuation(ctx, market_alignment),
        evaluate_bullish_reclaim_attempt_failing(ctx, market_alignment),
        evaluate_bearish_vwap_rejection_continuation(ctx, market_alignment),
        evaluate_pullback_holding(ctx, "bullish"),
        evaluate_pullback_holding(ctx, "bearish"),
        evaluate_opening_drive_up(ctx, market_alignment),
        evaluate_opening_dump(ctx, market_alignment),
        evaluate_opening_fakeout_risk(ctx, market_alignment),
        evaluate_closing_ramp(ctx, market_alignment),
        evaluate_closing_dump(ctx, market_alignment),
        evaluate_breakout_continuation(ctx, "bullish"),
        evaluate_breakout_continuation(ctx, "bearish"),
        evaluate_chop_no_trade(ctx),
        evaluate_fakeout_risk(ctx, "bullish"),
        evaluate_fakeout_risk(ctx, "bearish"),
        evaluate_do_not_chase(ctx),
    ]
    scenarios.sort(key=lambda item: item.score, reverse=True)
    top = scenarios[0] if scenarios else ScenarioResult("No Scenario", direction="neutral")
    second = scenarios[1] if len(scenarios) > 1 else ScenarioResult("No Scenario", direction="neutral")
    active = [item for item in scenarios if item.score > 0]
    combined_levels: Dict[str, float] = {}
    for item in scenarios:
        for key, value in (item.levels or {}).items():
            if isinstance(value, (int, float)):
                combined_levels[key] = value
    bullish_score, bearish_score, chop_score, fakeout_score = _merge_scenario_scores(scenarios)
    direction_conflict = False

    phase1_score = int((phase1_summary or {}).get("confidence_score") or 0)
    phase2_score = int((phase2_summary or {}).get("confirmation_score") or 0)
    phase1_direction = str((phase1_summary or {}).get("direction") or "neutral").lower()
    phase2_candle_label = str((phase2_summary or {}).get("candle_label") or "UNKNOWN").upper()
    phase2_volume_label = str((phase2_summary or {}).get("volume_label") or "UNKNOWN").upper()
    phase2_market_regime = str((phase2_summary or {}).get("market_regime") or "UNKNOWN").upper()
    phase2_entry_quality = str((phase2_summary or {}).get("entry_quality_label") or "UNKNOWN").upper()
    option_feed_status = str((option_context or {}).get("option_feed_status") or "UNAVAILABLE").upper()
    option_tradability_score = option_context.get("option_tradability_score") if option_context else None
    option_tradable = bool((option_context or {}).get("option_tradable", False))
    stock_setup_score = phase1_score
    if top.stage == "GOOD_POSITION":
        stock_setup_score = max(stock_setup_score, 70)
    elif top.stage == "CONFIRMED":
        stock_setup_score = max(stock_setup_score, 60)
    elif top.stage == "FORMING":
        stock_setup_score = max(stock_setup_score, 45)
    confirmation_score = phase2_score
    stock_setup_valid = stock_setup_score >= int(config.get("strategy_engine", {}).get("min_strategy_score_to_alert", 60)) and top.stage not in {"LATE", "DO_NOT_CHASE", "INVALIDATED"}
    sms_allowed_by_stock = top.stage in {"CONFIRMED", "GOOD_POSITION"} and stock_setup_valid
    sms_allowed_by_options = True
    sms_block_reason = ""
    direction_conflict = phase1_direction in {"bullish", "bearish"} and top.direction in {"bullish", "bearish"} and phase1_direction != top.direction
    candle_agrees = (
        (top.direction == "bullish" and phase2_candle_label == "BUYER_CONTROL")
        or (top.direction == "bearish" and phase2_candle_label == "SELLER_CONTROL")
        or top.direction == "neutral"
    )
    candle_conflict = (
        (top.direction == "bullish" and phase2_candle_label == "SELLER_CONTROL")
        or (top.direction == "bearish" and phase2_candle_label == "BUYER_CONTROL")
    )
    volume_supported = phase2_volume_label in {"NORMAL", "STRONG", "CLIMAX"}
    market_not_opposing = phase2_market_regime not in {"BEAR_TREND" if top.direction == "bullish" else "BULL_TREND", "CHOPPY", "UNKNOWN"}
    extended_from_vwap = _current_extension_pct(latest, top.levels.get("vwap")) > 0.6
    extended_from_ema = _current_extension_pct(latest, top.levels.get("ema9")) > 0.4
    scenario_conflict = bool(
        bullish_score >= 70 and bearish_score >= 70
        or direction_conflict
        or candle_conflict
    )
    if scenario_conflict:
        conflict_reasons = []
        if bullish_score >= 70 and bearish_score >= 70:
            conflict_reasons.append("bullish and bearish signals are both strong")
        if direction_conflict:
            conflict_reasons.append("current direction does not match top scenario")
        if candle_conflict:
            conflict_reasons.append("candle contradicts the setup")
        conflict_text = "; ".join(conflict_reasons) or "bullish and bearish signals disagree"
        top.warnings = list(dict.fromkeys([f"Scenario conflict: {conflict_text}"] + top.warnings))
        top.risk_label = "HIGH" if top.risk_label != "DO_NOT_CHASE" else top.risk_label
    stage_downgrade_reason = _apply_phase2_stage_consistency(
        top,
        latest=latest,
        levels=levels,
        current_levels={k: v for k, v in combined_levels.items() if isinstance(v, (int, float))},
        phase2_entry_quality=phase2_entry_quality,
    )
    if stage_downgrade_reason and stage_downgrade_reason not in top.warnings:
        top.warnings = list(dict.fromkeys(top.warnings + [stage_downgrade_reason]))
    option_strong_enough = bool(
        option_feed_status == "OPRA"
        or option_tradable
        or (option_feed_status == "INDICATIVE" and stock_setup_score >= 85 and confirmation_score >= 70)
    )
    would_sms = bool(
        top.stage == "GOOD_POSITION"
        and top.score >= 80
        and stock_setup_score >= 75
        and confirmation_score >= 65
        and candle_agrees
        and volume_supported
        and market_not_opposing
        and option_strong_enough
        and phase2_entry_quality not in {"LATE", "DO_NOT_CHASE", "UNKNOWN"}
    )
    alert_tier = _phase3_alert_tier(
        top=top,
        scenario_conflict=scenario_conflict,
        direction_conflict=direction_conflict,
        phase2_candle_label=phase2_candle_label,
        phase2_confirmation_score=confirmation_score,
        phase2_entry_quality=phase2_entry_quality,
        extended_from_vwap=extended_from_vwap,
        extended_from_ema=extended_from_ema,
        would_sms=would_sms,
    )
    scenario_alert_eligible = alert_tier in {"DASHBOARD_ALERT", "WOULD_SMS"}
    alert_block_reason = _phase3_alert_block_reason(
        tier=alert_tier,
        top=top,
        scenario_conflict=scenario_conflict,
        direction_conflict=direction_conflict,
        stage_downgrade_reason=stage_downgrade_reason or "",
        phase2_candle_label=phase2_candle_label,
        phase2_confirmation_score=confirmation_score,
        phase2_entry_quality=phase2_entry_quality,
        extended_from_vwap=extended_from_vwap,
        extended_from_ema=extended_from_ema,
    )
    scenario_sms_block_reason = ""
    if not scenario_alert_eligible:
        scenario_sms_block_reason = alert_block_reason
    elif not would_sms:
        reasons = []
        if top.stage != "GOOD_POSITION":
            reasons.append(f"stage is {top.stage}")
        if top.score < 80:
            reasons.append("scenario score below 80")
        if stock_setup_score < 75:
            reasons.append("stock setup score below 75")
        if confirmation_score < 65:
            reasons.append("confirmation score below 65")
        if not candle_agrees:
            reasons.append("candle does not agree with direction")
        if not volume_supported:
            reasons.append("volume is not normal/strong")
        if not market_not_opposing:
            reasons.append("market is opposing")
        if not option_strong_enough:
            reasons.append("option feed is not strong enough")
        if scenario_conflict:
            reasons.append("scenario conflict")
        if direction_conflict:
            reasons.append("direction conflict")
        if extended_from_vwap or extended_from_ema:
            reasons.append("price extended from VWAP/EMA9")
        scenario_sms_block_reason = "; ".join(dict.fromkeys([part for part in [alert_block_reason, *reasons] if part]))
    if scenario_conflict:
        sms_allowed_by_stock = False
        sms_block_reason = "Scenario conflict"
    if not stock_setup_valid:
        sms_allowed_by_stock = False
        sms_block_reason = f"Stock setup score below threshold ({stock_setup_score})"
    if top.stage in {"LATE", "DO_NOT_CHASE", "INVALIDATED", "WATCHING", "FORMING"}:
        sms_allowed_by_stock = False
        sms_block_reason = f"Scenario stage is {top.stage}"
    stock_setup_score_reason = _stock_setup_score_reason(top, active, stock_setup_score, top.stage, top.warnings)
    current_vwap = vwap(bars)
    current_ema9 = ema([bar.c for bar in bars], 9)
    current_ema20 = ema([bar.c for bar in bars], 20)
    if current_vwap:
        combined_levels.setdefault("vwap", current_vwap)
    if current_ema9:
        combined_levels.setdefault("ema9", current_ema9)
    if current_ema20:
        combined_levels.setdefault("ema20", current_ema20)
    if alert_tier == "DASHBOARD_ALERT" and scenario_sms_block_reason:
        alert_block_reason = scenario_sms_block_reason
    return {
        "top_scenario": top.to_dict(),
        "second_scenario": second.to_dict(),
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "chop_score": chop_score,
        "fakeout_score": fakeout_score,
        "scenario_conflict": scenario_conflict,
        "scenario_alert_tier": alert_tier,
        "scenario_alert_block_reason": alert_block_reason,
        "all_scenarios": [s.to_dict() for s in scenarios],
        "scenario_score": top.score,
        "scenario_stage": top.stage,
        "scenario_direction": top.direction,
        "scenario_confidence_label": top.confidence_label,
        "scenario_entry_quality_label": top.entry_quality_label,
        "scenario_risk_label": top.risk_label,
        "scenario_reasons": top.reasons,
        "scenario_warnings": top.warnings,
        "scenario_levels": combined_levels,
        "vwap": current_vwap,
        "ema9": current_ema9,
        "ema20": current_ema20,
        "stock_setup_score": stock_setup_score,
        "stock_setup_score_reason": stock_setup_score_reason,
        "stock_setup_valid": stock_setup_valid,
        "option_tradability_score": option_tradability_score,
        "option_feed_status": option_feed_status,
        "option_tradable": option_tradable,
        "sms_allowed_by_stock": sms_allowed_by_stock,
        "sms_allowed_by_options": sms_allowed_by_options,
        "sms_block_reason": sms_block_reason,
        "scenario_alert_eligible": scenario_alert_eligible,
        "scenario_would_sms": would_sms,
        "scenario_sms_block_reason": scenario_sms_block_reason or sms_block_reason or ("Scenario conflict" if scenario_conflict else ""),
        "scenario_sms_allowed": sms_allowed_by_stock and not scenario_conflict,
    }
