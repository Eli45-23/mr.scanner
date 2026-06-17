from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


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


def _candidate(alert: Dict[str, Any]) -> Dict[str, Any]:
    return alert.get("candidate") if isinstance(alert.get("candidate"), dict) else alert


def infer_flow_bias(alert: Dict[str, Any]) -> str:
    candidate = _candidate(alert)
    option_type = str(candidate.get("option_type") or "").upper()
    direction = str(alert.get("direction_label") or candidate.get("direction_label") or "").lower()
    if "bearish" in direction:
        return "BEARISH"
    if "bullish" in direction:
        return "BULLISH"
    if option_type == "CALL":
        return "BULLISH"
    if option_type == "PUT":
        return "BEARISH"
    return "UNKNOWN"


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
    bias = infer_flow_bias(alert)
    if detected_at is None or base_price is None or base_price <= 0:
        return {
            "outcome_status": "missing_start_context",
            "flow_bias": bias,
            "base_price": base_price,
            "windows": [],
            "max_favorable_move_pct": None,
            "max_adverse_move_pct": None,
        }

    outcomes: List[OutcomeWindow] = []
    closes: List[float] = []
    for minutes in windows:
        target = detected_at.replace(microsecond=0) + __import__("datetime").timedelta(minutes=int(minutes))
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
        closes.append(close)
        outcomes.append(OutcomeWindow(int(minutes), round(close, 4), move_pct, favorable, "ok"))

    all_moves = [item.move_pct for item in outcomes if item.move_pct is not None]
    favorable_moves: List[float] = []
    adverse_moves: List[float] = []
    for move in all_moves:
        if bias == "BULLISH":
            favorable_moves.append(move)
            adverse_moves.append(move)
        elif bias == "BEARISH":
            favorable_moves.append(-move)
            adverse_moves.append(-move)
    return {
        "outcome_status": "ok" if outcomes else "no_windows",
        "flow_bias": bias,
        "base_price": round(base_price, 4),
        "windows": [item.to_dict() for item in outcomes],
        "max_favorable_move_pct": round(max(favorable_moves), 4) if favorable_moves else None,
        "max_adverse_move_pct": round(min(adverse_moves), 4) if adverse_moves else None,
    }


def summarize_outcomes(outcomes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(outcomes)
    finished = [row for row in rows if row.get("outcome_status") == "ok"]
    if not finished:
        return {"count": len(rows), "completed": 0, "favorable_rate": None, "average_max_favorable_move_pct": None}
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
        "favorable_rate": round(favorable_count / len(finished), 4),
        "average_max_favorable_move_pct": round(sum(max_favorable) / len(max_favorable), 4) if max_favorable else None,
    }
