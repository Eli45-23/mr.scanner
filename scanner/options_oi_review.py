from __future__ import annotations

from typing import Any, Dict, Iterable, List


def classify_next_day_oi(original: Dict[str, Any], next_day_open_interest: int | None) -> Dict[str, Any]:
    prior_oi = original.get("open_interest")
    if prior_oi is None:
        prior_oi = (original.get("candidate") or {}).get("open_interest")
    volume = original.get("volume")
    if volume is None:
        volume = (original.get("candidate") or {}).get("volume")
    try:
        prior = int(prior_oi or 0)
        next_oi = int(next_day_open_interest or 0)
        vol = int(volume or 0)
    except (TypeError, ValueError):
        prior, next_oi, vol = 0, 0, 0
    change = next_oi - prior
    likely_opening = vol > 0 and change >= max(1, int(vol * 0.5))
    likely_closing = vol > 0 and change <= -max(1, int(vol * 0.25))
    unclear = not likely_opening and not likely_closing
    return {
        "next_day_open_interest": next_oi,
        "open_interest_change": change,
        "likely_opening": likely_opening,
        "likely_closing": likely_closing,
        "likely_roll_or_unclear": unclear,
    }


def review_alerts_with_next_day_oi(alerts: Iterable[Dict[str, Any]], oi_by_contract: Dict[str, int]) -> List[Dict[str, Any]]:
    reviews: List[Dict[str, Any]] = []
    for alert in alerts:
        candidate = alert.get("candidate") or alert
        symbol = str(candidate.get("option_symbol") or "")
        if not symbol or symbol not in oi_by_contract:
            continue
        result = classify_next_day_oi(alert, oi_by_contract[symbol])
        reviews.append({
            "option_symbol": symbol,
            "underlying_symbol": candidate.get("underlying_symbol"),
            "original_time": alert.get("time_detected") or candidate.get("time_detected"),
            **result,
        })
    return reviews
