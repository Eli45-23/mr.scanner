from __future__ import annotations

from typing import Any, Dict, Optional


OFFICIAL_SETUP_NAMES = {
    "Bullish Pullback Holding",
    "Bullish VWAP/EMA Reclaim",
    "Bullish Liquidity Sweep Reclaim",
    "Bullish Breakout Retest",
    "Bullish Trend Continuation",
    "Opening Drive Up",
    "Bearish Pullback Rejecting",
    "Bearish VWAP/EMA Rejection",
    "Bearish Liquidity Sweep Rejection",
    "Bearish Failed Breakout",
    "Bearish Trend Continuation",
    "Opening Drive Down",
    "Do Not Chase",
    "Choppy No Trade",
    "Mixed Signal",
    "Late Move",
    "Low Quality Setup",
}

VALID_STAGES = {
    "WATCHING",
    "FORMING",
    "CONFIRMED",
    "GOOD_POSITION",
    "LATE",
    "DO_NOT_CHASE",
    "INVALIDATED",
}


def _direction(value: Any) -> str:
    direction = str(value or "neutral").lower()
    return direction if direction in {"bullish", "bearish"} else "neutral"


def _official_name(raw_name: str, direction: str) -> str:
    text = raw_name.lower()
    if "do not chase" in text:
        return "Do Not Chase"
    if "chop" in text or "no trade" in text:
        return "Choppy No Trade"
    if "late" in text:
        return "Late Move"
    if "opening drive" in text:
        return "Opening Drive Up" if direction == "bullish" else "Opening Drive Down"
    if "liquidity sweep" in text or "sweep" in text:
        return "Bullish Liquidity Sweep Reclaim" if direction == "bullish" else "Bearish Liquidity Sweep Rejection"
    if "pullback" in text:
        return "Bullish Pullback Holding" if direction == "bullish" else "Bearish Pullback Rejecting"
    if "failed breakout" in text or "fakeout" in text or "failed break" in text:
        return "Bearish Failed Breakout"
    if "vwap" in text or "ema reclaim" in text or "failed reclaim" in text:
        return "Bullish VWAP/EMA Reclaim" if direction == "bullish" else "Bearish VWAP/EMA Rejection"
    if "retest" in text or "breakout" in text:
        return "Bullish Breakout Retest" if direction == "bullish" else "Bearish Failed Breakout"
    if "trend continuation" in text or "continuation" in text:
        return "Bullish Trend Continuation" if direction == "bullish" else "Bearish Trend Continuation"
    return "Low Quality Setup"


def _mixed_reason(
    primary_name: str,
    primary_direction: str,
    scenario_name: str,
    scenario_direction: str,
    market_alignment: str,
) -> str:
    primary = primary_name.lower()
    scenario = scenario_name.lower()
    if "sweep" in primary and primary_direction == "bullish" and scenario_direction == "bearish":
        return "Bullish sweep detected, but the reclaim failed and Phase 3 turned bearish"
    if "sweep" in primary and primary_direction == "bearish" and scenario_direction == "bullish":
        return "Bearish sweep detected, but price reclaimed the swept level and Phase 3 turned bullish"
    if "vwap" in primary or "vwap" in scenario or "ema" in primary or "ema" in scenario:
        return "VWAP/EMA conflict: primary setup and Phase 3 scenario point in opposite directions"
    if market_alignment == "OPPOSED":
        return "SPY/QQQ conflict: market context opposes the stock setup"
    return "Primary setup and Phase 3 scenario disagree on direction"


def classify_professional_setup(
    phase1_summary: Optional[Dict[str, Any]],
    scenario_summary: Optional[Dict[str, Any]],
    *,
    market_alignment: str = "UNKNOWN",
) -> Dict[str, Any]:
    phase1_summary = phase1_summary or {}
    scenario_summary = scenario_summary or {}
    top = scenario_summary.get("top_scenario") or {}
    primary_name = str(phase1_summary.get("primary_setup") or "")
    scenario_name = str(top.get("scenario_name") or "")
    primary_direction = _direction(phase1_summary.get("direction"))
    scenario_direction = _direction(top.get("direction") or scenario_summary.get("scenario_direction"))
    conflict = bool(
        scenario_summary.get("scenario_conflict")
        or (
            primary_direction in {"bullish", "bearish"}
            and scenario_direction in {"bullish", "bearish"}
            and primary_direction != scenario_direction
        )
    )

    direction = scenario_direction if scenario_direction != "neutral" else primary_direction
    stage = str(top.get("stage") or scenario_summary.get("scenario_stage") or "WATCHING").upper()
    if stage not in VALID_STAGES:
        stage = "WATCHING"
    score = int(top.get("score") or scenario_summary.get("scenario_score") or phase1_summary.get("confidence_score") or 0)
    confidence = str(top.get("confidence_label") or scenario_summary.get("scenario_confidence_label") or phase1_summary.get("confidence_label") or "LOW").upper()
    entry_quality = str(top.get("entry_quality_label") or scenario_summary.get("scenario_entry_quality_label") or phase1_summary.get("entry_quality_label") or "UNKNOWN").upper()
    risk_label = str(top.get("risk_label") or scenario_summary.get("scenario_risk_label") or phase1_summary.get("risk_label") or "MEDIUM").upper()
    reasons = list(top.get("reasons") or scenario_summary.get("scenario_reasons") or phase1_summary.get("reasons") or [])
    invalidation_level = top.get("invalidation_level")
    block_reason = str(
        scenario_summary.get("scenario_alert_block_reason")
        or scenario_summary.get("scenario_sms_block_reason")
        or scenario_summary.get("sms_block_reason")
        or ""
    )

    raw_name = scenario_name or primary_name
    setup_name = _official_name(raw_name, direction)
    reason = str(reasons[0]) if reasons else f"{raw_name or 'Setup'} is being monitored"

    if conflict:
        setup_name = "Mixed Signal"
        direction = "neutral"
        stage = "WATCHING"
        confidence = "LOW"
        risk_label = "HIGH"
        entry_quality = "UNKNOWN"
        reason = _mixed_reason(primary_name, primary_direction, scenario_name, scenario_direction, market_alignment)
        block_reason = block_reason or "MIXED_SIGNAL: primary setup and Phase 3 scenario disagree"
    elif risk_label == "DO_NOT_CHASE" or stage == "DO_NOT_CHASE":
        setup_name = "Do Not Chase"
        stage = "DO_NOT_CHASE"
        block_reason = block_reason or "Setup is extended; wait for pullback or retest"
    elif stage == "LATE":
        setup_name = "Late Move"
        block_reason = block_reason or "Setup is valid but entry timing is late"
    elif setup_name == "Low Quality Setup":
        block_reason = block_reason or "Setup does not match the official playbook cleanly"

    watch_text = (
        f"Watch only: {reason}. Confirm manually on chart."
        if setup_name in {"Mixed Signal", "Do Not Chase", "Choppy No Trade", "Late Move", "Low Quality Setup"}
        else f"Watch {setup_name}: {reason}. Confirm stage and invalidation manually."
    )
    return {
        "setup_name": setup_name,
        "setup_code": setup_name.upper().replace(" ", "_").replace("/", "_"),
        "direction": direction,
        "stage": stage,
        "score": max(0, min(100, score)),
        "confidence": confidence,
        "reason": reason,
        "invalidation_level": invalidation_level,
        "entry_quality": entry_quality,
        "risk_label": risk_label,
        "watch_text": watch_text,
        "block_reason": block_reason,
        "primary_setup_raw": primary_name or None,
        "phase3_scenario_raw": scenario_name or None,
        "mixed_signal": conflict,
    }
