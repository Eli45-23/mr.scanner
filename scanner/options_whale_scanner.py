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
        "index_0dte_min_score": 85,
        "index_0dte_min_premium": 250000,
        "index_0dte_max_spread_percent": 8,
        "index_0dte_min_price_confirmation_score": 6,
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
    if bool(candidate.get("stale_trade_print")) or str(candidate.get("fresh_flow_label") or "").lower() != "fresh premium print":
        return "Tier 2", False, "Delayed or stale flow is dashboard-only until fresh evidence arrives."
    if candidate.get("possible_multileg"):
        return "Tier 2", False, "Possible multi-leg flow is dashboard-only until direction is clearer."
    if score >= 90 and result.get("aggression_side") == "near_ask" and safe_float(candidate.get("estimated_premium")) >= float(cfg.get("min_premium", 100000)) and (spread is None or safe_float(spread) <= cfg.get("max_spread_percent", 15)) and result.get("price_context_score", 0) >= 6:
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
        self.baseline = OptionsUnusualnessBaseline(self.root)
        self.last_scan: Dict[str, Any] = {}
        self.latest_results: List[Dict[str, Any]] = []
        self.last_scan_order: Dict[str, Any] = {}

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
        self.last_scan_order = {
            "universe_size": len(entries),
            "underlying_symbols_considered": len(underlyings),
            "underlying_symbols_scanned": 0,
            "first_20_underlyings_scanned": [],
            "last_20_underlyings_scanned": [],
            "contracts_scanned_by_underlying": {},
        }
        if not self.whale.get("full_market", True):
            underlyings = underlyings[:100]
        contracts: List[Dict[str, Any]] = []
        seen_contracts = set()
        scanned_underlyings: List[str] = []
        batch_size = max(1, int(self.whale.get("priority_batch_size", 50)))
        for idx in range(0, len(underlyings), batch_size):
            if len(contracts) >= max_contracts:
                break
            batch = underlyings[idx: idx + batch_size]
            remaining = max_contracts - len(contracts)
            rows = self.client.get_option_contracts(
                expiration_gte=today,
                expiration_lte=today + timedelta(days=max_dte),
                underlying_symbols=batch,
                limit=min(10000, remaining),
                max_contracts=remaining,
            )
            scanned_underlyings.extend(batch)
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
        self.last_scan_order.update({
            "underlying_symbols_scanned": len(scanned_underlyings),
            "first_20_underlyings_scanned": scanned_underlyings[:20],
            "last_20_underlyings_scanned": scanned_underlyings[-20:],
        })
        return contracts

    def _underlying_prices(self, symbols: List[str]) -> Dict[str, Optional[float]]:
        end = datetime.now(timezone.utc)
        bars = self.client.get_stock_bars(symbols, start=end - timedelta(minutes=20), end=end)
        prices: Dict[str, Optional[float]] = {}
        for symbol, rows in bars.items():
            prices[symbol] = safe_float((rows[-1] if rows else {}).get("c") or (rows[-1] if rows else {}).get("close")) if rows else None
        return prices

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
        cfg.setdefault("notification_dedupe_minutes", 15)
        cfg.setdefault("max_notifications_per_symbol", 2)
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
        snapshots = self.client.get_option_snapshots(option_symbols[:max_contracts])
        underlyings = sorted({_contract_underlying(c) for c in contracts if _contract_underlying(c)})
        prices = self._underlying_prices(underlyings[:500])
        end = datetime.now(timezone.utc)
        stock_bars = self.client.get_stock_bars(underlyings[:50], start=end - timedelta(minutes=90), end=end) if self.whale.get("enable_price_action_context", True) else {}
        baseline_records = self.baseline.load_records()
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
                "learned_quality_score": None,
                "learned_quality_reason": "not enough outcome history yet",
            }
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
        results.sort(key=lambda item: int(item.get("whale_score") or 0), reverse=True)
        results = results[: int(effective_cfg.get("max_results", 100))]
        results = attach_simple_follow_through(results, self.storage.latest_alerts(limit=500))
        near_misses = sorted(
            evaluated,
            key=lambda item: (int(item.get("whale_score") or 0), safe_float((item.get("candidate") or {}).get("estimated_premium"))),
            reverse=True,
        )[:20]
        near_misses_out = []
        for item in near_misses:
            candidate = item.get("candidate") or {}
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
                "score": item.get("whale_score"),
                "reason_rejected": item.get("reason_rejected"),
                "thresholds_failed": item.get("filter_rejection_reasons", []),
            })
        raw_results_count = len(results)
        results = dedupe_whale_prints(results)
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
            "partial_scan": len(option_symbols) >= max_contracts,
            "partial_scan_warning": "Rate limited or contract cap reached — showing partial scan results." if len(option_symbols) >= max_contracts else "",
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
        for result in results:
            symbol = str((result.get("candidate") or {}).get("underlying_symbol") or "").upper()
            event_key = build_notification_event_key(result)
            prior = prior_events.get(event_key)
            prior_premium = safe_float(_key_value(prior or {}, "estimated_premium"))
            current_premium = safe_float(_key_value(result, "estimated_premium"))
            material_update = prior and prior_premium > 0 and current_premium >= prior_premium * 1.25
            if prior and not material_update:
                result.update({"should_notify": False, "notify_reason": "Duplicate option-flow event already logged; dashboard update only."})
            elif material_update:
                result.update({"update_type": "material_premium_follow_through", "notify_reason": "Material premium follow-through update."})
            elif recent_symbol_counts.get(symbol, 0) + per_symbol.get(symbol, 0) >= int(effective_cfg.get("max_notifications_per_symbol", 2)):
                result.update({"should_notify": False, "notify_reason": "Per-symbol notification budget reached in the recent window; grouped dashboard update only."})
            if result.get("should_notify"):
                per_symbol[symbol] = per_symbol.get(symbol, 0) + 1
                self.storage.append_alert(result)
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
