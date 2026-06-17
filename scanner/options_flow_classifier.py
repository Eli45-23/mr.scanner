from __future__ import annotations

from typing import Any, Dict, List

from scanner.options_whale_scoring import safe_float


def classify_aggression(candidate: Dict[str, Any]) -> Dict[str, Any]:
    last = safe_float(candidate.get("last"))
    bid = safe_float(candidate.get("bid"))
    ask = safe_float(candidate.get("ask"))
    option_type = str(candidate.get("option_type") or "").upper()
    midpoint = safe_float(candidate.get("midpoint"))
    quote_age = candidate.get("quote_freshness_seconds")
    side = "unknown"
    score = 0
    warning = ""
    reason = ""
    if last <= 0 or bid <= 0 or ask <= 0:
        warning = "Trade/quote aggression unavailable."
        reason = "Missing trade price or bid/ask quote."
    elif last >= ask * 0.995:
        side, score = "near_ask", 20
        reason = "Last trade printed near the ask."
    elif last <= bid * 1.005:
        side, score = "near_bid", 20
        reason = "Last trade printed near the bid."
    elif midpoint and abs(last - midpoint) / midpoint <= 0.03:
        side, score = "midpoint", 8
        warning = "Midpoint activity makes direction unclear."
        reason = "Last trade printed near the bid/ask midpoint."
    elif last > ask:
        side, score, warning = "above_ask_anomaly", 12, "Trade price above ask; data may be stale or crossed."
        reason = "Last trade printed above the displayed ask."
    elif last < bid:
        side, score, warning = "below_bid_anomaly", 12, "Trade price below bid; data may be stale or crossed."
        reason = "Last trade printed below the displayed bid."
    else:
        side, score = "unknown", 3
        reason = "Trade price did not clearly match bid, ask, or midpoint."

    quote_stale_warning = ""
    try:
        if quote_age is not None and float(quote_age) > 120:
            quote_stale_warning = "Quote is stale; aggression confidence reduced."
            warning = (warning + " " + quote_stale_warning).strip()
            score = min(score, 8)
    except (TypeError, ValueError):
        pass

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
    if quote_stale_warning:
        confidence = "LOW"
    return {
        "aggression_side": side,
        "aggression_score": score,
        "aggression_confidence": confidence,
        "bid_ask_reason": reason,
        "quote_stale_warning": quote_stale_warning,
        "direction_label": direction,
        "direction_confidence": confidence,
        "direction_warning": warning,
    }


def estimate_opening_flow(candidate: Dict[str, Any]) -> Dict[str, Any]:
    volume = safe_float(candidate.get("volume"))
    oi = safe_float(candidate.get("open_interest"))
    base = {
        "awaiting_next_day_oi_confirmation": True,
        "next_day_oi_status": "pending",
        "next_day_oi_reason": "awaiting next trading day OI",
    }
    if oi <= 0 and volume > 0:
        return {
            **base,
            "opening_flow_estimate": "possible opening flow",
            "open_close_estimate": "likely_opening",
            "opening_confidence": "MEDIUM",
            "open_close_confidence": "MEDIUM",
            "open_close_reason": "Volume printed against zero or unavailable open interest; next-day OI is required for confirmation.",
            "oi_warning": "Open interest is zero or unavailable; next-day OI review needed.",
        }
    ratio = volume / oi if oi else 0.0
    if ratio >= 2.0:
        return {
            **base,
            "opening_flow_estimate": "possible opening flow",
            "open_close_estimate": "likely_opening",
            "opening_confidence": "MEDIUM",
            "open_close_confidence": "MEDIUM",
            "open_close_reason": "Volume is much greater than current open interest; next-day OI is required for confirmation.",
            "oi_warning": "Volume is much greater than open interest; confirm with next-day OI.",
        }
    if oi >= volume * 3 and oi > 0:
        return {
            **base,
            "opening_flow_estimate": "could be closing or rolling",
            "open_close_estimate": "likely_closing",
            "opening_confidence": "LOW",
            "open_close_confidence": "LOW",
            "open_close_reason": "Existing open interest is large relative to today’s volume, so the flow may be closing or rolling.",
            "oi_warning": "Existing open interest is large relative to volume.",
        }
    if oi > 0 and 0.5 <= ratio < 2.0:
        return {
            **base,
            "opening_flow_estimate": "mixed",
            "open_close_estimate": "mixed",
            "opening_confidence": "LOW",
            "open_close_confidence": "LOW",
            "open_close_reason": "Volume is meaningful but not large enough versus open interest to separate opening from closing.",
            "oi_warning": "Opening/closing remains mixed until next-day OI is available.",
        }
    return {
        **base,
        "opening_flow_estimate": "unclear",
        "open_close_estimate": "unknown",
        "opening_confidence": "LOW",
        "open_close_confidence": "LOW",
        "open_close_reason": "Current-day volume and open interest do not clearly separate opening, closing, or rolling flow.",
        "oi_warning": "Opening/closing cannot be confirmed intraday.",
    }


def apply_multileg_direction_adjustment(flow: Dict[str, Any], multileg: Dict[str, Any]) -> Dict[str, Any]:
    adjusted = dict(flow)
    if multileg.get("possible_multileg") and multileg.get("direction_clarity") != "clear":
        adjusted["direction_confidence"] = "LOW"
        warning = adjusted.get("direction_warning") or ""
        adjusted["direction_warning"] = (warning + " Possible multi-leg structure reduces directional clarity.").strip()
    return adjusted
