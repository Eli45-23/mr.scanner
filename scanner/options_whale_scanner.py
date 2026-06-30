from __future__ import annotations

import math
import json
import re
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from scanner.options_block_detector import detect_block_print
from scanner.options_data_client import OptionsDataClient
from scanner.options_flow_classifier import classify_aggression, estimate_opening_flow, apply_multileg_direction_adjustment
from scanner.options_multileg_detector import default_multileg_result, detect_possible_multileg
from scanner.options_oi_review import review_alerts_with_next_day_oi
from scanner.options_price_context import classify_price_context
from scanner.options_sweep_detector import approximate_sweep_from_snapshot, detect_sweep_activity
from scanner.options_unusualness_baseline import OptionsUnusualnessBaseline
from scanner.options_universe import build_optionable_universe, default_universe_path, load_universe_cache, universe_status
from scanner.options_whale_models import DISCLAIMER, OptionFlowCandidate, utc_now_iso
from scanner.options_whale_scoring import classify_score, estimated_premium, midpoint, safe_float, score_options_whale_flow, spread_percent, volume_oi_ratio
from scanner.options_whale_storage import OptionsWhaleStorage


DEFAULT_PRIORITY_SEEDS = [
    "SPY", "QQQ", "IWM", "DIA", "NVDA", "AAPL", "TSLA", "AMD", "MSFT", "META",
    "AMZN", "GOOGL", "NFLX", "AVGO", "COIN", "MSTR", "SMH", "XLK", "XLF", "XLE",
    "XLV", "XLI", "XLY", "XLP", "XLU", "TLT", "HYG", "GLD", "SLV",
]

INDEX_0DTE_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA"}
MARKET_TIMEZONE = ZoneInfo("America/New_York")
OPTIONS_MARKET_OPEN = datetime_time(9, 30)
OPTIONS_MARKET_CLOSE = datetime_time(16, 0)

FORBIDDEN_ALERT_PHRASES = (
    "b" + "uy this",
    "s" + "ell this",
    "enter " + "now",
    "enter " + "trade",
    "guaran" + "teed",
    "confirmed smart " + "money",
)
FORBIDDEN_ALERT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(phrase) for phrase in FORBIDDEN_ALERT_PHRASES) + r")\b",
    re.IGNORECASE,
)


def default_options_whale_config() -> Dict[str, Any]:
    return {
        "enabled": True,
        "legacy_momentum_enabled": False,
        "full_market": True,
        "max_dte": 7,
        "include_0dte": True,
        "include_weeklies": True,
        "min_score": 75,
        "min_premium": 100000,
        "min_volume": 500,
        "min_volume_oi_ratio": 2.0,
        "max_spread_percent": 15,
        "scan_interval_seconds": 30,
        "max_contracts_per_scan": 10000,
        "max_results": 100,
        "enable_sweep_detection": True,
        "enable_block_detection": True,
        "enable_multileg_detection": True,
        "enable_price_action_context": True,
        "enable_next_day_oi_review": True,
        "enable_notifications": True,
        "notify_tier_2": False,
        "debug_loose_mode": False,
        "priority_seed_symbols": DEFAULT_PRIORITY_SEEDS,
        "priority_batch_size": 50,
        "always_scan_symbols": ["SPY", "QQQ", "IWM", "DIA", "AAPL"],
        "priority_contract_budget": 3000,
        "max_contracts_per_underlying": 600,
        "rotation_symbols_per_scan": 15,
        "rotation_safety_factor": 1.15,
        "contract_catalog_refresh_seconds": 900,
        "contract_catalog_max_per_symbol": 5000,
        "coverage_warning_age_seconds": 300,
        "active_episode_quote_minutes": 75,
        "active_episode_quote_limit": 500,
        "index_0dte_min_score": 85,
        "index_0dte_min_premium": 250000,
        "index_0dte_max_spread_percent": 8,
        "index_0dte_min_price_confirmation_score": 6,
        "flow_episode_bucket_minutes": 5,
        "symbol_bias_memory_enabled": True,
        "symbol_bias_memory_window_minutes": 15,
        "symbol_bias_memory_min_completed": 20,
        "symbol_bias_memory_weak_rate": 0.45,
        "symbol_bias_memory_strong_rate": 0.55,
        "symbol_bias_memory_penalty": 5,
        "symbol_bias_memory_half_life_sessions": 2,
        "symbol_bias_memory_contradiction_min_completed": 5,
        "symbol_bias_memory_break_min_completed": 10,
        "symbol_bias_memory_contradiction_gap": 0.20,
        "reliability_calibration_enabled": True,
        "reliability_min_effective_samples": 30,
        "reliability_min_sessions": 20,
        "reliability_history_days": 60,
        "reliability_half_life_days": 5,
        "reliability_prior_successes": 10,
        "reliability_prior_failures": 10,
        "reliability_max_penalty": 8,
        "reliability_max_bonus": 5,
        "bearish_tier1_min_score": 90,
        "bearish_tier1_min_price_context": 8,
        "zero_dte_tier1_min_score": 90,
        "zero_dte_tier1_min_premium": 250000,
        "zero_dte_tier1_max_spread_percent": 8,
        "zero_dte_tier1_min_price_context": 8,
        "strict_cohort_min_meaningful_rate": 0.30,
    }


def whale_config(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = default_options_whale_config()
    merged.update(config.get("options_whale_scanner", {}))
    return merged


def options_market_session_state(now: Optional[datetime] = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(MARKET_TIMEZONE)
    if local.weekday() >= 5:
        return "closed"
    local_time = local.time()
    return "regular" if OPTIONS_MARKET_OPEN <= local_time < OPTIONS_MARKET_CLOSE else "closed"


def _contract_symbol(row: Dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("option_symbol") or row.get("id") or "").upper()


def _contract_underlying(row: Dict[str, Any]) -> str:
    value = row.get("underlying_symbol") or row.get("underlying_asset_symbol") or row.get("root_symbol")
    if value:
        return str(value).upper()
    match = re.match(r"^([A-Z]+)\d{6}[CP]\d+", _contract_symbol(row))
    return match.group(1) if match else ""


def _contract_type(row: Dict[str, Any]) -> str:
    raw = str(row.get("type") or row.get("option_type") or "").upper()
    if raw in {"CALL", "C"}:
        return "CALL"
    if raw in {"PUT", "P"}:
        return "PUT"
    match = re.search(r"\d{6}([CP])\d+", _contract_symbol(row))
    return "CALL" if match and match.group(1) == "C" else "PUT" if match else "UNKNOWN"


def _contract_expiration(row: Dict[str, Any]) -> str:
    raw = row.get("expiration_date") or row.get("expiration")
    if raw:
        return str(raw)[:10]
    match = re.search(r"(\d{6})[CP]\d+", _contract_symbol(row))
    if not match:
        return date.today().isoformat()
    text = match.group(1)
    return f"20{text[:2]}-{text[2:4]}-{text[4:6]}"


def _contract_strike(row: Dict[str, Any]) -> float:
    raw = row.get("strike_price") or row.get("strike")
    if raw is not None:
        return safe_float(raw)
    match = re.search(r"[CP](\d{8})$", _contract_symbol(row))
    return safe_float(match.group(1)) / 1000 if match else 0.0


def _snapshot_quote(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return snapshot.get("latestQuote") or snapshot.get("latest_quote") or snapshot.get("q") or {}


def _snapshot_trade(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return snapshot.get("latestTrade") or snapshot.get("latest_trade") or snapshot.get("t") or {}


def _snapshot_bar(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return snapshot.get("dailyBar") or snapshot.get("daily_bar") or snapshot.get("day") or {}


def _timestamp(raw: Dict[str, Any]) -> Optional[str]:
    value = raw.get("t") or raw.get("timestamp") or raw.get("time")
    return str(value) if value else None


def _normalize_iso_timestamp(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.match(r"^(?P<prefix>.+?\.)(?P<fraction>\d{7,})(?P<suffix>Z|[+-]\d{2}:?\d{2})?$", text)
    if match:
        text = f"{match.group('prefix')}{match.group('fraction')[:6]}{match.group('suffix') or ''}"
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return text


def _quote_age_seconds(timestamp: Optional[str], now: datetime) -> Optional[float]:
    normalized = _normalize_iso_timestamp(timestamp)
    if not normalized:
        return None
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds())
    except ValueError:
        return None


def _parse_iso_time(value: Any) -> Optional[datetime]:
    normalized = _normalize_iso_timestamp(value)
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _moneyness(option_type: str, strike: float, underlying_price: Optional[float]) -> tuple[str, Optional[float], Optional[float]]:
    if not underlying_price or underlying_price <= 0 or strike <= 0:
        return "UNKNOWN", None, None
    distance = strike - underlying_price
    pct = distance / underlying_price * 100
    if abs(pct) <= 1.0:
        label = "ATM"
    elif (option_type == "CALL" and strike < underlying_price) or (option_type == "PUT" and strike > underlying_price):
        label = "ITM"
    else:
        label = "OTM"
    return label, round(distance, 4), round(pct, 2)


def build_premium_timing_fields(candidate: Dict[str, Any]) -> Dict[str, Any]:
    trade_time = candidate.get("trade_time")
    quote_time = candidate.get("quote_time")
    detected_time = candidate.get("time_detected")
    trade_dt = _parse_iso_time(trade_time)
    detected_dt = _parse_iso_time(detected_time)
    delay = None
    warning = ""
    stale_trade_print = False
    fresh_flow_label = "timing unavailable"
    trade_print_age_warning = ""
    if trade_dt and detected_dt:
        delay = max(0.0, round((detected_dt - trade_dt).total_seconds(), 2))
        if delay > 120:
            warning = "Premium timing is delayed; verify the print before relying on it."
            stale_trade_print = True
            fresh_flow_label = "old trade print"
            trade_print_age_warning = "Reported trade printed more than 2 minutes before scanner detection."
        else:
            fresh_flow_label = "fresh premium print"
        if delay > 900:
            fresh_flow_label = "stale / old premium print"
            trade_print_age_warning = "Reported trade printed more than 15 minutes before scanner detection; do not treat this as fresh flow."
    elif not trade_dt:
        warning = "Reported trade time unavailable."
        trade_print_age_warning = "Trade timestamp unavailable."
    elif not detected_dt:
        warning = "Scanner detection time unavailable."
        trade_print_age_warning = "Scanner detection time unavailable."
    summary_parts = []
    if trade_time:
        summary_parts.append(f"trade {trade_time}")
    if quote_time:
        summary_parts.append(f"quote {quote_time}")
    if delay is not None:
        summary_parts.append(f"detected {delay:g}s later")
    return {
        "reported_trade_time": trade_time,
        "reported_quote_time": quote_time,
        "scanner_detected_time": detected_time,
        "premium_trade_delay_seconds": delay,
        "stale_trade_print": stale_trade_print,
        "trade_print_age_seconds": delay,
        "trade_print_age_minutes": round(delay / 60, 2) if delay is not None else None,
        "trade_print_age_warning": trade_print_age_warning,
        "fresh_flow_label": fresh_flow_label,
        "premium_timing_summary": " | ".join(summary_parts) if summary_parts else "premium timing unavailable",
        "premium_timing_warning": warning,
    }


def build_premium_display_fields(candidate: Dict[str, Any]) -> Dict[str, Any]:
    price_paid = safe_float(candidate.get("last"))
    if price_paid <= 0:
        price_paid = safe_float(candidate.get("midpoint"))
    price_paid = price_paid if price_paid > 0 else None
    premium_per_contract = round(price_paid * 100, 2) if price_paid is not None else None
    total_premium = safe_float(candidate.get("estimated_premium"))
    option_type = str(candidate.get("option_type") or "").upper()
    display_contract = " ".join(str(part) for part in (
        candidate.get("underlying_symbol"),
        option_type,
        candidate.get("strike"),
        candidate.get("expiration"),
    ) if part not in {None, ""})
    moneyness = str(candidate.get("moneyness") or "UNKNOWN")
    distance = candidate.get("distance_percent")
    display_moneyness = moneyness if distance is None else f"{moneyness} ({safe_float(distance):+.2f}%)"
    summary = "premium unavailable"
    if price_paid is not None and total_premium > 0:
        summary = f"${price_paid:.2f} per contract; approx ${total_premium:,.0f} total premium"
    elif price_paid is not None:
        summary = f"${price_paid:.2f} per contract"
    return {
        "display_contract": display_contract,
        "display_moneyness": display_moneyness,
        "contract_price_paid": price_paid,
        "premium_per_contract": premium_per_contract,
        "premium_summary": summary,
    }


def build_premium_pressure_fields(result_or_candidate: Dict[str, Any]) -> Dict[str, Any]:
    side = str(result_or_candidate.get("aggression_side") or "").lower()
    confidence = result_or_candidate.get("aggression_confidence") or result_or_candidate.get("direction_confidence") or "LOW"
    reason = result_or_candidate.get("bid_ask_reason") or result_or_candidate.get("direction_warning") or ""
    if side == "near_ask":
        label = "ask-side pressure"
    elif side == "near_bid":
        label = "bid-side pressure"
    elif side == "midpoint":
        label = "midpoint / unclear"
    else:
        label = "unknown"
        reason = reason or "Bid/ask pressure could not be classified from available quote data."
    return {
        "premium_pressure_label": label,
        "premium_pressure_confidence": confidence,
        "premium_pressure_reason": reason,
    }


def _row_candidate(row: Dict[str, Any]) -> Dict[str, Any]:
    return row.get("candidate") if isinstance(row.get("candidate"), dict) else row


def _key_value(row: Dict[str, Any], field: str) -> Any:
    candidate = _row_candidate(row)
    value = candidate.get(field)
    return value if value is not None and value != "" else row.get(field)


def _key_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return round(safe_float(value), 4)


def _key_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(round(safe_float(value)))


def build_whale_print_key(row: Dict[str, Any]) -> tuple[Any, ...]:
    price = _key_value(row, "last")
    if price is None or price == "":
        price = _key_value(row, "contract_price_paid")
    if price is None or price == "":
        price = _key_value(row, "midpoint")
    return (
        str(_key_value(row, "option_symbol") or "").upper(),
        str(_key_value(row, "trade_time") or _key_value(row, "reported_trade_time") or ""),
        _key_float(price),
        _key_int(_key_value(row, "volume")),
        _key_float(_key_value(row, "estimated_premium")),
        _key_int(_key_value(row, "open_interest")),
    )


def build_notification_event_key(row: Dict[str, Any]) -> tuple[str, str, str]:
    """Stable identity for one observed option trade across repeated scans."""
    return (
        str(_key_value(row, "option_symbol") or "").upper(),
        str(_key_value(row, "trade_time") or _key_value(row, "reported_trade_time") or ""),
        str(_key_value(row, "direction_label") or "").upper(),
    )


def infer_direction_bias_label(row: Dict[str, Any]) -> str:
    explicit = str(row.get("flow_episode_bias") or row.get("flow_bias") or "").upper()
    if explicit in {"BULLISH", "BEARISH"}:
        return explicit
    label = str(_key_value(row, "direction_label") or row.get("direction_label") or "").lower()
    option_type = str(_key_value(row, "option_type") or "").upper()
    if "bearish" in label:
        return "BEARISH"
    if "bullish" in label:
        return "BULLISH"
    if option_type == "CALL":
        return "BULLISH"
    if option_type == "PUT":
        return "BEARISH"
    return "UNKNOWN"


def _time_bucket(value: Any, bucket_minutes: int) -> str:
    timestamp = _parse_iso_time(value)
    if not timestamp:
        return ""
    bucket_minutes = max(1, int(bucket_minutes or 5))
    minute = (timestamp.minute // bucket_minutes) * bucket_minutes
    bucketed = timestamp.replace(minute=minute, second=0, microsecond=0)
    return bucketed.isoformat().replace("+00:00", "Z")


def build_flow_episode_key(row: Dict[str, Any], bucket_minutes: int = 5) -> tuple[str, str, str]:
    """Group related strikes/expirations into one same-symbol, same-bias time episode."""
    return (
        str(_key_value(row, "underlying_symbol") or _key_value(row, "underlying") or "").upper(),
        infer_direction_bias_label(row),
        _time_bucket(_key_value(row, "time_detected") or row.get("scanner_detected_time") or row.get("timestamp"), bucket_minutes),
    )


def attach_flow_episode_context(rows: List[Dict[str, Any]], bucket_minutes: int = 5) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        key = build_flow_episode_key(row, bucket_minutes)
        if all(key):
            grouped.setdefault(key, []).append(row)
    for key, group in grouped.items():
        if not group:
            continue
        sorted_group = sorted(
            group,
            key=lambda item: (
                int(item.get("whale_score") or item.get("score") or 0),
                safe_float(_key_value(item, "estimated_premium")),
            ),
            reverse=True,
        )
        leader = sorted_group[0]
        leader_symbol = str(_key_value(leader, "option_symbol") or "")
        total_premium = sum(safe_float(_key_value(item, "estimated_premium")) for item in group)
        strikes = sorted({_key_value(item, "strike") for item in group if _key_value(item, "strike") not in (None, "")}, key=lambda value: safe_float(value))
        expirations = sorted({str(_key_value(item, "expiration")) for item in group if _key_value(item, "expiration")})
        episode_id = "|".join(key)
        for item in group:
            is_leader = item is leader
            item.update({
                "flow_episode_id": episode_id,
                "flow_episode_symbol": key[0],
                "flow_episode_bias": key[1],
                "flow_episode_bucket": key[2],
                "flow_episode_size": len(group),
                "flow_episode_total_premium": round(total_premium, 2),
                "flow_episode_leader": is_leader,
                "flow_episode_leader_option": leader_symbol,
                "flow_episode_strikes": strikes,
                "flow_episode_expirations": expirations,
                "flow_episode_reason": (
                    f"{len(group)} related same-symbol/same-bias contracts detected in a {bucket_minutes}-minute bucket."
                    if len(group) > 1 else "Single-contract flow episode."
                ),
            })
    return rows


def build_outcome_alert_key(row: Dict[str, Any]) -> str:
    candidate = _row_candidate(row)
    return "|".join(str(part or "") for part in (
        row.get("timestamp") or row.get("time_detected") or candidate.get("time_detected"),
        candidate.get("underlying_symbol"),
        candidate.get("option_symbol"),
        row.get("whale_score") or row.get("score"),
    ))


def latest_outcomes_by_key(outcomes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in outcomes:
        key = str(row.get("alert_key") or "")
        if not key:
            continue
        if key not in latest or str(row.get("reviewed_at") or "") >= str(latest[key].get("reviewed_at") or ""):
            latest[key] = row
    return latest


def outcome_completion_fields(outcome: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not outcome:
        return {
            "outcome_completeness_label": "Outcome not reviewed yet",
            "outcome_completeness_status": "pending_review",
            "outcome_completed_windows": 0,
            "outcome_total_windows": 4,
            "outcome_missing_windows": 4,
            "outcome_favorable_windows": 0,
        }
    windows = outcome.get("windows") if isinstance(outcome.get("windows"), list) else []
    total = len(windows) or len(outcome.get("outcome_window_minutes_requested") or []) or 4
    completed = sum(1 for item in windows if item.get("status") == "ok")
    favorable = sum(1 for item in windows if item.get("status") == "ok" and item.get("favorable") is True)
    missing = max(0, total - completed)
    status = "complete" if completed >= total and total else "partial" if completed else str(outcome.get("outcome_status") or "pending")
    label = f"{completed}/{total} outcome windows complete"
    if missing:
        label += f" ({missing} incomplete)"
    return {
        "outcome_reviewed_at": outcome.get("reviewed_at"),
        "outcome_status": outcome.get("outcome_status"),
        "outcome_completeness_label": label,
        "outcome_completeness_status": status,
        "outcome_completed_windows": completed,
        "outcome_total_windows": total,
        "outcome_missing_windows": missing,
        "outcome_favorable_windows": favorable,
        "outcome_favorable_rate": round(favorable / completed, 4) if completed else None,
    }


def attach_outcome_completeness(rows: List[Dict[str, Any]], outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key = latest_outcomes_by_key(outcomes)
    for row in rows:
        row.update(outcome_completion_fields(by_key.get(build_outcome_alert_key(row))))
    return rows


def build_symbol_bias_memory(outcomes: List[Dict[str, Any]], *, window_minutes: int = 15, min_completed: int = 20, weak_rate: float = 0.45, strong_rate: float = 0.55, half_life_sessions: float = 2.0) -> Dict[tuple[str, str], Dict[str, Any]]:
    buckets: Dict[tuple[str, str], Dict[str, Any]] = {}
    latest_date = max(((_parse_iso_time(row.get("detected_at") or row.get("reviewed_at")) or datetime.min.replace(tzinfo=timezone.utc)).date() for row in outcomes), default=datetime.now(timezone.utc).date())
    for row in latest_outcomes_by_key(outcomes).values():
        symbol = str(row.get("underlying_symbol") or "").upper()
        bias = str(row.get("flow_bias") or "UNKNOWN").upper()
        if not symbol or bias == "UNKNOWN":
            continue
        target = None
        for window in row.get("windows") or []:
            if int(window.get("minutes") or 0) == int(window_minutes) and window.get("status") == "ok":
                target = window
                break
        if not target or target.get("favorable") is None:
            continue
        stamp = _parse_iso_time(row.get("detected_at") or row.get("reviewed_at"))
        age_days = max(0, (latest_date - stamp.date()).days) if stamp else 0
        weight = 0.5 ** (age_days / max(0.1, float(half_life_sessions)))
        bucket = buckets.setdefault((symbol, bias), {"completed": 0, "favorable": 0, "weighted_completed": 0.0, "weighted_favorable": 0.0, "current_completed": 0, "current_favorable": 0})
        bucket["completed"] += 1
        bucket["weighted_completed"] += weight
        if target.get("favorable") is True:
            bucket["favorable"] += 1
            bucket["weighted_favorable"] += weight
        if stamp and stamp.date() == latest_date:
            bucket["current_completed"] += 1
            if target.get("favorable") is True:
                bucket["current_favorable"] += 1
    memory: Dict[tuple[str, str], Dict[str, Any]] = {}
    for key, bucket in buckets.items():
        completed = bucket["completed"]
        favorable = bucket["favorable"]
        rate = bucket["weighted_favorable"] / bucket["weighted_completed"] if bucket["weighted_completed"] else 0.0
        current_completed = int(bucket["current_completed"])
        current_rate = bucket["current_favorable"] / current_completed if current_completed else None
        if completed < int(min_completed):
            label = "insufficient_history"
        elif rate < float(weak_rate):
            label = "weak_recent_follow_through"
        elif rate > float(strong_rate):
            label = "strong_recent_follow_through"
        else:
            label = "mixed_recent_follow_through"
        memory[key] = {
            "symbol_bias_memory_label": label,
            "symbol_bias_memory_window": int(window_minutes),
            "symbol_bias_memory_completed": completed,
            "symbol_bias_memory_favorable": favorable,
            "symbol_bias_memory_rate": round(rate, 4),
            "symbol_bias_memory_effective_samples": round(bucket["weighted_completed"], 2),
            "symbol_bias_memory_current_completed": current_completed,
            "symbol_bias_memory_current_rate": round(current_rate, 4) if current_rate is not None else None,
        }
    return memory


def apply_symbol_bias_memory(result: Dict[str, Any], memory: Dict[tuple[str, str], Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    candidate = result.get("candidate") or {}
    symbol = str(candidate.get("underlying_symbol") or "").upper()
    bias = infer_direction_bias_label(result)
    learned = memory.get((symbol, bias))
    if not learned:
        result.update({
            "learned_quality_score": None,
            "learned_quality_reason": "not enough outcome history yet",
            "symbol_bias_memory_label": "no_history",
        })
        return result
    result.update(learned)
    rate = learned.get("symbol_bias_memory_rate")
    completed = learned.get("symbol_bias_memory_completed")
    window = learned.get("symbol_bias_memory_window")
    label = learned.get("symbol_bias_memory_label")
    if label == "weak_recent_follow_through":
        penalty = int(cfg.get("symbol_bias_memory_penalty", 5))
        current_completed = int(learned.get("symbol_bias_memory_current_completed") or 0)
        current_rate = learned.get("symbol_bias_memory_current_rate")
        gap = float(cfg.get("symbol_bias_memory_contradiction_gap", 0.20))
        if current_rate is not None and current_completed >= int(cfg.get("symbol_bias_memory_break_min_completed", 10)) and float(current_rate) >= float(cfg.get("symbol_bias_memory_strong_rate", 0.55)):
            penalty = 0
            result["symbol_bias_memory_label"] = "regime_break"
        elif current_rate is not None and current_completed >= int(cfg.get("symbol_bias_memory_contradiction_min_completed", 5)) and float(current_rate) - float(rate) >= gap:
            penalty = max(0, penalty // 2)
            result["symbol_bias_memory_label"] = "contradicted_recent_memory"
        original_score = int(result.get("whale_score") or 0)
        result["pre_memory_whale_score"] = original_score
        result["whale_score"] = max(0, original_score - penalty)
        result["noise_adjusted_score"] = result["whale_score"]
        result["classification"] = classify_score(result["whale_score"])
        result["learned_quality_score"] = round(float(rate), 4)
        result["learned_quality_reason"] = f"Recent {symbol} {bias.lower()} flow has weak {window}m follow-through ({rate:.0%} over {completed} completed windows); score reduced by {penalty}."
        warnings = list(result.get("score_warnings") or [])
        warnings.append("weak symbol/bias follow-through memory")
        result["score_warnings"] = warnings
    elif label == "strong_recent_follow_through":
        result["learned_quality_score"] = round(float(rate), 4)
        result["learned_quality_reason"] = f"Recent {symbol} {bias.lower()} flow has strong {window}m follow-through ({rate:.0%} over {completed} completed windows)."
    elif label == "mixed_recent_follow_through":
        result["learned_quality_score"] = round(float(rate), 4)
        result["learned_quality_reason"] = f"Recent {symbol} {bias.lower()} flow is mixed at {window}m ({rate:.0%} over {completed} completed windows)."
    else:
        result["learned_quality_score"] = round(float(rate), 4)
        result["learned_quality_reason"] = f"Only {completed} completed {window}m windows for recent {symbol} {bias.lower()} flow; not enough history to adjust confidence."
    return result


def _reliability_dte_bucket(value: Any) -> str:
    dte = int(safe_float(value))
    return "0DTE" if dte <= 0 else "1-2DTE" if dte <= 2 else "3-7DTE"


def _reliability_score_bucket(value: Any) -> str:
    score = int(safe_float(value))
    return "90+" if score >= 90 else "80-89" if score >= 80 else "75-79"


def build_reliability_table(outcomes: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[tuple[str, str, str, str, str], Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    history_days = max(1, int(cfg.get("reliability_history_days", 20)))
    half_life = max(0.1, float(cfg.get("reliability_half_life_days", 5)))
    buckets: Dict[tuple[str, str, str, str, str], Dict[str, Any]] = {}
    score_totals: Dict[str, Dict[str, float]] = {}
    for row in latest_outcomes_by_key(outcomes).values():
        stamp = _parse_iso_time(row.get("detected_at") or row.get("reviewed_at"))
        if stamp and (now - stamp).days > history_days:
            continue
        window = next((item for item in row.get("windows") or [] if int(item.get("minutes") or 0) == 15 and item.get("status") == "ok"), None)
        if not window:
            continue
        signed = window.get("signed_move_pct")
        if signed is None:
            move = safe_float(window.get("move_pct"))
            signed = move if str(row.get("flow_bias")) == "BULLISH" else -move
        key = (
            _reliability_score_bucket(row.get("whale_score")),
            str(row.get("dte_bucket") or _reliability_dte_bucket(row.get("dte"))),
            str(row.get("direction_confidence") or "UNKNOWN").upper(),
            str(row.get("market_regime") or "UNKNOWN").upper(),
            str(row.get("flow_bias") or "UNKNOWN").upper(),
        )
        age = max(0.0, (now - stamp).total_seconds() / 86400.0) if stamp else 0.0
        weight = 0.5 ** (age / half_life)
        bucket = buckets.setdefault(key, {"effective_samples": 0.0, "successes": 0.0, "raw_samples": 0.0, "sessions": set()})
        bucket["effective_samples"] += weight
        bucket["raw_samples"] += 1
        if stamp:
            bucket["sessions"].add(stamp.date().isoformat())
        score_total = score_totals.setdefault(key[0], {"samples": 0.0, "successes": 0.0})
        score_total["samples"] += weight
        if float(signed) >= 0.10:
            bucket["successes"] += weight
            score_total["successes"] += weight
    score_rates = {name: value["successes"] / value["samples"] for name, value in score_totals.items() if value["samples"]}
    guard_active = bool(score_rates.get("90+") is not None and score_rates.get("80-89") is not None and score_rates["90+"] <= score_rates["80-89"])
    guard_reason = "90+ score outcomes do not outperform 80-89 outcomes." if guard_active else ""
    table: Dict[tuple[str, str, str, str, str], Dict[str, Any]] = {}
    prior_success = float(cfg.get("reliability_prior_successes", 10))
    prior_failure = float(cfg.get("reliability_prior_failures", 10))
    for key, bucket in buckets.items():
        posterior = (bucket["successes"] + prior_success) / (bucket["effective_samples"] + prior_success + prior_failure)
        adjustment = round((posterior - 0.50) * 20)
        adjustment = max(-int(cfg.get("reliability_max_penalty", 8)), min(int(cfg.get("reliability_max_bonus", 5)), adjustment))
        session_count = len(bucket["sessions"])
        qualified = session_count >= int(cfg.get("reliability_min_sessions", 20)) and bucket["effective_samples"] >= float(cfg.get("reliability_min_effective_samples", 30))
        if guard_active and adjustment > 0:
            adjustment = 0
        table[key] = {"reliability_rate": round(posterior, 4), "reliability_effective_samples": round(bucket["effective_samples"], 2), "reliability_raw_samples": int(bucket["raw_samples"]), "reliability_session_count": session_count, "reliability_qualified": qualified, "reliability_score_adjustment": adjustment, "calibration_guard_active": guard_active, "calibration_guard_reason": guard_reason}
    return table


def apply_reliability_adjustment(result: Dict[str, Any], table: Dict[tuple[str, str, str, str, str], Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    candidate = result.get("candidate") or {}
    key = (_reliability_score_bucket(result.get("whale_score")), str(candidate.get("dte_bucket") or _reliability_dte_bucket(candidate.get("dte"))), str(result.get("direction_confidence") or candidate.get("direction_confidence") or "UNKNOWN").upper(), str(result.get("market_regime") or "UNKNOWN").upper(), infer_direction_bias_label(result).upper())
    learned = table.get(key)
    if not learned or not learned.get("reliability_qualified"):
        result.update({"reliability_bucket": "|".join(key), "reliability_status": "insufficient_history", "reliability_score_adjustment": 0})
        return result
    adjustment = int(learned.get("reliability_score_adjustment") or 0)
    original = int(result.get("whale_score") or 0)
    result.update(learned)
    result.update({"pre_reliability_whale_score": original, "whale_score": max(0, min(100, original + adjustment)), "noise_adjusted_score": max(0, min(100, original + adjustment)), "reliability_bucket": "|".join(key), "reliability_status": "applied"})
    result["classification"] = classify_score(result["whale_score"])
    return result


def _row_completeness(row: Dict[str, Any]) -> int:
    candidate = _row_candidate(row)
    values = list(row.values()) + list(candidate.values())
    return sum(1 for value in values if value not in (None, "", [], {}))


def _prefer_whale_print(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    existing_score = int(existing.get("whale_score") or existing.get("score") or 0)
    incoming_score = int(incoming.get("whale_score") or incoming.get("score") or 0)
    if incoming_score != existing_score:
        return incoming if incoming_score > existing_score else existing
    existing_dt = _parse_iso_time(_key_value(existing, "time_detected") or existing.get("timestamp"))
    incoming_dt = _parse_iso_time(_key_value(incoming, "time_detected") or incoming.get("timestamp"))
    if incoming_dt != existing_dt:
        return incoming if incoming_dt and (not existing_dt or incoming_dt > existing_dt) else existing
    return incoming if _row_completeness(incoming) > _row_completeness(existing) else existing


def dedupe_whale_prints(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    kept: Dict[tuple[Any, ...], Dict[str, Any]] = {}
    order: List[tuple[Any, ...]] = []
    for row in rows:
        key = build_whale_print_key(row)
        if key not in kept:
            kept[key] = row
            order.append(key)
        else:
            kept[key] = _prefer_whale_print(kept[key], row)
    return [kept[key] for key in order]


def is_stale_whale_print(row: Dict[str, Any]) -> bool:
    candidate = _row_candidate(row)
    if bool(row.get("stale_trade_print") or candidate.get("stale_trade_print")):
        return True
    label = str(row.get("fresh_flow_label") or candidate.get("fresh_flow_label") or "").strip().lower()
    if label in {"old trade print", "stale / old premium print"}:
        return True
    age = row.get("trade_print_age_seconds", candidate.get("trade_print_age_seconds"))
    try:
        return float(age) > 120
    except (TypeError, ValueError):
        return False


def _is_duplicate_whale_print(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return build_whale_print_key(left) == build_whale_print_key(right)


def _is_true_follow_up(prior: Dict[str, Any], current: Dict[str, Any]) -> bool:
    if _is_duplicate_whale_print(prior, current):
        return False
    prior_trade = _parse_iso_time(_key_value(prior, "trade_time") or _key_value(prior, "reported_trade_time"))
    current_trade = _parse_iso_time(_key_value(current, "trade_time") or _key_value(current, "reported_trade_time"))
    if prior_trade and current_trade:
        return current_trade > prior_trade
    prior_detected = _parse_iso_time(_key_value(prior, "time_detected") or prior.get("timestamp"))
    current_detected = _parse_iso_time(_key_value(current, "time_detected") or current.get("timestamp"))
    if prior_detected and current_detected and current_detected <= prior_detected:
        return False
    premium_changed = _key_float(_key_value(prior, "estimated_premium")) != _key_float(_key_value(current, "estimated_premium"))
    volume_changed = _key_int(_key_value(prior, "volume")) != _key_int(_key_value(current, "volume"))
    return premium_changed or volume_changed


def attach_simple_follow_through(rows: List[Dict[str, Any]], history: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    history = history or []
    seen: Dict[str, List[Dict[str, Any]]] = {}
    for row in history:
        candidate = _row_candidate(row)
        symbol = str(candidate.get("option_symbol") or "").upper()
        if symbol:
            seen.setdefault(symbol, []).append(row)
    for row in rows:
        candidate = _row_candidate(row)
        symbol = str(candidate.get("option_symbol") or "").upper()
        premium = safe_float(candidate.get("estimated_premium") or row.get("estimated_premium"))
        prior_matches = [prior for prior in seen.get(symbol, []) if _is_true_follow_up(prior, row)]
        if symbol and prior_matches:
            prior = prior_matches[-1]
            prior_candidate = _row_candidate(prior)
            row.update({
                "follow_through_status": "more_premium_added",
                "follow_up_premium": premium,
                "follow_up_count": int(prior.get("follow_up_count") or 0) + 1,
                "last_follow_up_time": candidate.get("time_detected") or row.get("timestamp"),
                "follow_through_reason": "Same option contract showed later trade timing or changed premium/volume in available scan or alert history.",
            })
            prior_candidate["last_follow_up_time"] = candidate.get("time_detected") or row.get("timestamp")
        else:
            row.update({
                "follow_through_status": "no_follow_up_yet" if symbol else "waiting",
                "follow_up_premium": None,
                "follow_up_count": 0,
                "last_follow_up_time": None,
                "follow_through_reason": "No later same-contract flow is available yet.",
            })
        if symbol:
            seen.setdefault(symbol, []).append(row)
    return rows


def result_alert_tier(result: Dict[str, Any], cfg: Dict[str, Any]) -> tuple[str, bool, str]:
    score = int(result.get("whale_score") or 0)
    candidate = result.get("candidate") or {}
    spread = candidate.get("spread_percent")
    warnings = candidate.get("warnings") or []
    confidence_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    required_confidence = str(cfg.get("tier1_min_direction_confidence", "MEDIUM")).upper()
    observed_confidence = str(result.get("direction_confidence") or candidate.get("direction_confidence") or "LOW").upper()
    bias = infer_direction_bias_label(result)
    regime = str(result.get("market_regime") or "UNKNOWN").upper()
    bullish_regimes = {"TRENDING_UP", "OPENING_DRIVE_UP", "BULL_TREND"}
    bearish_regimes = {"TRENDING_DOWN", "OPENING_DRIVE_DOWN", "BEAR_TREND"}
    aligned_regime = regime in (bearish_regimes if bias == "BEARISH" else bullish_regimes if bias == "BULLISH" else set())
    reliability_qualified = bool(result.get("reliability_qualified"))
    reliability_rate = safe_float(result.get("reliability_rate"))
    cohort_reasons: List[str] = []
    if bias == "BEARISH":
        if not aligned_regime: cohort_reasons.append("bearish Tier 1 requires a known bearish-aligned regime")
        if score < int(cfg.get("bearish_tier1_min_score", 90)): cohort_reasons.append("bearish score below Tier 1 threshold")
        if safe_float(result.get("price_confirmation_score")) < int(cfg.get("bearish_tier1_min_price_context", 8)): cohort_reasons.append("bearish price confirmation below Tier 1 threshold")
        if not reliability_qualified or reliability_rate < float(cfg.get("strict_cohort_min_meaningful_rate", 0.30)): cohort_reasons.append("bearish reliability is not proven across multiple sessions")
    if int(candidate.get("dte") or 0) == 0:
        if not aligned_regime: cohort_reasons.append("0DTE Tier 1 requires a known aligned regime")
        if score < int(cfg.get("zero_dte_tier1_min_score", 90)): cohort_reasons.append("0DTE score below Tier 1 threshold")
        if safe_float(candidate.get("estimated_premium")) < float(cfg.get("zero_dte_tier1_min_premium", 250000)): cohort_reasons.append("0DTE premium below Tier 1 threshold")
        if candidate.get("spread_percent") is None or safe_float(candidate.get("spread_percent")) > float(cfg.get("zero_dte_tier1_max_spread_percent", 8)): cohort_reasons.append("0DTE spread exceeds Tier 1 threshold")
        if safe_float(result.get("price_confirmation_score")) < int(cfg.get("zero_dte_tier1_min_price_context", 8)): cohort_reasons.append("0DTE price confirmation below Tier 1 threshold")
        if not reliability_qualified or reliability_rate < float(cfg.get("strict_cohort_min_meaningful_rate", 0.30)): cohort_reasons.append("0DTE reliability is not proven across multiple sessions")
    if bool(candidate.get("stale_trade_print")) or str(candidate.get("fresh_flow_label") or "").lower() != "fresh premium print":
        return "Tier 2", False, "Delayed or stale flow is dashboard-only until fresh evidence arrives."
    if candidate.get("possible_multileg"):
        return "Tier 2", False, "Possible multi-leg flow is dashboard-only until direction is clearer."
    if confidence_rank.get(observed_confidence, 0) < confidence_rank.get(required_confidence, 1):
        return "Tier 2", False, "Direction confidence is below the Tier 1 requirement."
    if score >= int(cfg.get("tier1_min_score", 95)) and result.get("aggression_side") == "near_ask" and safe_float(candidate.get("estimated_premium")) >= float(cfg.get("min_premium", 100000)) and (spread is None or safe_float(spread) <= cfg.get("max_spread_percent", 15)) and result.get("price_context_score", 0) >= int(cfg.get("tier1_min_price_context", 8)):
        if cohort_reasons:
            result.update({"cohort_tier1_gate_passed": False, "cohort_tier1_gate_reasons": cohort_reasons})
            return "Tier 2", False, "; ".join(cohort_reasons)
        result.update({"cohort_tier1_gate_passed": True, "cohort_tier1_gate_reasons": []})
        return "Tier 1", True, "Extreme score, aggressive flow, acceptable spread, and price context."
    if score >= 80 and not any("wide spread" in str(w).lower() or "stale" in str(w).lower() for w in warnings):
        return "Tier 2", bool(cfg.get("notify_tier_2", False)), "High score with minor or no quality warnings."
    if score >= 75:
        return "Tier 3", False, "Unusual but unclear; watch only."
    return "Ignore", False, "Below whale-flow threshold."


def _unusualness_bucket(score: Any) -> str:
    value = safe_float(score)
    if value >= 17:
        return "EXTREME"
    if value >= 13:
        return "HIGH"
    if value >= 8:
        return "MODERATE"
    return "LOW_OR_UNCONFIRMED"


def _baseline_public_fields(candidate: Dict[str, Any], baseline: Dict[str, Any]) -> Dict[str, Any]:
    stats = baseline.get("unusualness_baseline") if isinstance(baseline.get("unusualness_baseline"), dict) else {}
    baseline_volume = safe_float(stats.get("average_volume"))
    baseline_premium = safe_float(stats.get("average_premium"))
    baseline_vol_oi_ratio = None
    volume = safe_float(candidate.get("volume"))
    premium = safe_float(candidate.get("estimated_premium"))
    volume_multiple = volume / baseline_volume if baseline_volume > 0 else None
    premium_multiple = premium / baseline_premium if baseline_premium > 0 else None
    multiples = [item for item in (volume_multiple, premium_multiple) if item is not None]
    warnings = list(baseline.get("unusualness_warnings") or [])
    return {
        "baseline_volume": round(baseline_volume, 2) if baseline_volume else None,
        "baseline_premium": round(baseline_premium, 2) if baseline_premium else None,
        "baseline_vol_oi_ratio": baseline_vol_oi_ratio,
        "unusualness_multiple": round(max(multiples), 2) if multiples else None,
        "unusualness_bucket": _unusualness_bucket(baseline.get("unusualness_score")),
        "baseline_sample_size": int(baseline.get("unusualness_sample_size") or 0),
        "low_sample_warning": "limited historical baseline" in " ".join(str(w).lower() for w in warnings),
    }


def apply_index_0dte_noise_filter(result: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    candidate = result.get("candidate") if isinstance(result.get("candidate"), dict) else result
    symbol = str(candidate.get("underlying_symbol") or "").upper()
    dte = int(candidate.get("dte") or 0)
    score = int(result.get("whale_score") or 0)
    adjusted_score = score
    reasons: List[str] = []
    if symbol in INDEX_0DTE_SYMBOLS and dte == 0:
        min_score = int(cfg.get("index_0dte_min_score", 85))
        min_premium = float(cfg.get("index_0dte_min_premium", 250000))
        max_spread = float(cfg.get("index_0dte_max_spread_percent", 8))
        min_price_score = int(cfg.get("index_0dte_min_price_confirmation_score", 6))
        if score < min_score:
            reasons.append(f"score below stronger 0DTE index threshold ({min_score})")
        if safe_float(candidate.get("estimated_premium")) < min_premium:
            reasons.append(f"premium below stronger 0DTE index threshold (${min_premium:,.0f})")
        if candidate.get("spread_percent") is None or safe_float(candidate.get("spread_percent")) > max_spread:
            reasons.append(f"spread wider than stronger 0DTE index threshold ({max_spread:g}%)")
        if safe_float(result.get("price_confirmation_score") or candidate.get("price_confirmation_score")) < min_price_score:
            reasons.append("price confirmation below stronger 0DTE index threshold")
        if reasons:
            adjusted_score = max(0, score - 15)
    return {
        "index_0dte_noise_flag": bool(reasons),
        "noise_filter_reason": "; ".join(reasons),
        "noise_adjusted_score": adjusted_score,
    }


def format_whale_alert(result: Dict[str, Any]) -> str:
    candidate = result.get("candidate") or {}
    lines = [
        f"{candidate.get('underlying_symbol', 'UNKNOWN')} {result.get('classification', 'POSSIBLE WHALE FLOW')}",
        f"{result.get('direction_label', 'Mixed / unclear flow')} | Score {result.get('whale_score', 0)} | {result.get('alert_tier', 'Tier 3')}",
        f"Contract: {candidate.get('option_symbol')} {candidate.get('option_type')} {candidate.get('strike')} exp {candidate.get('expiration')}",
        f"Premium: ${safe_float(candidate.get('estimated_premium')):,.0f} | Vol/OI: {candidate.get('volume_oi_ratio')}",
        f"Reason: {result.get('reason_summary', 'Unusual options activity detected.')}",
        f"Price context: {result.get('price_confirmation_label', 'Needs price confirmation')}",
        DISCLAIMER,
        "Watch only. Needs price confirmation.",
    ]
    message = "\n".join(str(line) for line in lines if line)
    if FORBIDDEN_ALERT_RE.search(message.replace(DISCLAIMER, "")):
        raise ValueError("Forbidden alert wording generated")
    return message


class OptionsWhaleScanner:
    def __init__(self, config: Dict[str, Any], client: OptionsDataClient, storage: OptionsWhaleStorage, *, root: Optional[Path] = None) -> None:
        self.config = config
        self.whale = whale_config(config)
        self.client = client
        self.storage = storage
        self.root = root or Path.cwd()
        self.universe_path = self.root / "data" / "options_universe.json"
        self.latest_path = self.root / "data" / "options_whale_latest.json"
        self.scan_state_path = self.root / "data" / "options_whale_scan_state.json"
        self.baseline = OptionsUnusualnessBaseline(self.root)
        self.last_scan: Dict[str, Any] = {}
        self.latest_results: List[Dict[str, Any]] = []
        self.last_scan_order: Dict[str, Any] = {}
        self._contract_catalog: Dict[str, List[Dict[str, Any]]] = {}
        self._contract_catalog_refreshed_at: Dict[str, datetime] = {}

    def _load_scan_state(self) -> Dict[str, Any]:
        if not self.scan_state_path.exists():
            return {"symbol_cursor": 0, "contract_cursors": {}, "last_scanned_at": {}}
        try:
            value = json.loads(self.scan_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"symbol_cursor": 0, "contract_cursors": {}, "last_scanned_at": {}}
        return value if isinstance(value, dict) else {"symbol_cursor": 0, "contract_cursors": {}, "last_scanned_at": {}}

    def _save_scan_state(self, state: Dict[str, Any]) -> None:
        self.scan_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.scan_state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True, default=str), encoding="utf-8")
        temporary.replace(self.scan_state_path)

    def _always_scan_symbols(self) -> List[str]:
        raw = self.whale.get("always_scan_symbols") or ["SPY", "QQQ", "IWM", "DIA", "AAPL"]
        if isinstance(raw, str):
            raw = raw.split(",")
        return list(dict.fromkeys(str(value).strip().upper() for value in raw if str(value).strip()))

    def _catalog_for_symbol(self, symbol: str, today: date, max_dte: int) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        refreshed = self._contract_catalog_refreshed_at.get(symbol)
        refresh_seconds = max(60, int(self.whale.get("contract_catalog_refresh_seconds", 900)))
        if symbol in self._contract_catalog and refreshed and (now - refreshed).total_seconds() < refresh_seconds:
            return self._contract_catalog[symbol]
        rows = self.client.get_option_contracts(
            expiration_gte=today,
            expiration_lte=today + timedelta(days=max_dte),
            underlying_symbols=[symbol],
            limit=1000,
            max_contracts=max(1, int(self.whale.get("contract_catalog_max_per_symbol", 5000))),
        )
        deduped = {str(_contract_symbol(row)): row for row in rows if _contract_symbol(row)}
        catalog = [deduped[key] for key in sorted(deduped)]
        self._contract_catalog[symbol] = catalog
        self._contract_catalog_refreshed_at[symbol] = now
        return catalog

    def status(self) -> Dict[str, Any]:
        access = self.client.check_access()
        return {
            "scanner_name": "Options Whale Scanner",
            "enabled": bool(self.whale.get("enabled", True)),
            "legacy_momentum_enabled": bool(self.whale.get("legacy_momentum_enabled", False)),
            **access,
            "last_scan_time": self.last_scan.get("timestamp"),
            "universe": universe_status(self.universe_path),
            "latest_result_count": len(self.latest_results),
            "data_plan_warning": access.get("data_plan_warning") or access.get("last_error") or "",
        }

    def rebuild_universe(self) -> Dict[str, Any]:
        return build_optionable_universe(self.client, self.config, cache_path=self.universe_path)

    def universe_status(self) -> Dict[str, Any]:
        return universe_status(self.universe_path)

    def _priority_seed_symbols(self) -> List[str]:
        raw = self.whale.get("priority_seed_symbols") or DEFAULT_PRIORITY_SEEDS
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",")]
        out: List[str] = []
        seen = set()
        for item in raw:
            symbol = str(item or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                out.append(symbol)
        return out

    def _prioritized_underlyings(self, entries: List[Dict[str, Any]]) -> List[str]:
        by_symbol = {
            str(entry.get("underlying_symbol") or "").upper(): entry
            for entry in entries
            if entry.get("underlying_symbol")
        }
        seeds = self._priority_seed_symbols()
        ordered: List[str] = []
        seen = set()
        for symbol in seeds:
            if symbol not in seen:
                ordered.append(symbol)
                seen.add(symbol)
        rest = sorted(
            (entry for symbol, entry in by_symbol.items() if symbol not in seen),
            key=lambda item: (-int(item.get("contract_count") or 0), str(item.get("underlying_symbol") or "")),
        )
        for entry in rest:
            symbol = str(entry.get("underlying_symbol") or "").upper()
            if symbol and symbol not in seen:
                ordered.append(symbol)
                seen.add(symbol)
        return ordered

    def _contracts(self) -> List[Dict[str, Any]]:
        cache = load_universe_cache(self.universe_path)
        today = datetime.now(timezone.utc).date()
        max_dte = int(self.whale.get("max_dte", 7))
        max_contracts = int(self.whale.get("max_contracts_per_scan", 10000))
        entries = [entry for entry in cache.get("entries", []) if entry.get("underlying_symbol")]
        if not entries:
            universe = self.rebuild_universe()
            entries = [entry for entry in universe.get("entries", []) if entry.get("underlying_symbol")]
        underlyings = self._prioritized_underlyings(entries)
        core = [symbol for symbol in self._always_scan_symbols() if symbol in set(underlyings)]
        rotating = [symbol for symbol in underlyings if symbol not in set(core)]
        state = self._load_scan_state()
        cursor = int(state.get("symbol_cursor") or 0) % max(1, len(rotating))
        previous_cycle = _parse_iso_time(state.get("last_updated"))
        measured_cycle = max(1.0, (datetime.now(timezone.utc) - previous_cycle).total_seconds()) if previous_cycle else float(self.whale.get("scan_interval_seconds", 30))
        prior_ewma = safe_float(state.get("cycle_duration_ewma_seconds")) or measured_cycle
        cycle_ewma = prior_ewma * 0.7 + measured_cycle * 0.3
        target_age = max(1, int(self.whale.get("coverage_warning_age_seconds", 300)))
        dynamic_count = math.ceil(len(rotating) * cycle_ewma / target_age * float(self.whale.get("rotation_safety_factor", 1.15))) if rotating else 0
        rotation_count = max(1, int(self.whale.get("rotation_symbols_per_scan", 15)), dynamic_count)
        rotation_page = [rotating[(cursor + offset) % len(rotating)] for offset in range(min(rotation_count, len(rotating)))] if rotating else []
        selected_underlyings = core + rotation_page
        self.last_scan_order = {
            "universe_size": len(entries),
            "underlying_symbols_considered": len(underlyings),
            "underlying_symbols_scanned": 0,
            "first_20_underlyings_scanned": [],
            "last_20_underlyings_scanned": [],
            "contracts_scanned_by_underlying": {},
        }
        if not self.whale.get("full_market", True):
            selected_underlyings = selected_underlyings[:100]
        contracts: List[Dict[str, Any]] = []
        seen_contracts = set()
        scanned_underlyings: List[str] = []
        priority_budget = min(max_contracts, max(0, int(self.whale.get("priority_contract_budget", 3000))))
        per_symbol_cap = max(1, int(self.whale.get("max_contracts_per_underlying", 600)))
        core_quota = min(per_symbol_cap, max(1, priority_budget // max(1, len(core)))) if core else 0
        rotating_budget = max_contracts - min(max_contracts, core_quota * len(core))
        rotation_quota = min(per_symbol_cap, max(1, rotating_budget // max(1, len(rotation_page)))) if rotation_page else 0
        contract_cursors = state.setdefault("contract_cursors", {})
        last_scanned_at = state.setdefault("last_scanned_at", {})
        catalog_counts: Dict[str, int] = {}
        selected_counts: Dict[str, int] = {}
        failures: Dict[str, str] = {}
        now_iso = utc_now_iso()
        for underlying in selected_underlyings:
            if len(contracts) >= max_contracts:
                break
            quota = core_quota if underlying in core else rotation_quota
            try:
                catalog = self._catalog_for_symbol(underlying, today, max_dte)
            except Exception as exc:
                failures[underlying] = str(exc)[:180]
                catalog = self._contract_catalog.get(underlying, [])
            catalog_counts[underlying] = len(catalog)
            start_index = int(contract_cursors.get(underlying) or 0) % max(1, len(catalog))
            rows = [catalog[(start_index + offset) % len(catalog)] for offset in range(min(quota, len(catalog)))] if catalog else []
            contract_cursors[underlying] = (start_index + len(rows)) % max(1, len(catalog))
            scanned_underlyings.append(underlying)
            last_scanned_at[underlying] = now_iso
            selected_counts[underlying] = len(rows)
            for row in rows:
                symbol = _contract_symbol(row)
                if not symbol or symbol in seen_contracts:
                    continue
                seen_contracts.add(symbol)
                contracts.append(row)
                underlying = _contract_underlying(row)
                counts = self.last_scan_order["contracts_scanned_by_underlying"]
                counts[underlying] = counts.get(underlying, 0) + 1
                if len(contracts) >= max_contracts:
                    break
        rotating_scanned_count = sum(1 for symbol in scanned_underlyings if symbol not in set(core))
        state["symbol_cursor"] = (cursor + rotating_scanned_count) % max(1, len(rotating))
        state["cycle_duration_ewma_seconds"] = round(cycle_ewma, 2)
        state["last_updated"] = now_iso
        self._save_scan_state(state)
        now_dt = datetime.now(timezone.utc)
        symbol_ages: Dict[str, Optional[float]] = {}
        for symbol in underlyings:
            stamp = _parse_iso_time(last_scanned_at.get(symbol))
            symbol_ages[symbol] = round((now_dt - stamp).total_seconds(), 1) if stamp else None
        warning_age = target_age
        stale_symbols = [symbol for symbol, age in symbol_ages.items() if age is None or age > warning_age]
        warmup_symbols = [symbol for symbol, age in symbol_ages.items() if age is None]
        self.last_scan_order.update({
            "underlying_symbols_scanned": len(scanned_underlyings),
            "first_20_underlyings_scanned": scanned_underlyings[:20],
            "last_20_underlyings_scanned": scanned_underlyings[-20:],
            "coverage_cycle_cursor": state["symbol_cursor"],
            "coverage_rotation_page": rotation_page,
            "coverage_rotation_symbols_requested": rotation_count,
            "coverage_rotation_symbols_dynamic": dynamic_count,
            "coverage_cycle_duration_ewma_seconds": round(cycle_ewma, 2),
            "coverage_core_symbols": core,
            "coverage_symbol_ages_seconds": symbol_ages,
            "coverage_stale_symbols": stale_symbols,
            "coverage_warning": f"{len(stale_symbols)} symbols exceed the {warning_age}s coverage target." if stale_symbols else "",
            "coverage_warning_type": "startup_warmup" if warmup_symbols else "sustained_miss" if stale_symbols else "none",
            "coverage_catalog_counts": catalog_counts,
            "coverage_selected_counts": selected_counts,
            "coverage_fetch_failures": failures,
        })
        return contracts

    def _underlying_prices(self, symbols: List[str]) -> Dict[str, Optional[float]]:
        end = datetime.now(timezone.utc)
        bars = self.client.get_stock_bars(symbols, start=end - timedelta(minutes=20), end=end)
        prices: Dict[str, Optional[float]] = {}
        for symbol, rows in bars.items():
            prices[symbol] = safe_float((rows[-1] if rows else {}).get("c") or (rows[-1] if rows else {}).get("close")) if rows else None
        return prices

    def _latest_market_regime(self) -> str:
        latest_path = self.root / "data" / "market_regime_latest.json"
        try:
            row = json.loads(latest_path.read_text(encoding="utf-8"))
            stamp = _parse_iso_time(row.get("timestamp"))
            ages = row.get("source_bar_ages_seconds") or {}
            bars_fresh = all(ages.get(symbol) is not None and float(ages.get(symbol)) <= 90 for symbol in ("SPY", "QQQ"))
            if stamp and (datetime.now(timezone.utc) - stamp).total_seconds() <= 90 and row.get("context_complete") and bars_fresh:
                return str(row.get("market_regime") or "UNKNOWN").upper()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
        path = self.root / "logs" / "market_regime_heartbeat.jsonl"
        if not path.exists():
            return "UNKNOWN"
        try:
            for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]):
                row = json.loads(line)
                stamp = _parse_iso_time(row.get("timestamp"))
                if stamp and (datetime.now(timezone.utc) - stamp).total_seconds() <= 90 and row.get("context_complete"):
                    return str(row.get("market_regime") or row.get("regime") or "UNKNOWN").upper()
        except (OSError, json.JSONDecodeError):
            pass
        return "UNKNOWN"

    def _candidate_from_contract(self, contract: Dict[str, Any], snapshot: Dict[str, Any], prices: Dict[str, Optional[float]], now: datetime) -> OptionFlowCandidate:
        symbol = _contract_symbol(contract)
        underlying = _contract_underlying(contract)
        option_type = _contract_type(contract)
        expiration = _contract_expiration(contract)
        strike = _contract_strike(contract)
        quote = _snapshot_quote(snapshot)
        trade = _snapshot_trade(snapshot)
        bar = _snapshot_bar(snapshot)
        greeks = snapshot.get("greeks") or {}
        bid = safe_float(quote.get("bp") or quote.get("bid_price") or quote.get("bid"))
        ask = safe_float(quote.get("ap") or quote.get("ask_price") or quote.get("ask"))
        last = safe_float(trade.get("p") or trade.get("price") or snapshot.get("latestPrice") or bar.get("c") or contract.get("close_price"))
        mid = midpoint(bid, ask)
        spread = round(ask - bid, 4) if bid and ask else None
        spread_pct = spread_percent(bid, ask)
        volume = int(safe_float(snapshot.get("volume") or snapshot.get("day_volume") or snapshot.get("dailyVolume") or bar.get("v")))
        oi = snapshot.get("open_interest") or snapshot.get("openInterest") or contract.get("open_interest")
        voi = volume_oi_ratio(volume, oi)
        quote_time = _timestamp(quote)
        trade_time = _timestamp(trade)
        underlying_price = prices.get(underlying)
        money, distance, distance_pct = _moneyness(option_type, strike, underlying_price)
        exp_date = date.fromisoformat(expiration)
        dte = max(0, (exp_date - now.date()).days)
        premium = estimated_premium(volume, last, bid, ask)
        warnings: List[str] = []
        if not bid or not ask:
            warnings.append("zero bid/ask or missing quote")
        if spread_pct is not None and spread_pct > float(self.whale.get("max_spread_percent", 15)):
            warnings.append("wide spread")
        age = _quote_age_seconds(quote_time, now)
        if age is None:
            warnings.append("missing quote timestamp")
        elif age > 120:
            warnings.append("stale quote")
        if dte == 0:
            warnings.append("0DTE high-risk contract")
        if money == "OTM" and distance_pct is not None and abs(distance_pct) > 8 and premium < float(self.whale.get("min_premium", 100000)) * 3:
            warnings.append("far OTM warning")
        return OptionFlowCandidate(
            time_detected=utc_now_iso(),
            underlying_symbol=underlying,
            underlying_price=underlying_price,
            option_symbol=symbol,
            contract_id=contract.get("id"),
            option_type=option_type,
            expiration=expiration,
            dte=dte,
            strike=strike,
            moneyness=money,
            distance_from_underlying_price=distance,
            distance_percent=distance_pct,
            bid=bid or None,
            ask=ask or None,
            last=last or None,
            midpoint=mid,
            spread=spread,
            spread_percent=spread_pct,
            volume=volume,
            open_interest=int(safe_float(oi)) if oi is not None else None,
            volume_oi_ratio=voi,
            implied_volatility=safe_float(snapshot.get("impliedVolatility") or snapshot.get("implied_volatility") or snapshot.get("iv")) or None,
            delta=safe_float(greeks.get("delta")) if greeks else None,
            gamma=safe_float(greeks.get("gamma")) if greeks else None,
            theta=safe_float(greeks.get("theta")) if greeks else None,
            vega=safe_float(greeks.get("vega")) if greeks else None,
            trade_count=int(safe_float(snapshot.get("trade_count") or snapshot.get("tradeCount"))) if snapshot.get("trade_count") or snapshot.get("tradeCount") else None,
            quote_time=quote_time,
            trade_time=trade_time,
            quote_freshness_seconds=age,
            estimated_premium=premium,
            data_source=snapshot.get("data_source") or "alpaca",
            warnings=warnings,
        )

    def _effective_whale_config(self) -> Dict[str, Any]:
        cfg = dict(self.whale)
        cfg.setdefault("stale_trade_penalty", 15)
        cfg.setdefault("closing_flow_penalty", 8)
        cfg.setdefault("low_direction_confidence_penalty", 10)
        cfg.setdefault("notification_dedupe_minutes", 15)
        cfg.setdefault("max_notifications_per_symbol", 2)
        cfg.setdefault("tier1_min_score", 90)
        cfg.setdefault("tier1_min_price_context", 8)
        cfg.setdefault("tier1_min_direction_confidence", "MEDIUM")
        if cfg.get("debug_loose_mode", False):
            cfg.update({
                "min_score": min(int(cfg.get("min_score", 75)), 40),
                "min_premium": min(float(cfg.get("min_premium", 100000)), 1000.0),
                "min_volume": min(int(cfg.get("min_volume", 500)), 1),
                "min_volume_oi_ratio": 0.0,
                "max_spread_percent": max(float(cfg.get("max_spread_percent", 15)), 100.0),
                "enable_notifications": False,
            })
        return cfg

    def _filter_rejections(self, candidate: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> List[str]:
        cfg = cfg or self.whale
        reasons: List[str] = []
        if candidate.get("dte", 999) > int(cfg.get("max_dte", 7)):
            reasons.append("dte_above_max")
        if candidate.get("dte") == 0 and not cfg.get("include_0dte", True):
            reasons.append("0dte_disabled")
        if safe_float(candidate.get("estimated_premium")) < float(cfg.get("min_premium", 100000)):
            reasons.append("premium_below_threshold")
        if int(candidate.get("volume") or 0) < int(cfg.get("min_volume", 500)):
            reasons.append("volume_below_threshold")
        if candidate.get("volume_oi_ratio") is not None and safe_float(candidate.get("volume_oi_ratio")) < float(cfg.get("min_volume_oi_ratio", 2.0)):
            reasons.append("volume_oi_below_threshold")
        if candidate.get("spread_percent") is not None and safe_float(candidate.get("spread_percent")) > float(cfg.get("max_spread_percent", 15)):
            reasons.append("spread_above_threshold")
        warnings = [str(w).lower() for w in candidate.get("warnings", [])]
        if any("zero bid/ask" in warning for warning in warnings):
            reasons.append("zero_bid_ask")
        if any("stale quote" in warning for warning in warnings):
            reasons.append("stale_quote")
        return reasons

    def _passes_filters(self, candidate: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> bool:
        return not self._filter_rejections(candidate, cfg)

    def _snapshot_field_diagnostic(self, contract: Dict[str, Any], snapshot: Dict[str, Any], candidate: Dict[str, Any], score: int) -> Dict[str, Any]:
        return {
            "underlying": candidate.get("underlying_symbol"),
            "option_symbol": candidate.get("option_symbol"),
            "raw_snapshot_keys": sorted(snapshot.keys()),
            "parsed_bid": candidate.get("bid"),
            "parsed_ask": candidate.get("ask"),
            "parsed_last": candidate.get("last"),
            "parsed_volume": candidate.get("volume"),
            "parsed_open_interest": candidate.get("open_interest"),
            "parsed_trade_time": candidate.get("trade_time"),
            "parsed_quote_time": candidate.get("quote_time"),
            "calculated_premium": candidate.get("estimated_premium"),
            "calculated_spread_percent": candidate.get("spread_percent"),
            "calculated_score": score,
        }

    def scan(self) -> Dict[str, Any]:
        start = datetime.now(timezone.utc)
        scan_session_state = options_market_session_state(start)
        if not self.whale.get("enabled", True):
            return {"enabled": False, "results": [], "message": "Options Whale Scanner disabled."}
        effective_cfg = self._effective_whale_config()
        debug_loose = bool(self.whale.get("debug_loose_mode", False))
        contracts = self._contracts()
        option_symbols = [_contract_symbol(c) for c in contracts if _contract_symbol(c)]
        max_contracts = int(self.whale.get("max_contracts_per_scan", 10000))
        active_cutoff = start - timedelta(minutes=max(5, int(effective_cfg.get("active_episode_quote_minutes", 75))))
        active_rows = []
        for episode in self.storage.latest_episodes(limit=5000):
            stamp = _parse_iso_time(episode.get("episode_updated_at") or episode.get("scanner_detected_time"))
            if stamp and stamp >= active_cutoff:
                active_rows.append(episode)
        active_rows.sort(key=lambda row: (str(row.get("alert_tier")) == "Tier 1", _parse_iso_time(row.get("episode_updated_at")) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        active_symbols = []
        for row in active_rows:
            symbol = str(_key_value(row, "option_symbol") or "")
            if symbol and symbol not in active_symbols:
                active_symbols.append(symbol)
        active_symbols = active_symbols[: max(1, int(effective_cfg.get("active_episode_quote_limit", 500)))]
        snapshot_symbols = list(dict.fromkeys(option_symbols[:max_contracts] + active_symbols))
        snapshots = self.client.get_option_snapshots(snapshot_symbols)
        for symbol in active_symbols:
            snapshot = snapshots.get(symbol) or {}
            quote, trade = _snapshot_quote(snapshot), _snapshot_trade(snapshot)
            bid = safe_float(quote.get("bp") or quote.get("bid_price") or quote.get("bid")) or None
            ask = safe_float(quote.get("ap") or quote.get("ask_price") or quote.get("ask")) or None
            if bid or ask:
                self.storage.append_quote_observation({"timestamp": utc_now_iso(), "option_symbol": symbol, "bid": bid, "ask": ask, "last": safe_float(trade.get("p") or trade.get("price")) or None, "quote_time": quote.get("t") or quote.get("timestamp"), "data_source": snapshot.get("data_source") or "alpaca", "observation_type": "active_episode_nbbo"})
        underlyings = sorted({_contract_underlying(c) for c in contracts if _contract_underlying(c)})
        prices = self._underlying_prices(underlyings[:500])
        end = datetime.now(timezone.utc)
        stock_bars = self.client.get_stock_bars(underlyings[:50], start=end - timedelta(minutes=90), end=end) if self.whale.get("enable_price_action_context", True) else {}
        baseline_records = self.baseline.load_records()
        recent_outcomes = self.storage.latest_episode_outcomes(limit=5000) or self.storage.latest_outcomes(limit=5000)
        market_regime = self._latest_market_regime()
        reliability_table = build_reliability_table(recent_outcomes, effective_cfg) if bool(effective_cfg.get("reliability_calibration_enabled", True)) else {}
        symbol_bias_memory = build_symbol_bias_memory(
            recent_outcomes,
            window_minutes=int(effective_cfg.get("symbol_bias_memory_window_minutes", 15)),
            min_completed=int(effective_cfg.get("symbol_bias_memory_min_completed", 20)),
            weak_rate=float(effective_cfg.get("symbol_bias_memory_weak_rate", 0.45)),
            strong_rate=float(effective_cfg.get("symbol_bias_memory_strong_rate", 0.55)),
            half_life_sessions=float(effective_cfg.get("symbol_bias_memory_half_life_sessions", 2)),
        ) if bool(effective_cfg.get("symbol_bias_memory_enabled", True)) else {}
        raw_candidates: List[Dict[str, Any]] = []
        evaluated: List[Dict[str, Any]] = []
        skipped_reasons: Dict[str, int] = {}
        rejection_summary: Dict[str, int] = {}
        snapshot_field_diagnostics: List[Dict[str, Any]] = []
        for contract in contracts:
            symbol = _contract_symbol(contract)
            if symbol not in snapshots:
                skipped_reasons["missing_snapshot"] = skipped_reasons.get("missing_snapshot", 0) + 1
                continue
            candidate = self._candidate_from_contract(contract, snapshots[symbol], prices, end).to_dict()
            candidate.update(build_premium_timing_fields(candidate))
            if candidate.get("stale_trade_print"):
                candidate["warnings"] = list(candidate.get("warnings") or []) + ["old premium print — do not treat as fresh flow"]
            candidate.update(build_premium_display_fields(candidate))
            context = classify_price_context(candidate["underlying_symbol"], candidate["option_type"], candidate.get("underlying_price"), stock_bars.get(candidate["underlying_symbol"], [])) if self.whale.get("enable_price_action_context", True) else {}
            candidate.update(classify_aggression(candidate))
            candidate.update(build_premium_pressure_fields(candidate))
            candidate.update(estimate_opening_flow(candidate))
            candidate.update(context)
            unusualness = self.baseline.evaluate_candidate(candidate, baseline_records)
            baseline_fields = _baseline_public_fields(candidate, unusualness)
            candidate.update(unusualness)
            candidate.update(baseline_fields)
            if unusualness.get("unusualness_warnings"):
                candidate["warnings"] = list(candidate.get("warnings") or []) + list(unusualness.get("unusualness_warnings") or [])
            approximate = approximate_sweep_from_snapshot(candidate)
            block = detect_block_print(candidate, [], {"min_premium": effective_cfg.get("min_premium", 100000)}) if self.whale.get("enable_block_detection", True) else {}
            scored = score_options_whale_flow({**candidate, **approximate, **block}, context, effective_cfg)
            scored.update(apply_index_0dte_noise_filter({**scored, "candidate": candidate, **context}, effective_cfg))
            scored["whale_score"] = scored["noise_adjusted_score"]
            scored["classification"] = classify_score(scored["whale_score"])
            reasons = self._filter_rejections(candidate, effective_cfg)
            if scored["whale_score"] < int(effective_cfg.get("min_score", 75)):
                reasons.append("score_below_threshold")
            for reason in reasons:
                rejection_summary[reason] = rejection_summary.get(reason, 0) + 1
            record = {
                "candidate": candidate,
                **scored,
                "filter_rejection_reasons": reasons,
                "reason_rejected": ", ".join(reasons) if reasons else "",
            }
            evaluated.append(record)
            if candidate["underlying_symbol"] in {"SPY", "QQQ", "NVDA", "AAPL"} and len(snapshot_field_diagnostics) < 10:
                snapshot_field_diagnostics.append(self._snapshot_field_diagnostic(contract, snapshots[symbol], candidate, scored["whale_score"]))
            if not reasons:
                raw_candidates.append(candidate)
        trade_map: Dict[str, List[Dict[str, Any]]] = {}
        if self.whale.get("enable_sweep_detection", True) and raw_candidates:
            try:
                trade_map = self.client.get_option_trades([c["option_symbol"] for c in raw_candidates[:300]], start=end - timedelta(minutes=3), end=end)
            except Exception:
                trade_map = {}
        multileg_map = detect_possible_multileg(raw_candidates) if self.whale.get("enable_multileg_detection", True) else {}
        results: List[Dict[str, Any]] = []
        for candidate in raw_candidates:
            trades = trade_map.get(candidate["option_symbol"], [])
            aggression = classify_aggression(candidate)
            candidate.update(aggression)
            candidate.update(build_premium_pressure_fields(candidate))
            sweep = detect_sweep_activity(trades) if trades else approximate_sweep_from_snapshot(candidate)
            block = detect_block_print(candidate, trades, {"min_premium": self.whale.get("min_premium", 100000)}) if self.whale.get("enable_block_detection", True) else {}
            multileg = multileg_map.get(candidate["option_symbol"], default_multileg_result())
            flow = apply_multileg_direction_adjustment(aggression, multileg)
            opening = estimate_opening_flow(candidate)
            context = classify_price_context(candidate["underlying_symbol"], candidate["option_type"], candidate.get("underlying_price"), stock_bars.get(candidate["underlying_symbol"], [])) if self.whale.get("enable_price_action_context", True) else {}
            candidate.update(sweep)
            candidate.update(block)
            candidate.update(opening)
            candidate.update(context)
            scored = score_options_whale_flow({**candidate, **sweep, **block, **flow}, context, effective_cfg)
            scored.update(apply_index_0dte_noise_filter({**scored, "candidate": candidate, **flow, **context}, effective_cfg))
            scored["whale_score"] = scored["noise_adjusted_score"]
            scored["classification"] = classify_score(scored["whale_score"])
            if scored["whale_score"] < int(effective_cfg.get("min_score", 75)):
                continue
            result = {
                "candidate": candidate,
                **scored,
                **flow,
                **sweep,
                **block,
                **multileg,
                **opening,
                **context,
                **build_premium_timing_fields(candidate),
                **build_premium_display_fields(candidate),
                **build_premium_pressure_fields({**candidate, **flow}),
                "next_day_oi_status": "pending",
                "next_day_oi_reason": "awaiting next trading day OI",
                "market_regime": market_regime,
            }
            if symbol_bias_memory:
                result = apply_symbol_bias_memory(result, symbol_bias_memory, effective_cfg)
            else:
                result.update({
                    "learned_quality_score": None,
                    "learned_quality_reason": "not enough outcome history yet",
                    "symbol_bias_memory_label": "disabled_or_no_history",
                })
            if reliability_table:
                result = apply_reliability_adjustment(result, reliability_table, effective_cfg)
            else:
                result.update({"reliability_status": "no_history", "reliability_score_adjustment": 0})
            if int(result.get("whale_score") or 0) < int(effective_cfg.get("min_score", 75)):
                continue
            result.update({
                "option_price_follow_through_status": result.get("follow_through_status", "pending_same_contract_follow_up"),
                "option_price_follow_through_note": "Uses same-contract premium/volume follow-up from later scans when available; not full option P&L yet.",
            })
            tier, should_notify, notify_reason = result_alert_tier(result, effective_cfg)
            if debug_loose:
                should_notify = False
                notify_reason = "DEBUG LOOSE MODE — not alert quality; notifications disabled."
            result.update({"alert_tier": tier, "should_notify": should_notify, "notify_reason": notify_reason, "disclaimer": DISCLAIMER})
            if debug_loose:
                result["debug_loose_mode"] = True
                result["debug_label"] = "DEBUG LOOSE MODE — not alert quality"
            result["message_preview"] = format_whale_alert(result)
            results.append(result)
            candidate_for_quote = result.get("candidate") or {}
            if candidate_for_quote.get("bid") or candidate_for_quote.get("ask"):
                self.storage.append_quote_observation({"timestamp": candidate_for_quote.get("time_detected") or utc_now_iso(), "option_symbol": candidate_for_quote.get("option_symbol"), "bid": candidate_for_quote.get("bid"), "ask": candidate_for_quote.get("ask"), "last": candidate_for_quote.get("last"), "quote_time": candidate_for_quote.get("quote_time"), "data_source": candidate_for_quote.get("data_source"), "observation_type": "episode_entry_nbbo"})
        results.sort(key=lambda item: int(item.get("whale_score") or 0), reverse=True)
        results = results[: int(effective_cfg.get("max_results", 100))]
        results = attach_simple_follow_through(results, self.storage.latest_alerts(limit=500))
        for result in results:
            result.update({
                "option_price_follow_through_status": result.get("follow_through_status", "no_follow_up_yet"),
                "option_price_follow_through_note": "Uses same-contract premium/volume follow-up from later scans when available; not full option P&L yet.",
            })
        final_by_key = {build_whale_print_key(item): item for item in results}
        near_misses = sorted(
            evaluated,
            key=lambda item: (int(item.get("whale_score") or 0), safe_float((item.get("candidate") or {}).get("estimated_premium"))),
            reverse=True,
        )[:20]
        near_misses_out = []
        for item in near_misses:
            candidate = item.get("candidate") or {}
            final = final_by_key.get(build_whale_print_key(item))
            near_misses_out.append({
                "option_symbol": candidate.get("option_symbol"),
                "underlying": candidate.get("underlying_symbol"),
                "trade_time": candidate.get("trade_time"),
                "time_detected": candidate.get("time_detected"),
                "fresh_flow_label": candidate.get("fresh_flow_label"),
                "stale_trade_print": candidate.get("stale_trade_print"),
                "trade_print_age_seconds": candidate.get("trade_print_age_seconds"),
                "trade_print_age_minutes": candidate.get("trade_print_age_minutes"),
                "trade_print_age_warning": candidate.get("trade_print_age_warning"),
                "volume": candidate.get("volume"),
                "open_interest": candidate.get("open_interest"),
                "premium": candidate.get("estimated_premium"),
                "spread_percent": candidate.get("spread_percent"),
                "score": (final or item).get("whale_score"),
                "reason_rejected": item.get("reason_rejected"),
                "thresholds_failed": item.get("filter_rejection_reasons", []),
            })
        raw_results_count = len(results)
        results = dedupe_whale_prints(results)
        results = attach_flow_episode_context(results, int(effective_cfg.get("flow_episode_bucket_minutes", 5)))
        results = attach_outcome_completeness(results, recent_outcomes)
        duplicate_results_count = raw_results_count - len(results)
        fresh_results_count = sum(1 for item in results if not is_stale_whale_print(item))
        stale_results_count = len(results) - fresh_results_count
        stale_quote_rejection_count = int(rejection_summary.get("stale_quote") or 0)
        contracts_evaluated = len(evaluated)
        passed_filter_count = len(raw_candidates)
        scan_record = {
            "timestamp": utc_now_iso(),
            "duration_seconds": round((datetime.now(timezone.utc) - start).total_seconds(), 2),
            "contracts_scanned": len(option_symbols),
            "contracts_evaluated": contracts_evaluated,
            "passed_filter_count": passed_filter_count,
            "candidates_found": passed_filter_count,
            "results_count": len(results),
            "fresh_count": fresh_results_count,
            "stale_count": stale_results_count,
            "stale_quote_rejection_count": stale_quote_rejection_count,
            "raw_results_count": raw_results_count,
            "deduped_results_count": len(results),
            "duplicate_results_count": duplicate_results_count,
            "partial_scan": bool(
                len(option_symbols) >= max_contracts
                or int(self.last_scan_order.get("underlying_symbols_scanned") or 0)
                < int(self.last_scan_order.get("underlying_symbols_considered") or 0)
            ),
            "partial_scan_warning": (
                "Rotating contract coverage is active — this pass covers only part of the universe."
                if int(self.last_scan_order.get("underlying_symbols_scanned") or 0)
                < int(self.last_scan_order.get("underlying_symbols_considered") or 0)
                else "Rate limited or contract cap reached — showing partial scan results."
                if len(option_symbols) >= max_contracts else ""
            ),
            "scan_session_state": scan_session_state,
            "scan_session_warning": "Options market is closed; treat this scan as after-hours/stale context." if scan_session_state != "regular" else "",
            "debug_loose_mode": debug_loose,
            "debug_label": "DEBUG LOOSE MODE — not alert quality" if debug_loose else "",
            "debug_loose_mode_warning": "DEBUG LOOSE MODE — results are not alert quality and notifications are disabled." if debug_loose else "",
            **self.last_scan_order,
            "skipped_contracts_count": sum(skipped_reasons.values()),
            "skipped_reasons_summary": skipped_reasons,
            "candidate_filter_rejection_summary": rejection_summary,
            "top_rejection_reasons": sorted(rejection_summary.items(), key=lambda item: item[1], reverse=True)[:10],
            "near_misses": near_misses_out,
            "near_miss_count": len(near_misses_out),
            "snapshot_field_diagnostics": snapshot_field_diagnostics,
            "results": results,
        }
        self.last_scan = scan_record
        self.latest_results = results
        self.storage.append_scan({k: v for k, v in scan_record.items() if k != "results"})
        try:
            self.latest_path.parent.mkdir(parents=True, exist_ok=True)
            self.latest_path.write_text(json.dumps(scan_record, indent=2, sort_keys=True, default=str), encoding="utf-8")
        except OSError:
            pass
        prior_alerts = self.storage.latest_alerts(limit=5000)
        prior_events = {build_notification_event_key(row): row for row in prior_alerts}
        notification_window = timedelta(minutes=int(effective_cfg.get("notification_dedupe_minutes", 15)))
        now = datetime.now(timezone.utc)
        recent_symbol_counts: Dict[str, int] = {}
        for prior_row in prior_alerts:
            candidate = prior_row.get("candidate") or {}
            prior_symbol = str(candidate.get("underlying_symbol") or "").upper()
            prior_time = _parse_iso_time(prior_row.get("scanner_detected_time") or candidate.get("time_detected"))
            if prior_symbol and prior_time and now - prior_time <= notification_window:
                recent_symbol_counts[prior_symbol] = recent_symbol_counts.get(prior_symbol, 0) + 1
        per_symbol: Dict[str, int] = {}
        notified_episodes: set[str] = set()
        for result in results:
            symbol = str((result.get("candidate") or {}).get("underlying_symbol") or "").upper()
            episode_id = str(result.get("flow_episode_id") or "")
            event_key = build_notification_event_key(result)
            prior = prior_events.get(event_key)
            prior_premium = safe_float(_key_value(prior or {}, "estimated_premium"))
            current_premium = safe_float(_key_value(result, "estimated_premium"))
            material_update = prior and prior_premium > 0 and current_premium >= prior_premium * 1.25
            if prior and not material_update:
                result.update({"should_notify": False, "notify_reason": "Duplicate option-flow event already logged; dashboard update only."})
            elif material_update:
                result.update({"update_type": "material_premium_follow_through", "notify_reason": "Material premium follow-through update."})
            elif episode_id and episode_id in notified_episodes:
                result.update({"should_notify": False, "notify_reason": "Related strike grouped into an existing flow episode; dashboard update only."})
            elif episode_id and result.get("flow_episode_size", 1) > 1 and not result.get("flow_episode_leader"):
                result.update({"should_notify": False, "notify_reason": "Related strike is not the lead contract for this flow episode; dashboard update only."})
            elif recent_symbol_counts.get(symbol, 0) + per_symbol.get(symbol, 0) >= int(effective_cfg.get("max_notifications_per_symbol", 2)):
                result.update({"should_notify": False, "notify_reason": "Per-symbol notification budget reached in the recent window; grouped dashboard update only."})
            if result.get("should_notify"):
                if episode_id:
                    notified_episodes.add(episode_id)
                per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
                self.storage.append_alert(result)
            if result.get("alert_tier") in {"Tier 1", "Tier 2"}:
                self.storage.append_qualified_event(result)
        episode_groups: Dict[str, List[Dict[str, Any]]] = {}
        for result in results:
            episode_id = str(result.get("flow_episode_id") or "")
            if episode_id:
                episode_groups.setdefault(episode_id, []).append(result)
        for episode_id, members in episode_groups.items():
            leader = next((item for item in members if item.get("flow_episode_leader")), members[0])
            snapshot = dict(leader)
            snapshot.update({
                "episode_id": episode_id,
                "episode_updated_at": utc_now_iso(),
                "episode_member_contracts": [
                    {
                        "option_symbol": _key_value(item, "option_symbol"),
                        "strike": _key_value(item, "strike"),
                        "expiration": _key_value(item, "expiration"),
                        "score": item.get("whale_score"),
                        "premium": _key_value(item, "estimated_premium"),
                        "volume": _key_value(item, "volume"),
                        "open_interest": _key_value(item, "open_interest"),
                    }
                    for item in members
                ],
                "episode_observation_count": len(members),
            })
            self.storage.append_episode(snapshot)
        try:
            self.baseline.append_observations(evaluated)
        except Exception:
            pass
        return scan_record

    def history(self, limit: int = 100) -> Dict[str, Any]:
        raw_alerts = self.storage.latest_alerts(limit=limit)
        deduped_alerts = dedupe_whale_prints(raw_alerts)
        stale_count = sum(1 for item in deduped_alerts if is_stale_whale_print(item))
        return {
            "alerts": deduped_alerts,
            "metadata": {
                "raw_count": len(raw_alerts),
                "deduped_count": len(deduped_alerts),
                "duplicate_count": len(raw_alerts) - len(deduped_alerts),
                "fresh_count": len(deduped_alerts) - stale_count,
                "stale_count": stale_count,
            },
        }

    def latest(self) -> Dict[str, Any]:
        if not self.last_scan and self.latest_path.exists():
            try:
                payload = json.loads(self.latest_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self.last_scan = payload
                    self.latest_results = list(payload.get("results") or [])
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "results": self.latest_results,
            "near_misses": self.last_scan.get("near_misses", []),
            "diagnostics": {k: v for k, v in self.last_scan.items() if k not in {"results", "near_misses"}},
            "last_scan": {k: v for k, v in self.last_scan.items() if k != "results"},
        }

    def review_next_day_oi(self, oi_by_contract: Dict[str, int]) -> List[Dict[str, Any]]:
        reviews = review_alerts_with_next_day_oi(self.storage.latest_alerts(limit=10000), oi_by_contract)
        for record in reviews:
            self.storage.append_oi_review(record)
        return reviews
