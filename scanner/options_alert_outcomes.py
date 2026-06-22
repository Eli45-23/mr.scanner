from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


MARKET_TIMEZONE = ZoneInfo("America/New_York")
REGULAR_MARKET_CLOSE = datetime_time(16, 0)


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bar_time(bar: Dict[str, Any]) -> Optional[datetime]:
    return _parse_time(bar.get("t") or bar.get("timestamp") or bar.get("time"))


def _bar_close(bar: Dict[str, Any]) -> Optional[float]:
    return _safe_float(bar.get("c") if bar.get("c") is not None else bar.get("close"))


def _bar_high(bar: Dict[str, Any]) -> Optional[float]:
    return _safe_float(bar.get("h") if bar.get("h") is not None else bar.get("high") if bar.get("high") is not None else bar.get("c") if bar.get("c") is not None else bar.get("close"))


def _bar_low(bar: Dict[str, Any]) -> Optional[float]:
    return _safe_float(bar.get("l") if bar.get("l") is not None else bar.get("low") if bar.get("low") is not None else bar.get("c") if bar.get("c") is not None else bar.get("close"))


def _candidate(alert: Dict[str, Any]) -> Dict[str, Any]:
    return alert.get("candidate") if isinstance(alert.get("candidate"), dict) else alert


def _regular_close_for(timestamp: datetime) -> datetime:
    local = timestamp.astimezone(MARKET_TIMEZONE)
    close_local = datetime.combine(local.date(), REGULAR_MARKET_CLOSE, tzinfo=MARKET_TIMEZONE)
    return close_local.astimezone(timezone.utc)


def infer_flow_bias_details(alert: Dict[str, Any]) -> Dict[str, str]:
    candidate = _candidate(alert)
    option_type = str(candidate.get("option_type") or "").upper()
    direction_label = str(alert.get("direction_label") or candidate.get("direction_label") or "")
    direction = direction_label.lower()
    if "bearish" in direction:
        return {
            "flow_bias": "BEARISH",
            "flow_bias_source": "direction_label",
            "flow_bias_reason": f"Direction label says bearish: {direction_label}",
        }
    if "bullish" in direction:
        return {
            "flow_bias": "BULLISH",
            "flow_bias_source": "direction_label",
            "flow_bias_reason": f"Direction label says bullish: {direction_label}",
        }
    if option_type == "CALL":
        return {
            "flow_bias": "BULLISH",
            "flow_bias_source": "option_type_fallback",
            "flow_bias_reason": "No clear direction label, so CALL flow defaults bullish.",
        }
    if option_type == "PUT":
        return {
            "flow_bias": "BEARISH",
            "flow_bias_source": "option_type_fallback",
            "flow_bias_reason": "No clear direction label, so PUT flow defaults bearish.",
        }
    return {
        "flow_bias": "UNKNOWN",
        "flow_bias_source": "unknown",
        "flow_bias_reason": "No clear direction label or option type was available.",
    }


def infer_flow_bias(alert: Dict[str, Any]) -> str:
    return infer_flow_bias_details(alert)["flow_bias"]


@dataclass
class OutcomeWindow:
    minutes: int
    price: Optional[float]
    move_pct: Optional[float]
    favorable: Optional[bool]
    status: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "minutes": self.minutes,
            "price": self.price,
            "move_pct": self.move_pct,
            "favorable": self.favorable,
            "status": self.status,
        }


def _first_bar_at_or_after(bars: Iterable[Dict[str, Any]], target: datetime) -> Optional[Dict[str, Any]]:
    ordered = sorted(
        (bar for bar in bars if _bar_time(bar) is not None),
        key=lambda bar: _bar_time(bar) or target,
    )
    for bar in ordered:
        timestamp = _bar_time(bar)
        if timestamp and timestamp >= target:
            return bar
    return None


def evaluate_alert_outcome(
    alert: Dict[str, Any],
    bars: Iterable[Dict[str, Any]],
    *,
    windows: Iterable[int] = (5, 15, 30, 60),
) -> Dict[str, Any]:
    candidate = _candidate(alert)
    detected_at = _parse_time(alert.get("timestamp") or alert.get("time_detected") or candidate.get("time_detected"))
    base_price = _safe_float(candidate.get("underlying_price") or alert.get("underlying_price") or alert.get("price"))
    bias_details = infer_flow_bias_details(alert)
    bias = bias_details["flow_bias"]
    if detected_at is None or base_price is None or base_price <= 0:
        return {
            "outcome_status": "missing_start_context",
            **bias_details,
            "base_price": base_price,
            "windows": [],
            "completed_window_count": 0,
            "pending_window_count": 0,
            "insufficient_window_count": 0,
            "market_close_time": None,
            "max_favorable_move_pct": None,
            "max_adverse_move_pct": None,
        }

    market_close = _regular_close_for(detected_at)
    outcomes: List[OutcomeWindow] = []
    for minutes in windows:
        target = detected_at.replace(microsecond=0) + timedelta(minutes=int(minutes))
        if target > market_close:
            outcomes.append(OutcomeWindow(int(minutes), None, None, None, "insufficient_future_session"))
            continue
        bar = _first_bar_at_or_after(bars, target)
        close = _bar_close(bar or {})
        if close is None:
            outcomes.append(OutcomeWindow(int(minutes), None, None, None, "missing_bar"))
            continue
        move_pct = round(((close - base_price) / base_price) * 100.0, 4)
        favorable = None
        if bias == "BULLISH":
            favorable = move_pct > 0
        elif bias == "BEARISH":
            favorable = move_pct < 0
        outcomes.append(OutcomeWindow(int(minutes), round(close, 4), move_pct, favorable, "ok"))

    completed_windows = [item for item in outcomes if item.status == "ok" and item.move_pct is not None]
    pending_windows = [item for item in outcomes if item.status == "missing_bar"]
    insufficient_windows = [item for item in outcomes if item.status == "insufficient_future_session"]
    if not outcomes:
        outcome_status = "no_windows"
    elif completed_windows and not pending_windows and not insufficient_windows:
        outcome_status = "ok"
    elif completed_windows:
        outcome_status = "partial"
    elif insufficient_windows and not pending_windows:
        outcome_status = "insufficient_future_session"
    else:
        outcome_status = "pending"

    max_window = max((item.minutes for item in completed_windows), default=0)
    horizon = detected_at + timedelta(minutes=max_window)
    observed = [bar for bar in bars if (timestamp := _bar_time(bar)) and detected_at <= timestamp <= horizon]
    favorable_moves: List[float] = []
    adverse_moves: List[float] = []
    for bar in observed:
        high, low = _bar_high(bar), _bar_low(bar)
        if bias == "BULLISH":
            if high is not None: favorable_moves.append((high - base_price) / base_price * 100.0)
            if low is not None: adverse_moves.append((low - base_price) / base_price * 100.0)
        elif bias == "BEARISH":
            if low is not None: favorable_moves.append((base_price - low) / base_price * 100.0)
            if high is not None: adverse_moves.append((base_price - high) / base_price * 100.0)
    return {
        "outcome_status": outcome_status,
        **bias_details,
        "base_price": round(base_price, 4),
        "windows": [item.to_dict() for item in outcomes],
        "completed_window_count": len(completed_windows),
        "pending_window_count": len(pending_windows),
        "insufficient_window_count": len(insufficient_windows),
        "market_close_time": market_close.isoformat(),
        "max_favorable_move_pct": round(max(favorable_moves), 4) if favorable_moves else None,
        "max_adverse_move_pct": round(min(adverse_moves), 4) if adverse_moves else None,
    }


def summarize_outcomes(outcomes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(outcomes)
    def clean_completed(row: Dict[str, Any]) -> bool:
        if row.get("outcome_status") not in {"ok", "partial", "completed"}:
            return False
        windows = row.get("windows") or []
        return any(
            isinstance(item, dict)
            and item.get("status") in {"ok", "completed"}
            and _safe_float(item.get("move_pct")) is not None
            for item in windows
        )

    finished = [row for row in rows if clean_completed(row)]
    dirty = [
        row for row in rows
        if row.get("outcome_status") in {"ok", "partial", "completed"} and not clean_completed(row)
    ]
    pending = [row for row in rows if row.get("outcome_status") == "pending"]
    insufficient = [row for row in rows if row.get("outcome_status") == "insufficient_future_session"]
    if not finished:
        return {
            "count": len(rows),
            "completed": 0,
            "pending": len(pending),
            "insufficient_future_session": len(insufficient),
            "dirty_completed_ignored": len(dirty),
            "favorable_rate": None,
            "average_max_favorable_move_pct": None,
        }
    favorable_count = 0
    max_favorable: List[float] = []
    for row in finished:
        windows = row.get("windows") or []
        if any(item.get("favorable") is True for item in windows if isinstance(item, dict)):
            favorable_count += 1
        value = _safe_float(row.get("max_favorable_move_pct"))
        if value is not None:
            max_favorable.append(value)
    return {
        "count": len(rows),
        "completed": len(finished),
        "pending": len(pending),
        "insufficient_future_session": len(insufficient),
        "dirty_completed_ignored": len(dirty),
        "favorable_rate": round(favorable_count / len(finished), 4),
        "average_max_favorable_move_pct": round(sum(max_favorable) / len(max_favorable), 4) if max_favorable else None,
    }
