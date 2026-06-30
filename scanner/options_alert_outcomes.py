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
    serialized_windows = []
    for item in outcomes:
        payload = item.to_dict()
        signed = item.move_pct if bias == "BULLISH" else -item.move_pct if bias == "BEARISH" and item.move_pct is not None else None
        payload["signed_move_pct"] = round(signed, 4) if signed is not None else None
        payload["meaningful_0_10"] = signed is not None and signed >= 0.10
        payload["meaningful_0_20"] = signed is not None and signed >= 0.20
        serialized_windows.append(payload)
    return {
        "outcome_status": outcome_status,
        **bias_details,
        "base_price": round(base_price, 4),
        "windows": serialized_windows,
        "completed_window_count": len(completed_windows),
        "pending_window_count": len(pending_windows),
        "insufficient_window_count": len(insufficient_windows),
        "market_close_time": market_close.isoformat(),
        "max_favorable_move_pct": round(max(favorable_moves), 4) if favorable_moves else None,
        "max_adverse_move_pct": round(min(adverse_moves), 4) if adverse_moves else None,
    }


def evaluate_option_price_outcome(
    alert: Dict[str, Any],
    bars: Iterable[Dict[str, Any]],
    quotes: Iterable[Dict[str, Any]],
    *,
    windows: Iterable[int] = (5, 15, 30, 60),
    slippage_fraction_of_spread: float = 0.10,
    min_slippage: float = 0.01,
    max_slippage_fraction_of_price: float = 0.05,
) -> Dict[str, Any]:
    candidate = _candidate(alert)
    detected_at = _parse_time(alert.get("timestamp") or alert.get("time_detected") or candidate.get("time_detected"))
    aggression = str(alert.get("aggression_side") or candidate.get("aggression_side") or "").lower()
    side = "LONG" if aggression == "near_ask" else "SHORT" if aggression == "near_bid" else "UNKNOWN"
    reference_entry = _safe_float(candidate.get("contract_price_paid") or candidate.get("last") or candidate.get("midpoint"))
    entry_bid = _safe_float(candidate.get("bid"))
    entry_ask = _safe_float(candidate.get("ask"))
    if detected_at is None or reference_entry is None or reference_entry <= 0:
        return {"option_outcome_status": "missing_start_context", "option_position_side": side, "option_windows": []}

    def timestamp(row: Dict[str, Any]) -> Optional[datetime]:
        return _parse_time(row.get("t") or row.get("timestamp") or row.get("time"))

    ordered_bars = sorted((row for row in bars if timestamp(row)), key=lambda row: timestamp(row) or detected_at)
    ordered_quotes = sorted((row for row in quotes if timestamp(row)), key=lambda row: timestamp(row) or detected_at)

    def first_at_or_after(rows: List[Dict[str, Any]], target: datetime) -> Optional[Dict[str, Any]]:
        return next((row for row in rows if (timestamp(row) or target) >= target), None)

    entry_spread = max(0.0, (entry_ask or 0) - (entry_bid or 0)) if entry_bid and entry_ask else None
    base_for_cap = entry_ask if side == "LONG" else entry_bid if side == "SHORT" else reference_entry
    entry_slippage = min(max(min_slippage, (entry_spread or 0) * slippage_fraction_of_spread), max_slippage_fraction_of_price * base_for_cap) if entry_spread is not None and base_for_cap else None
    executable_entry = (entry_ask + entry_slippage) if side == "LONG" and entry_ask and entry_slippage is not None else (entry_bid - entry_slippage) if side == "SHORT" and entry_bid and entry_slippage is not None else None
    output_windows = []
    for minutes in windows:
        target = detected_at.replace(microsecond=0) + timedelta(minutes=int(minutes))
        bar = first_at_or_after(ordered_bars, target)
        quote = first_at_or_after(ordered_quotes, target)
        close = _bar_close(bar or {})
        raw_return = ((close - reference_entry) / reference_entry * 100.0) if close is not None else None
        signed_reference = raw_return if side != "SHORT" else -raw_return if raw_return is not None else None
        future_bid = _safe_float((quote or {}).get("bp") or (quote or {}).get("bid_price") or (quote or {}).get("bid"))
        future_ask = _safe_float((quote or {}).get("ap") or (quote or {}).get("ask_price") or (quote or {}).get("ask"))
        future_spread = max(0.0, future_ask - future_bid) if future_bid and future_ask else None
        exit_base = future_bid if side == "LONG" else future_ask if side == "SHORT" else None
        exit_slippage = min(max(min_slippage, (future_spread or 0) * slippage_fraction_of_spread), max_slippage_fraction_of_price * exit_base) if future_spread is not None and exit_base else None
        executable_exit = (future_bid - exit_slippage) if side == "LONG" and future_bid and exit_slippage is not None else (future_ask + exit_slippage) if side == "SHORT" and future_ask and exit_slippage is not None else None
        executable_return = None
        if executable_entry and executable_entry > 0 and executable_exit is not None:
            executable_return = ((executable_exit - executable_entry) / executable_entry * 100.0) if side == "LONG" else ((executable_entry - executable_exit) / executable_entry * 100.0)
        output_windows.append({
            "minutes": int(minutes), "status": "ok" if close is not None else "missing_bar",
            "reference_price": round(close, 4) if close is not None else None,
            "reference_return_pct": round(signed_reference, 4) if signed_reference is not None else None,
            "future_bid": future_bid, "future_ask": future_ask,
            "estimated_executable_return_pct": round(executable_return, 4) if executable_return is not None else None,
            "executable_status": "ok" if executable_return is not None else "historical_quote_unavailable",
        })
    reference_values = [item["reference_return_pct"] for item in output_windows if item["reference_return_pct"] is not None]
    return {
        "option_outcome_status": "ok" if reference_values else "option_bars_unavailable",
        "option_position_side": side,
        "option_entry_reference_price": round(reference_entry, 4),
        "option_entry_executable_price": round(executable_entry, 4) if executable_entry is not None else None,
        "option_entry_bid": entry_bid, "option_entry_ask": entry_ask,
        "option_slippage_model": "max($0.01, 10% of spread), capped at 5% of option price",
        "option_windows": output_windows,
        "option_max_favorable_return_pct": round(max(reference_values), 4) if reference_values else None,
        "option_max_adverse_return_pct": round(min(reference_values), 4) if reference_values else None,
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
