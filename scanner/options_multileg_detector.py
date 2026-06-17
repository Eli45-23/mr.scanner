from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List

from scanner.options_whale_scoring import safe_float


def detect_possible_multileg(candidates: Iterable[Dict[str, Any]], *, size_tolerance_pct: float = 20.0) -> Dict[str, Dict[str, Any]]:
    rows = [dict(c) for c in candidates if c]
    grouped: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row.get("underlying_symbol"), row.get("expiration"))].append(row)
    output: Dict[str, Dict[str, Any]] = {}
    for _, group in grouped.items():
        if len(group) < 2:
            continue
        for row in group:
            base_size = max(1.0, safe_float(row.get("volume")))
            related = [
                other for other in group
                if other.get("option_symbol") != row.get("option_symbol")
                and abs(safe_float(other.get("volume")) - base_size) / base_size * 100 <= size_tolerance_pct
            ]
            if not related:
                continue
            types = {str(item.get("option_type") or "").upper() for item in [row] + related}
            strikes = {safe_float(item.get("strike")) for item in [row] + related}
            if types == {"CALL"} and len(strikes) > 1:
                kind, clarity = "possible call spread", "mixed"
            elif types == {"PUT"} and len(strikes) > 1:
                kind, clarity = "possible put spread", "mixed"
            elif types == {"CALL", "PUT"} and len(strikes) == 1:
                kind, clarity = "possible straddle", "unclear"
            elif types == {"CALL", "PUT"}:
                kind, clarity = "possible strangle or hedge", "unclear"
            else:
                kind, clarity = "possible related multi-leg flow", "unclear"
            output[str(row.get("option_symbol"))] = {
                "possible_multileg": True,
                "multileg_type": kind,
                "linked_contracts": [str(item.get("option_symbol")) for item in related],
                "direction_clarity": clarity,
                "multileg_warning": "Possible multi-leg, spread, roll, or hedge; directional read is less certain.",
            }
    return output


def default_multileg_result() -> Dict[str, Any]:
    return {
        "possible_multileg": False,
        "multileg_type": "none",
        "linked_contracts": [],
        "direction_clarity": "clear",
        "multileg_warning": "",
    }
