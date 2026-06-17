from __future__ import annotations

from typing import Any, Dict, Iterable

from scanner.options_whale_scoring import safe_float


def detect_block_print(candidate: Dict[str, Any], trades: Iterable[Dict[str, Any]] | None = None, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    config = config or {}
    min_premium = float(config.get("min_block_premium", config.get("min_premium", 100000)))
    min_size = int(config.get("min_block_size", 250))
    rows = [dict(t) for t in trades or [] if t]
    largest_size = 0
    largest_premium = 0.0
    for trade in rows:
        size = int(safe_float(trade.get("size") or trade.get("s") or trade.get("volume")))
        price = safe_float(trade.get("price") or trade.get("p") or candidate.get("last") or candidate.get("midpoint"))
        premium = size * price * 100
        if premium > largest_premium:
            largest_premium = premium
            largest_size = size
    if not rows:
        largest_size = int(safe_float(candidate.get("volume")))
        largest_premium = safe_float(candidate.get("estimated_premium"))
    possible = largest_size >= min_size and largest_premium >= min_premium
    return {
        "is_possible_block": possible,
        "block_size": largest_size if possible else None,
        "block_premium": round(largest_premium, 2) if possible else None,
        "block_reason": (
            f"Possible block-like print/volume jump: {largest_size} contracts, ${largest_premium:,.0f} premium."
            if possible else "No block-like size/premium threshold met."
        ),
        "block_warning": "Block prints may be spreads, hedges, rolls, or closing transactions." if possible else "",
    }
