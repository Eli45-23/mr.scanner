from __future__ import annotations

from typing import Any, Dict, List, Optional


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _trade_price(trade: Any) -> Optional[float]:
    value = _get(trade, "price", _get(trade, "p", None))
    return float(value) if isinstance(value, (int, float)) else None


def _trade_size(trade: Any) -> Optional[float]:
    value = _get(trade, "size", _get(trade, "s", None))
    return float(value) if isinstance(value, (int, float)) else None


def evaluate_pressure_score(
    pressure_data: Optional[Dict[str, Any]],
    config: Dict[str, Any],
    *,
    direction: str = "neutral",
) -> Dict[str, Any]:
    cfg = config.get("confirmation", {}).get("pressure_score", {})
    lookback = int(cfg.get("pressure_lookback_trades", 50))
    large_print_multiplier = float(cfg.get("large_print_multiplier", 3.0))
    max_spread_pct = float(cfg.get("max_spread_pct", 0.08))
    quote_imbalance_enabled = bool(cfg.get("enable_quote_imbalance", True))

    if not pressure_data:
        return {
            "pressure_score": 50,
            "pressure_label": "UNKNOWN",
            "bid_size": None,
            "ask_size": None,
            "spread": None,
            "trade_near_ask_count": 0,
            "trade_near_bid_count": 0,
            "large_print_count": 0,
            "reasons": [],
            "warnings": ["Trade/quote pressure data unavailable"],
        }

    quote = pressure_data.get("quote") or {}
    trades = list(pressure_data.get("trades") or [])[-lookback:]
    bid = _get(quote, "bid", _get(quote, "bp", _get(quote, "bid_price", None)))
    ask = _get(quote, "ask", _get(quote, "ap", _get(quote, "ask_price", None)))
    bid_size = _get(quote, "bid_size", _get(quote, "bs", None))
    ask_size = _get(quote, "ask_size", _get(quote, "as", None))
    bid = float(bid) if isinstance(bid, (int, float)) else None
    ask = float(ask) if isinstance(ask, (int, float)) else None
    bid_size = float(bid_size) if isinstance(bid_size, (int, float)) else None
    ask_size = float(ask_size) if isinstance(ask_size, (int, float)) else None

    if bid is None or ask is None or ask <= 0 or bid <= 0 or ask < bid:
        return {
            "pressure_score": 50,
            "pressure_label": "UNKNOWN",
            "bid_size": bid_size,
            "ask_size": ask_size,
            "spread": None,
            "trade_near_ask_count": 0,
            "trade_near_bid_count": 0,
            "large_print_count": 0,
            "reasons": [],
            "warnings": ["Quote bid/ask unavailable for pressure scoring"],
        }

    midpoint = (bid + ask) / 2
    spread = ask - bid
    spread_pct = (spread / midpoint) * 100 if midpoint else 0.0
    near_ask = 0
    near_bid = 0
    sizes = [_trade_size(trade) for trade in trades]
    sizes = [size for size in sizes if size is not None]
    avg_size = sum(sizes) / len(sizes) if sizes else 0.0
    large_print_count = 0
    large_print_against = 0

    for trade in trades:
        price = _trade_price(trade)
        size = _trade_size(trade) or 0.0
        if price is None:
            continue
        if price >= midpoint:
            near_ask += 1
            if avg_size and size >= avg_size * large_print_multiplier and direction == "bullish":
                large_print_count += 1
            elif avg_size and size >= avg_size * large_print_multiplier and direction == "bearish":
                large_print_against += 1
        else:
            near_bid += 1
            if avg_size and size >= avg_size * large_print_multiplier and direction == "bearish":
                large_print_count += 1
            elif avg_size and size >= avg_size * large_print_multiplier and direction == "bullish":
                large_print_against += 1

    reasons: List[str] = []
    warnings: List[str] = ["Top-of-book only; not full Level 2"]
    score = 50.0
    if near_ask > near_bid:
        score += min(20, (near_ask - near_bid) * 3)
        reasons.append("Trades are printing closer to the ask")
    elif near_bid > near_ask:
        score -= min(20, (near_bid - near_ask) * 3)
        reasons.append("Trades are printing closer to the bid")

    if quote_imbalance_enabled and bid_size is not None and ask_size is not None and bid_size + ask_size > 0:
        bid_imbalance = bid_size / (bid_size + ask_size)
        if bid_imbalance >= 0.65:
            score += 8
            reasons.append("Top-of-book bid size is stronger than ask size")
        elif bid_imbalance <= 0.35:
            score -= 8
            reasons.append("Top-of-book ask size is stronger than bid size")

    if large_print_count:
        score += 10
        reasons.append("Large prints align with setup direction")
    if large_print_against:
        score -= 12
        warnings.append("Large print appeared against setup direction")
    if spread_pct > max_spread_pct:
        score -= 8
        warnings.append("Spread is wide for pressure confirmation")

    label = "BALANCED"
    if score >= 62:
        label = "BUYERS_ACTIVE"
    elif score <= 38:
        label = "SELLERS_ACTIVE"
    if direction == "bearish" and label == "BUYERS_ACTIVE":
        warnings.append("Buyer pressure is opposing bearish setup")
    if direction == "bullish" and label == "SELLERS_ACTIVE":
        warnings.append("Seller pressure is opposing bullish setup")

    return {
        "pressure_score": int(max(0, min(100, round(score)))),
        "pressure_label": label,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread": round(spread, 4),
        "trade_near_ask_count": near_ask,
        "trade_near_bid_count": near_bid,
        "large_print_count": large_print_count,
        "reasons": reasons[:6],
        "warnings": warnings[:6],
    }
