from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional


def detect_missed_clean_entry(
    history: Iterable[Dict[str, Any]],
    *,
    setup_name: str,
    direction: str,
    current_stage: str,
    now: Optional[datetime] = None,
    lookback_minutes: int = 15,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if current_stage.upper() not in {"LATE", "DO_NOT_CHASE"}:
        return {"missed_clean_entry": False}
    cutoff = now - timedelta(minutes=lookback_minutes)
    for record in reversed(list(history)):
        try:
            timestamp = datetime.fromisoformat(str(record.get("timestamp")))
            timestamp = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if timestamp < cutoff:
            break
        if (
            str(record.get("setup_name") or "").lower() == setup_name.lower()
            and str(record.get("direction") or "").upper() == direction.upper()
            and str(record.get("stage") or "").upper() == "GOOD_POSITION"
        ):
            return {
                "missed_clean_entry": True,
                "previous_clean_setup_time": timestamp.isoformat(),
                "previous_clean_setup_name": record.get("setup_name"),
                "previous_clean_setup_score": record.get("score"),
                "missed_clean_entry_reason": (
                    f"Earlier {setup_name} was clean, but price is now extended from VWAP/EMA9."
                ),
                "lesson": "The clean entry was the pullback hold/rejection, not the chase candle.",
                "can_approve_trades": False,
            }
    return {"missed_clean_entry": False}
