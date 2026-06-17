from __future__ import annotations

from typing import Any, Dict, List

from scanner.options_whale_scoring import safe_float


def classify_aggression(candidate: Dict[str, Any]) -> Dict[str, Any]:
    last = safe_float(candidate.get("last"))
    bid = safe_float(candidate.get("bid"))
    ask = safe_float(candidate.get("ask"))
    option_type = str(candidate.get("option_type") or "").upper()
    midpoint = safe_float(candidate.get("midpoint"))
    side = "unknown"
    score = 0
    warning = ""
    if last <= 0 or bid <= 0 or ask <= 0:
        warning = "Trade/quote aggression unavailable."
    elif last >= ask * 0.995:
        side, score = "near_ask", 20
    elif last <= bid * 1.005:
        side, score = "near_bid", 20
    elif midpoint and abs(last - midpoint) / midpoint <= 0.03:
        side, score = "midpoint", 8
        warning = "Midpoint activity makes direction unclear."
    elif last > ask:
        side, score, warning = "above_ask_anomaly", 12, "Trade price above ask; data may be stale or crossed."
    elif last < bid:
        side, score, warning = "below_bid_anomaly", 12, "Trade price below bid; data may be stale or crossed."
    else:
        side, score = "unknown", 3

    if option_type == "CALL" and side == "near_ask":
        direction, confidence = "Possible bullish call flow", "MEDIUM"
    elif option_type == "PUT" and side == "near_ask":
        direction, confidence = "Possible bearish put flow", "MEDIUM"
    elif option_type == "CALL" and side == "near_bid":
        direction, confidence = "Possible call selling / bearish or unclear", "LOW"
    elif option_type == "PUT" and side == "near_bid":
        direction, confidence = "Possible put selling / bullish or unclear", "LOW"
    else:
        direction, confidence = "Mixed / unclear flow", "LOW"
    return {
        "aggression_side": side,
        "aggression_score": score,
        "direction_label": direction,
        "direction_confidence": confidence,
        "direction_warning": warning,
    }


def estimate_opening_flow(candidate: Dict[str, Any]) -> Dict[str, Any]:
    volume = safe_float(candidate.get("volume"))
    oi = safe_float(candidate.get("open_interest"))
    if oi <= 0 and volume > 0:
        return {
            "opening_flow_estimate": "possible opening flow",
            "opening_confidence": "MEDIUM",
            "oi_warning": "Open interest is zero or unavailable; next-day OI review needed.",
        }
    ratio = volume / oi if oi else 0.0
    if ratio >= 2.0:
        return {
            "opening_flow_estimate": "possible opening flow",
            "opening_confidence": "MEDIUM",
            "oi_warning": "Volume is much greater than open interest; confirm with next-day OI.",
        }
    if oi >= volume * 3 and oi > 0:
        return {
            "opening_flow_estimate": "could be closing or rolling",
            "opening_confidence": "LOW",
            "oi_warning": "Existing open interest is large relative to volume.",
        }
    return {
        "opening_flow_estimate": "unclear",
        "opening_confidence": "LOW",
        "oi_warning": "Opening/closing cannot be confirmed intraday.",
    }


def apply_multileg_direction_adjustment(flow: Dict[str, Any], multileg: Dict[str, Any]) -> Dict[str, Any]:
    adjusted = dict(flow)
    if multileg.get("possible_multileg") and multileg.get("direction_clarity") != "clear":
        adjusted["direction_confidence"] = "LOW"
        warning = adjusted.get("direction_warning") or ""
        adjusted["direction_warning"] = (warning + " Possible multi-leg structure reduces directional clarity.").strip()
    return adjusted
