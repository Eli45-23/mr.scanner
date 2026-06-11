from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple


FORBIDDEN_WORDING = re.compile(r"\b(?:buy|sell|enter|get in|take (?:this )?trade)\b", re.IGNORECASE)


def _direction(value: Any) -> str:
    direction = str(value or "").upper()
    return direction if direction in {"BULLISH", "BEARISH"} else "NEUTRAL"


def _score(context: Dict[str, Any]) -> int:
    values = [
        context.get("setup_score"),
        context.get("stock_setup_score"),
        context.get("strategy_confidence_score"),
        context.get("scenario_score"),
    ]
    return max((int(value) for value in values if isinstance(value, (int, float))), default=0)


def collect_engine_votes(context: Dict[str, Any]) -> Dict[str, Any]:
    trends = [_direction(context.get(key)) for key in ("trend_1m", "trend_5m", "trend_15m")]
    bullish = trends.count("BULLISH")
    bearish = trends.count("BEARISH")
    trend_direction = "BULLISH" if bullish > bearish else "BEARISH" if bearish > bullish else "NEUTRAL"
    strategy_directions = [
        _direction(item.get("direction"))
        for item in context.get("strategy_results") or []
        if item.get("active")
    ]
    return {
        "timeframe_trends": trends,
        "timeframes_bullish": bullish,
        "timeframes_bearish": bearish,
        "timeframe_direction": trend_direction,
        "structure_bias": _direction(context.get("current_structure_bias")),
        "strategy_direction": _direction(context.get("strategy_direction")),
        "scenario_direction": _direction(context.get("scenario_direction")),
        "alert_direction": _direction(context.get("direction")),
        "strategy_directions": strategy_directions,
        "market_alignment": str(context.get("market_alignment") or "UNKNOWN").upper(),
        "chop_mode_active": bool(context.get("chop_mode_active")),
        "sweep_status": str(context.get("liquidity_sweep_status") or "").upper(),
        "sweep_trap_bias": _direction(context.get("sweep_trap_bias")),
    }


def determine_trend_context_eligibility(
    context: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[bool, str, Dict[str, Any]]:
    settings = config.get("alert_orchestrator", {})
    votes = collect_engine_votes(context)
    direction = votes["timeframe_direction"]
    aligned_count = max(votes["timeframes_bullish"], votes["timeframes_bearish"])
    facts = {"direction": direction, "aligned_timeframes": aligned_count, "votes": votes}
    if not settings.get("trend_context_enabled", True):
        return False, "Trend-context alerts disabled", facts
    if settings.get("trend_context_aapl_only", True) and str(context.get("symbol") or "").upper() != "AAPL":
        return False, "AAPL is the only trend-context alert symbol", facts
    if context.get("chop_mode_active") and not (
        settings.get("trend_context_allow_through_chop", True)
        and settings.get("chop_allows_trend_context", True)
    ):
        return False, "Trend context is configured not to pass through Chop Mode", facts
    if direction == "NEUTRAL" or aligned_count < int(settings.get("trend_context_min_timeframes_aligned", 2)):
        return False, "Not enough timeframe agreement", facts
    if _score(context) < int(settings.get("trend_context_min_score", 75)):
        return False, "Directional setup score below trend-context threshold", facts
    if bool(context.get("mixed_signal_detected") or context.get("scenario_conflict")):
        return False, "Mixed setup signals block directional trend context", facts
    if votes["market_alignment"] == "OPPOSED":
        return False, "Market alignment strongly opposes the trend context", facts
    structure = votes["structure_bias"]
    strategy = votes["strategy_direction"]
    if structure not in {direction, "NEUTRAL"} and strategy != direction:
        return False, "Structure and strategy do not support the timeframe trend", facts
    if settings.get("trend_context_require_vwap_ema_alignment", True):
        price = context.get("price")
        vwap, ema9 = context.get("vwap"), context.get("ema9")
        if not all(isinstance(value, (int, float)) for value in (price, vwap, ema9)):
            return False, "VWAP/EMA9 alignment data unavailable", facts
        aligned = price > vwap and price > ema9 if direction == "BULLISH" else price < vwap and price < ema9
        if not aligned:
            return False, "Price is not aligned with VWAP and EMA9", facts
    option_present = context.get("option_tradable") is not None or context.get("option_quality")
    if option_present and not bool(context.get("option_tradable")):
        return False, "Option context is present but not tradable", facts
    return True, f"Strong {direction.lower()} trend context across {aligned_count} timeframes", facts


def determine_trade_quality_eligibility(context: Dict[str, Any], config: Dict[str, Any]) -> Tuple[bool, str]:
    if not bool(context.get("existing_trade_ready")):
        return False, "Existing strict trade-quality rules did not approve"
    if not bool(context.get("option_tradable")):
        return False, "Option quality does not support trade-quality alert"
    if context.get("mixed_signal_detected") or context.get("scenario_conflict"):
        return False, "Mixed setup signals block trade-quality alert"
    if context.get("do_not_chase") or str(context.get("risk_label") or "").upper() == "DO_NOT_CHASE":
        return False, "Do-not-chase risk blocks trade-quality alert"
    if config.get("alert_orchestrator", {}).get("chop_blocks_trade_quality", True) and context.get("chop_mode_active"):
        return False, "Chop Mode blocks trade-quality alert"
    return True, "Existing strict trade-quality approval preserved"


def determine_sweep_event_eligibility(
    context: Dict[str, Any],
    config: Dict[str, Any],
) -> Tuple[bool, str, Dict[str, Any]]:
    if not config.get("alert_orchestrator", {}).get("sweep_event_uses_alert_filter", True):
        return False, "Sweep-event orchestration disabled", {}
    metadata = dict(context.get("sweep_filter") or {})
    allowed = bool(metadata.get("telegram_filter_allowed"))
    return allowed, str(metadata.get("telegram_filter_reason") or "No meaningful sweep event"), metadata


def orchestrate_alert_decision(
    context: Dict[str, Any],
    config: Dict[str, Any],
    recent_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = config.get("alert_orchestrator", {})
    votes = collect_engine_votes(context)
    direction = votes["timeframe_direction"]
    score = _score(context)
    risk_notes = []
    conflicts = []
    wait_for = ["Clean pullback/retest and manual chart confirmation."]
    if context.get("chop_mode_active"):
        risk_notes.append("Chop Mode is active; directional context remains watch-only.")
    if context.get("sweep_risk_active") or context.get("liquidity_sweep_context"):
        risk_notes.append(str(context.get("liquidity_sweep_context") or "Liquidity sweep risk is active."))
    if context.get("do_not_chase") or str(context.get("risk_label") or "").upper() == "DO_NOT_CHASE":
        risk_notes.append("Move is late or extended; do not chase.")
        wait_for = ["A fresh pullback/retest before continuation."]
    if context.get("mixed_signal_detected") or context.get("scenario_conflict"):
        conflicts.append("Primary setup and scenario signals conflict.")

    trade_allowed, trade_reason = determine_trade_quality_eligibility(context, config)
    trend_allowed, trend_reason, trend_detail = determine_trend_context_eligibility(context, config)
    sweep_allowed, sweep_reason, sweep_detail = determine_sweep_event_eligibility(context, config)

    final_type = "DASHBOARD_ONLY"
    reason = "Context remains visible on dashboard; no user-facing alert qualified."
    telegram_allowed = False
    watch_only = False
    context_only = True
    trade_ready = False
    priority = min(55, score)
    if trade_allowed:
        final_type, reason, telegram_allowed, trade_ready, context_only, priority = (
            "TRADE_QUALITY", trade_reason, True, True, False, max(85, score)
        )
    elif conflicts:
        final_type, reason, telegram_allowed, watch_only, priority = (
            "MIXED_NO_TRADE", "Directional setup signals conflict.", bool(context.get("existing_user_alert")), True, 65
        )
    elif (
        settings.get("do_not_chase_context_enabled", True)
        and (context.get("do_not_chase") or str(context.get("risk_label") or "").upper() == "DO_NOT_CHASE")
    ):
        final_type, reason, telegram_allowed, watch_only, priority = (
            "DO_NOT_CHASE", "Strong context may remain, but the move is late or extended.", bool(trend_allowed), True, 72
        )
    elif trend_allowed:
        previous = (recent_state or {}).get("last_trend_context") if isinstance(recent_state, dict) else None
        if isinstance(previous, dict) and _direction(previous.get("direction")) == direction:
            try:
                sent_at = datetime.fromisoformat(str(previous.get("timestamp")).replace("Z", "+00:00"))
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                cooldown = int(settings.get("trend_context_cooldown_minutes", 15))
                if datetime.now(timezone.utc) - sent_at <= timedelta(minutes=cooldown):
                    trend_allowed, trend_reason = False, "Trend context duplicate within cooldown"
            except (TypeError, ValueError):
                pass
    if final_type == "DASHBOARD_ONLY":
        if trend_allowed:
            final_type, reason, telegram_allowed, watch_only, priority = (
                "TREND_CONTEXT", trend_reason, True, True, max(75, score)
            )
        elif sweep_allowed:
            final_type, reason, telegram_allowed, watch_only, priority = (
                "SWEEP_EVENT", sweep_reason, True, True, max(70, score)
            )
        elif context.get("chop_mode_active") and trend_reason != "Trend context duplicate within cooldown":
            final_type, reason, telegram_allowed, watch_only, priority = (
                "CHOP_WARNING", "Chop Mode is active and no strong directional context qualified.", bool(context.get("chop_warning_due")), True, 60
            )

    title_direction = direction.title() if direction != "NEUTRAL" else "Market"
    title = {
        "TREND_CONTEXT": f"AAPL {title_direction} Trend Watch",
        "TRADE_QUALITY": f"AAPL {title_direction} Trade Quality Watch",
        "SWEEP_EVENT": "AAPL Liquidity Sweep Event",
        "CHOP_WARNING": "AAPL CHOP MODE — No clean edge",
        "DO_NOT_CHASE": "AAPL DO NOT CHASE",
        "MIXED_NO_TRADE": "AAPL MIXED / NO TRADE",
        "DASHBOARD_ONLY": "AAPL Dashboard Context",
    }[final_type]
    return {
        "final_alert_type": final_type,
        "final_direction": direction,
        "final_priority": min(100, int(priority)),
        "telegram_allowed": bool(telegram_allowed),
        "dashboard_allowed": True,
        "trade_ready": bool(trade_ready),
        "watch_only": bool(watch_only),
        "context_only": bool(context_only),
        "can_approve_trades": False,
        "decision_reason": reason,
        "block_reason": "" if telegram_allowed else reason,
        "suppression_reason": "" if telegram_allowed else reason,
        "what_to_wait_for": wait_for,
        "risk_notes": risk_notes,
        "engine_votes": votes,
        "conflicts": conflicts,
        "recommended_message_title": title,
        "recommended_message_sections": ["Why", "Risk", "Wait for", "Invalidation"],
        "trend_context_detail": trend_detail,
        "sweep_filter": sweep_detail,
    }


def format_orchestrator_summary(decision: Dict[str, Any]) -> str:
    text = (
        f"{decision.get('recommended_message_title')}: {decision.get('decision_reason')} "
        f"Wait for: {' '.join(decision.get('what_to_wait_for') or [])}"
    )
    return FORBIDDEN_WORDING.sub("review", text)
