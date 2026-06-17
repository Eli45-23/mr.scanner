from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from scanner.options_whale_scoring import safe_float


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def detect_sweep_activity(
    trades: Iterable[Dict[str, Any]],
    *,
    window_seconds: int = 10,
    min_trade_count: int = 3,
    min_total_premium: float = 100000,
) -> Dict[str, Any]:
    rows = [dict(t) for t in trades if t]
    if not rows:
        return {
            "is_possible_sweep": False,
            "sweep_detection_status": "limited",
            "sweep_reason": "sweep detection limited — trade prints unavailable.",
        }
    rows.sort(key=lambda item: str(item.get("timestamp") or item.get("t") or ""))
    start = _parse_time(rows[0].get("timestamp") or rows[0].get("t"))
    end = _parse_time(rows[-1].get("timestamp") or rows[-1].get("t"))
    elapsed = (end - start).total_seconds() if start and end else None
    total_volume = int(sum(safe_float(t.get("size") or t.get("s") or t.get("volume")) for t in rows))
    total_premium = sum(
        safe_float(t.get("premium"))
        or safe_float(t.get("price") or t.get("p")) * safe_float(t.get("size") or t.get("s") or t.get("volume")) * 100
        for t in rows
    )
    ask_side = sum(1 for t in rows if str(t.get("aggression_side") or "").lower() == "near_ask")
    bid_side = sum(1 for t in rows if str(t.get("aggression_side") or "").lower() == "near_bid")
    same_window = elapsed is None or elapsed <= window_seconds
    possible = len(rows) >= min_trade_count and total_premium >= min_total_premium and same_window
    group_source = "|".join(str(rows[0].get(k, "")) for k in ("underlying_symbol", "option_symbol", "timestamp", "t"))
    return {
        "is_possible_sweep": possible,
        "sweep_group_id": hashlib.sha1(group_source.encode("utf-8")).hexdigest()[:12] if possible else None,
        "sweep_trade_count": len(rows),
        "sweep_total_volume": total_volume,
        "sweep_total_premium": round(total_premium, 2),
        "sweep_time_window_seconds": elapsed,
        "sweep_reason": (
            f"{len(rows)} prints within {elapsed if elapsed is not None else 'unknown'} seconds totaling ${total_premium:,.0f}."
            if possible else "No sweep-like print cluster met threshold."
        ),
        "sweep_aggression_summary": "mostly_ask" if ask_side > bid_side else "mostly_bid" if bid_side > ask_side else "mixed",
    }


def approximate_sweep_from_snapshot(candidate: Dict[str, Any]) -> Dict[str, Any]:
    if candidate.get("trade_count") and int(candidate["trade_count"]) >= 3 and safe_float(candidate.get("estimated_premium")) >= 100000:
        return {
            "is_possible_sweep": True,
            "sweep_group_id": None,
            "sweep_trade_count": int(candidate["trade_count"]),
            "sweep_total_volume": int(safe_float(candidate.get("volume"))),
            "sweep_total_premium": safe_float(candidate.get("estimated_premium")),
            "sweep_time_window_seconds": None,
            "sweep_reason": "sweep detection limited — inferred from snapshot volume/trade count because prints were unavailable.",
        }
    return {
        "is_possible_sweep": False,
        "sweep_reason": "sweep detection limited — trade prints unavailable.",
    }
