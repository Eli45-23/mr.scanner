from __future__ import annotations

from typing import Any, Dict, List, Optional


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def midpoint(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return round((bid + ask) / 2.0, 4)


def spread_percent(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    mid = midpoint(bid, ask)
    if mid is None:
        return None
    return round(((ask - bid) / mid) * 100.0, 2)


def volume_oi_ratio(volume: Any, open_interest: Any) -> Optional[float]:
    vol = safe_float(volume)
    oi = safe_float(open_interest)
    if oi <= 0:
        return None
    return round(vol / oi, 2)


def estimated_premium(volume: Any, last: Any, bid: Any = None, ask: Any = None) -> float:
    vol = safe_float(volume)
    price = safe_float(last)
    if price <= 0:
        mid = midpoint(safe_float(bid), safe_float(ask))
        price = safe_float(mid)
    return round(max(0.0, vol * price * 100.0), 2)


def classify_score(score: int) -> str:
    if score >= 90:
        return "EXTREME WHALE FLOW"
    if score >= 80:
        return "HIGH WHALE FLOW"
    if score >= 75:
        return "POSSIBLE WHALE FLOW"
    if score >= 60:
        return "WATCH ONLY"
    return "IGNORE"


def score_options_whale_flow(candidate: Dict[str, Any], context: Dict[str, Any] | None = None, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    context = context or {}
    config = config or {}
    premium = safe_float(candidate.get("estimated_premium"))
    voi = candidate.get("volume_oi_ratio")
    spread = candidate.get("spread_percent")
    aggression = safe_float(candidate.get("aggression_score"))
    sweep = 15 if candidate.get("is_possible_sweep") else 0
    block = 8 if candidate.get("is_possible_block") else 0
    dte = int(candidate.get("dte") or 0)
    moneyness = str(candidate.get("moneyness") or "OTM").upper()
    price_context_score = safe_float(context.get("price_context_score") or candidate.get("price_context_score"))

    premium_score = min(20, int(premium / 25000))
    ratio_score = 0 if voi is None else min(15, int(safe_float(voi) * 5))
    aggression_score = min(20, int(aggression))
    sweep_score = min(15, sweep + block)
    liquidity_score = 0
    if spread is not None:
        if spread <= 5:
            liquidity_score = 10
        elif spread <= 10:
            liquidity_score = 7
        elif spread <= 15:
            liquidity_score = 4
    urgency_score = 5 if dte <= 1 else 3 if dte <= 7 else 0
    moneyness_score = 5 if moneyness in {"ATM", "ITM"} else 2
    context_score = min(10, int(price_context_score))

    components = {
        "premium_size": premium_score,
        "volume_oi_ratio": ratio_score,
        "trade_quote_aggression": aggression_score,
        "sweep_repeated_activity": sweep_score,
        "liquidity_spread_quality": liquidity_score,
        "expiration_urgency": urgency_score,
        "moneyness_quality": moneyness_score,
        "underlying_price_action_alignment": context_score,
    }
    total = max(0, min(100, sum(components.values())))
    reasons: List[str] = []
    if premium_score >= 12:
        reasons.append(f"Large estimated premium near ${premium:,.0f}.")
    if ratio_score >= 10:
        reasons.append(f"Volume/OI ratio is elevated at {safe_float(voi):.2f}x.")
    if aggression_score >= 12:
        reasons.append("Activity appears aggressive versus bid/ask context.")
    if sweep:
        reasons.append("Possible sweep-like repeated activity detected.")
    if block:
        reasons.append("Possible block-like premium concentration detected.")
    if liquidity_score <= 4:
        reasons.append("Spread/liquidity quality is a risk.")
    if context_score >= 7:
        reasons.append("Underlying price action provides some confirmation.")
    if not reasons:
        reasons.append("Flow is measurable but lacks strong confirming evidence.")

    return {
        "whale_score": total,
        "score_components": components,
        "classification": classify_score(total),
        "reason_summary": " ".join(reasons[:2]),
        "detailed_reasons": reasons,
    }
