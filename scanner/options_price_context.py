from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from scanner.options_whale_scoring import safe_float


def _vwap(bars: List[Dict[str, Any]]) -> Optional[float]:
    pv = 0.0
    vol = 0.0
    for bar in bars:
        typical = (safe_float(bar.get("high") or bar.get("h")) + safe_float(bar.get("low") or bar.get("l")) + safe_float(bar.get("close") or bar.get("c"))) / 3
        v = safe_float(bar.get("volume") or bar.get("v"))
        pv += typical * v
        vol += v
    return round(pv / vol, 4) if vol > 0 else None


def classify_price_context(
    underlying_symbol: str,
    option_type: str,
    underlying_price: Optional[float],
    bars: Iterable[Dict[str, Any]] | None = None,
    market_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    rows = [dict(b) for b in bars or [] if b]
    market_context = market_context or {}
    price = safe_float(underlying_price)
    if price <= 0:
        return {
            "price_context": {"symbol": underlying_symbol, "status": "unavailable"},
            "price_context_score": 0,
            "price_confirmation_label": "stock price not confirming yet",
            "price_confirmation_score": 0,
            "price_confirmation_reason": "Underlying price context is unavailable.",
            "price_warning": "Underlying price data unavailable.",
        }
    highs = [safe_float(b.get("high") or b.get("h")) for b in rows]
    lows = [safe_float(b.get("low") or b.get("l")) for b in rows]
    closes = [safe_float(b.get("close") or b.get("c")) for b in rows]
    hod = max(highs) if highs else price
    lod = min(lows) if lows else price
    day_open = safe_float(rows[0].get("open") or rows[0].get("o")) if rows else price
    vwap = _vwap(rows)
    trend = "UP" if len(closes) >= 3 and closes[-1] > closes[0] else "DOWN" if len(closes) >= 3 and closes[-1] < closes[0] else "UNKNOWN"
    bullish_contract = str(option_type).upper() == "CALL"
    above_vwap = vwap is not None and price >= vwap
    breaking_hod = price >= hod * 0.998
    losing_lod = price <= lod * 1.002
    score = 0
    labels: List[str] = []
    if bullish_contract and trend == "UP":
        score += 3
        labels.append("flow aligns with upward trend")
    if not bullish_contract and trend == "DOWN":
        score += 3
        labels.append("flow aligns with downward trend")
    if bullish_contract and above_vwap:
        score += 3
        labels.append("above VWAP")
    if not bullish_contract and vwap is not None and not above_vwap:
        score += 3
        labels.append("below VWAP")
    if bullish_contract and breaking_hod:
        score += 4
        labels.append("flow aligns with breakout")
    if not bullish_contract and losing_lod:
        score += 4
        labels.append("flow aligns with breakdown")
    if not labels:
        labels.append("stock price not confirming yet")
    if market_context.get("chop_mode_active"):
        score = max(0, score - 3)
        labels.append("flow inside chop")
    label = "needs price confirmation"
    if score >= 7:
        label = "price action partially confirms"
    elif score <= 2:
        label = "stock price not confirming yet"
    return {
        "price_context": {
            "symbol": underlying_symbol,
            "current_price": price,
            "vwap": vwap,
            "hod": round(hod, 4),
            "lod": round(lod, 4),
            "day_open": round(day_open, 4),
            "trend_status": trend,
            "above_below_vwap": "above_vwap" if above_vwap else "below_vwap" if vwap else "unknown",
            "labels": labels,
        },
        "price_context_score": max(0, min(10, score)),
        "price_confirmation_label": label,
        "price_confirmation_score": max(0, min(10, score)),
        "price_confirmation_reason": "; ".join(labels),
        "price_warning": "" if score >= 4 else "Needs price confirmation.",
    }
