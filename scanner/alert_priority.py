from __future__ import annotations

import re
from typing import Any, Dict, Tuple

from scanner.warning_alert_filter import classify_warning_alert


TIERS = {"TIER_1_TEXT_ALERT", "TIER_2_DASHBOARD_ONLY", "TIER_3_LOG_ONLY"}
FORBIDDEN_ACTION_WORDING = re.compile(
    r"\b(?:buy|sell|enter|get in|take trade|guaranteed|sure win)\b",
    re.IGNORECASE,
)


def _text(payload: Dict[str, Any]) -> str:
    values = [
        payload.get("category"),
        payload.get("primary_setup"),
        payload.get("setup_name"),
        payload.get("phone_conclusion"),
        payload.get("decision_label"),
        payload.get("orchestrator_final_alert_type"),
        payload.get("liquidity_sweep_status"),
        payload.get("update_type"),
    ]
    return " ".join(str(value or "") for value in values).upper()


def _score(payload: Dict[str, Any]) -> int:
    values = [
        payload.get("orchestrator_final_priority"),
        payload.get("alert_score"),
        payload.get("setup_score"),
        payload.get("strategy_confidence_score"),
        payload.get("scenario_score"),
        payload.get("confirmation_score"),
        payload.get("score"),
    ]
    return max((int(value) for value in values if isinstance(value, (int, float))), default=0)


def _result(tier: str, reason: str, score: int, *, context_only: bool) -> Dict[str, Any]:
    return {
        "tier": tier,
        "should_send_telegram": tier == "TIER_1_TEXT_ALERT",
        "dashboard_only": tier == "TIER_2_DASHBOARD_ONLY",
        "reason": reason,
        "priority_score": max(0, min(100, int(score))),
        "can_approve_trades": False,
        "context_only": bool(context_only),
        "warning_filter_suppressed": False,
        "warning_suppression_reason": "",
        "warning_filter_type": "",
    }


def classify_alert_priority(alert_payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    settings = config.get("alert_priority", {})
    if not settings.get("enabled", True):
        return _result("TIER_2_DASHBOARD_ONLY", "Alert priority ladder disabled; safe dashboard fallback", 0, context_only=True)

    text = _text(alert_payload)
    score = _score(alert_payload)
    min_score = int(settings.get("min_priority_score_for_telegram", 80))
    context_only = bool(alert_payload.get("context_only") or alert_payload.get("orchestrator_context_only"))
    existing_approved = bool(
        alert_payload.get("existing_user_facing_approved")
        or alert_payload.get("sms_allowed")
        or alert_payload.get("watch_allowed")
        or alert_payload.get("phase3_heads_up_sent")
        or alert_payload.get("telegram_filter_allowed")
    )
    warning_decision = classify_warning_alert(alert_payload, config)
    if warning_decision["matched"] and not warning_decision["telegram_allowed"]:
        result = _result(
            "TIER_2_DASHBOARD_ONLY",
            warning_decision["suppression_reason"],
            score,
            context_only=True,
        )
        result.update({
            "warning_filter_suppressed": True,
            "warning_suppression_reason": warning_decision["suppression_reason"],
            "warning_filter_type": warning_decision["warning_type"],
        })
        return result

    duplicate = bool(
        alert_payload.get("dedupe_blocked")
        or alert_payload.get("phase3_heads_up_dedupe_blocked")
        or alert_payload.get("duplicate_context")
    )
    if duplicate or any(token in text for token in ("INTERNAL DIAGNOSTIC", "UNCHANGED ZONE", "REPEATED BLOCKED")):
        return _result("TIER_3_LOG_ONLY", "Duplicate, unchanged, or internal diagnostic record", score, context_only=True)
    if score < 40 and not existing_approved:
        return _result("TIER_3_LOG_ONLY", "Low-confidence internal noise", score, context_only=True)

    sweep_status = str(alert_payload.get("liquidity_sweep_status") or alert_payload.get("sweep_status") or "").upper()
    sweep_confidence = alert_payload.get("score") if isinstance(alert_payload.get("score"), (int, float)) else score
    sweep_major = bool(
        alert_payload.get("near_major_level")
        or str(alert_payload.get("level_source") or "").lower()
        in {"hod", "lod", "pmh", "pml", "pdh", "pdl", "5m_supply", "5m_demand", "15m_supply", "15m_demand"}
    )
    if sweep_status in {"SWEEP_WATCH", "SWEEP_FORMING", "SWEEP_FAILED_HELD"}:
        return _result("TIER_2_DASHBOARD_ONLY", f"{sweep_status.replace('_', ' ').title()} remains context-only", score, context_only=True)
    if sweep_status == "SWEEP_CONFIRMED":
        threshold = int(settings.get("confirmed_sweep_min_confidence", 70))
        if (
            settings.get("allow_confirmed_sweep_text", True)
            and existing_approved
            and float(sweep_confidence or 0) >= threshold
            and sweep_major
        ):
            return _result("TIER_1_TEXT_ALERT", "High-confidence confirmed sweep near a major level", max(score, int(sweep_confidence)), context_only=True)
        return _result("TIER_2_DASHBOARD_ONLY", "Confirmed sweep did not meet Tier 1 confidence/major-level requirements", score, context_only=True)

    if any(token in text for token in (
        "SUPPORT/RESISTANCE", "SUPPORT RESISTANCE", "SUPPLY/DEMAND", "SUPPLY DEMAND",
        "LOW QUALITY", "CONTEXT ONLY",
        "DASHBOARD_ONLY", "TREND_CONTEXT", "SWEEP_EVENT",
    )):
        return _result("TIER_2_DASHBOARD_ONLY", "Context, mixed, late, or risk warning remains dashboard-only", score, context_only=True)
    if "CHOP" in text:
        first_activation = bool(alert_payload.get("chop_activation_first"))
        clean_exit = bool(warning_decision.get("clean_chop_exit"))
        warning_settings = config.get("warning_alert_filter", {})
        if clean_exit and warning_settings.get("allow_chop_exit_text", True) and existing_approved and score >= min_score:
            return _result("TIER_1_TEXT_ALERT", "Clean confirmed exit from Chop Mode", score, context_only=False)
        if (
            first_activation
            and warning_settings.get("chop_activation_text_once", settings.get("allow_chop_activation_text_once", True))
            and existing_approved
            and score >= min_score
        ):
            return _result("TIER_1_TEXT_ALERT", "Initial high-priority Chop Mode activation", score, context_only=True)
        return _result("TIER_2_DASHBOARD_ONLY", "repeated_chop", score, context_only=True)

    stage = str(alert_payload.get("scenario_stage") or alert_payload.get("setup_stage") or "").upper()
    confirmed = (
        stage in {"CONFIRMED", "GOOD_POSITION"}
        or "CONFIRMED" in text
        or bool(alert_payload.get("sms_allowed"))
        or bool(alert_payload.get("orchestrator_trade_ready"))
    )
    clean_directional = (
        ("CLEAN" in text and any(token in text for token in ("BREAKOUT", "BREAKDOWN")))
        or any(token in text for token in (
        "VWAP RECLAIM",
        "VWAP LOSS",
        "PULLBACK HOLD",
        "PULLBACK REJECT",
        "OPENING DRIVE",
        ))
        or ("MAJOR MARKET STRUCTURE SHIFT" in text and settings.get("major_structure_shift_text", True))
    )
    if existing_approved and confirmed and clean_directional and score >= min_score:
        return _result("TIER_1_TEXT_ALERT", "Existing approved high-priority directional event", score, context_only=False)

    return _result("TIER_2_DASHBOARD_ONLY", "Event did not meet Tier 1 notification requirements", score, context_only=True)


def validate_priority_decision(priority_result: Dict[str, Any]) -> Tuple[bool, str]:
    tier = str(priority_result.get("tier") or "")
    if tier not in TIERS:
        return False, "Unknown alert priority tier"
    if priority_result.get("can_approve_trades") is not False:
        return False, "Alert priority cannot approve trades"
    if tier != "TIER_1_TEXT_ALERT" and priority_result.get("should_send_telegram"):
        return False, "Tier 2 and Tier 3 cannot send Telegram"
    if tier == "TIER_1_TEXT_ALERT" and not priority_result.get("should_send_telegram"):
        return False, "Tier 1 notification decision is inconsistent"
    protected_context = str(priority_result.get("upgrade_source") or "").upper()
    if protected_context in {"MARKET_STRUCTURE", "LIQUIDITY_SWEEP"} and priority_result.get("can_approve_trades"):
        return False, "Context engines cannot approve trades"
    message = str(priority_result.get("message") or "")
    message_without_required_disclaimer = message.replace("Not a buy/sell signal.", "")
    if FORBIDDEN_ACTION_WORDING.search(message_without_required_disclaimer):
        return False, "Forbidden action wording detected"
    return True, "Alert priority decision is valid"
