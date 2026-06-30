from __future__ import annotations

from typing import Any, Dict


WARNING_FILTER_VERSION = "warning-alert-filter-1.0"


def classify_warning_alert(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    settings = config.get("warning_alert_filter", {})
    if not settings.get("enabled", True):
        return {
            "matched": False,
            "telegram_allowed": True,
            "suppression_reason": "",
            "warning_type": "",
            "clean_chop_exit": False,
            "version": WARNING_FILTER_VERSION,
        }

    conclusion = str(payload.get("phone_conclusion") or "").upper()
    decision = str(
        payload.get("decision_label")
        or payload.get("decision_tier")
        or payload.get("orchestrator_final_alert_type")
        or ""
    ).upper()
    category = str(payload.get("category") or "").upper()
    combined = " ".join((conclusion, decision, category))
    setup = str(payload.get("setup_name") or payload.get("primary_setup") or "").strip().upper()
    clean_chop_exit = bool(payload.get("clean_chop_exit") or payload.get("chop_exit_clean_confirmation"))

    if clean_chop_exit and settings.get("allow_chop_exit_text", True):
        return {
            "matched": True,
            "telegram_allowed": True,
            "suppression_reason": "",
            "warning_type": "clean_chop_exit",
            "clean_chop_exit": True,
            "version": WARNING_FILTER_VERSION,
        }

    if "CHOP" in combined:
        first_activation = bool(payload.get("chop_activation_first") or payload.get("chop_warning_sent"))
        if first_activation and settings.get("chop_activation_text_once", True):
            return {
                "matched": True,
                "telegram_allowed": True,
                "suppression_reason": "",
                "warning_type": "chop_activation",
                "clean_chop_exit": False,
                "version": WARNING_FILTER_VERSION,
            }
        if settings.get("chop_repeated_dashboard_only", True):
            return {
                "matched": True,
                "telegram_allowed": False,
                "suppression_reason": "repeated_chop",
                "warning_type": "chop_repeated",
                "clean_chop_exit": False,
                "version": WARNING_FILTER_VERSION,
            }

    if ("MIXED" in combined or bool(payload.get("mixed_signal_detected"))) and settings.get(
        "mixed_dashboard_only", True
    ):
        return {
            "matched": True,
            "telegram_allowed": False,
            "suppression_reason": "mixed_dashboard_only",
            "warning_type": "mixed_signal",
            "clean_chop_exit": False,
            "version": WARNING_FILTER_VERSION,
        }

    dashboard_only_setups = {
        str(value).strip().upper()
        for value in settings.get("dashboard_only_setups", ["MIXED SIGNAL", "BULLISH PULLBACK HOLDING"])
        if str(value).strip()
    }
    if setup in dashboard_only_setups:
        return {
            "matched": True,
            "telegram_allowed": False,
            "suppression_reason": "configured_setup_dashboard_only",
            "warning_type": "setup_dashboard_only",
            "clean_chop_exit": False,
            "version": WARNING_FILTER_VERSION,
        }

    standalone_do_not_chase = conclusion == "DO NOT CHASE" or decision in {
        "DO_NOT_CHASE",
        "MISSED_CLEAN_ENTRY_NOW_LATE",
    }
    if standalone_do_not_chase and settings.get("do_not_chase_dashboard_only", True):
        return {
            "matched": True,
            "telegram_allowed": False,
            "suppression_reason": "do_not_chase_dashboard_only",
            "warning_type": "do_not_chase",
            "clean_chop_exit": False,
            "version": WARNING_FILTER_VERSION,
        }

    standalone_late = conclusion == "LATE MOVE" or decision in {"LATE", "LATE_MOVE"}
    if standalone_late and settings.get("late_move_dashboard_only", True):
        return {
            "matched": True,
            "telegram_allowed": False,
            "suppression_reason": "late_move_dashboard_only",
            "warning_type": "late_move",
            "clean_chop_exit": False,
            "version": WARNING_FILTER_VERSION,
        }

    return {
        "matched": False,
        "telegram_allowed": True,
        "suppression_reason": "",
        "warning_type": "",
        "clean_chop_exit": False,
        "version": WARNING_FILTER_VERSION,
    }
