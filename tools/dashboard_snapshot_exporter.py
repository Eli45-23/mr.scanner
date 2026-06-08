from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.error import URLError
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPORT_DIR = PROJECT_ROOT / "exports"
DEFAULT_JSON_PATH = EXPORT_DIR / "dashboard_snapshot_latest.json"
DEFAULT_MD_PATH = EXPORT_DIR / "dashboard_snapshot_latest.md"
WATCHLIST_SYMBOLS: Sequence[str] = ("AAPL", "QQQ", "SPY")
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8765"

SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "credential",
    "client_secret",
    "private_key",
    "refresh",
    "access",
    "account_id",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _truncate(text: Any, limit: int = 180) -> str:
    if text is None:
        return "unavailable"
    value = str(text).replace("\n", " ").strip()
    if not value:
        return "unavailable"
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def _short_join(values: Any, limit: int = 3) -> str:
    if not values:
        return "unavailable"
    if not isinstance(values, (list, tuple)):
        return _truncate(values)
    items = [str(v).strip() for v in values if str(v).strip()]
    if not items:
        return "unavailable"
    joined = "; ".join(items[:limit])
    if len(items) > limit:
        joined += f"; +{len(items) - limit} more"
    return _truncate(joined)


def _fmt_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "unavailable"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _truncate(value)
    fmt = f"{{:.{digits}f}}"
    return fmt.format(number) + suffix


def _fmt_bool(value: Any) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "unavailable"


def _safe_num(data: Dict[str, Any], *keys: str) -> Any:
    value = _pick(data, *keys)
    return value if isinstance(value, (int, float)) else None


def _level_num(row: Dict[str, Any], key: str) -> Any:
    levels = row.get("strategy_levels")
    if isinstance(levels, dict):
        value = levels.get(key)
        if isinstance(value, (int, float)):
            return value
    return _safe_num(row, key)


def redact_payload(value: Any, key_hint: str = "") -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            lower = key.lower()
            if any(marker in lower for marker in SECRET_KEY_MARKERS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_payload(item, key)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item, key_hint) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item, key_hint) for item in value]
    return value


def fetch_json(url: str, timeout: float = 5.0) -> Optional[Any]:
    try:
        with urlopen(url, timeout=timeout) as response:
            return json.load(response)
    except Exception:
        return None


def load_jsonl_tail(path: Path, limit: int = 20) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    items: deque[Dict[str, Any]] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                items.append(redact_payload(parsed))
    return list(items)


def collect_live_sources(
    *,
    base_url: str = DEFAULT_DASHBOARD_URL,
    log_dir: Path = DEFAULT_LOG_DIR,
    timeout: float = 5.0,
    fetcher: Callable[[str, float], Optional[Any]] = fetch_json,
) -> Dict[str, Any]:
    status = fetcher(f"{base_url}/api/status", timeout) or {}
    symbols_payload = fetcher(f"{base_url}/api/symbols", timeout) or {}
    alerts_payload = fetcher(f"{base_url}/api/alerts", timeout) or {}
    if not isinstance(status, dict):
        status = {}
    if not isinstance(symbols_payload, dict):
        symbols_payload = {}
    if not isinstance(alerts_payload, dict):
        alerts_payload = {}
    return {
        "exported_at": _now_iso(),
        "source": base_url,
        "dashboard_status": redact_payload(status),
        "dashboard_symbols_raw": redact_payload(symbols_payload),
        "dashboard_alerts_raw": redact_payload(alerts_payload),
        "recent_alert_logs": load_jsonl_tail(log_dir / "alerts.jsonl", 20),
        "recent_scenario_records": load_jsonl_tail(log_dir / "scenario_engine.jsonl", 20),
        "recent_option_decisions": load_jsonl_tail(log_dir / "option_quality_decisions.jsonl", 20),
        "recent_notification_events": load_jsonl_tail(log_dir / "notification_status.jsonl", 20),
    }


def _extract_symbol_rows(symbols_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = symbols_payload.get("symbols")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    if isinstance(symbols_payload, list):
        return [item for item in symbols_payload if isinstance(item, dict)]
    return []


def _symbol_key_lookup(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            result[symbol] = row
    return result


def _alert_tier(row: Dict[str, Any]) -> Any:
    return _pick(row, "alert_tier", "scenario_alert_tier", "alert_grade", "grade", default=None)


def _symbol_summary_row(row: Dict[str, Any]) -> Dict[str, Any]:
    scenario_top = row.get("scenario_top") if isinstance(row.get("scenario_top"), dict) else {}
    scenario_block_reason = _pick(row, "scenario_sms_block_reason", "sms_block_reason", default=None)
    option_warning = _pick(row, "option_warning", "option_block_reason", "sms_block_reason", default=None)
    return {
        "symbol": row.get("symbol"),
        "timestamp": _pick(row, "timestamp", "bar_time", default=None),
        "price": row.get("price"),
        "direction": _pick(row, "strategy_direction", "scenario_direction", "direction", default=None),
        "top_scenario": _pick(scenario_top, "scenario_name", default=row.get("primary_setup")),
        "stage": _pick(row, "scenario_stage", default=_pick(scenario_top, "stage", default=None)),
        "scenario_score": row.get("scenario_score"),
        "stock_setup_score": row.get("stock_setup_score"),
        "confirmation_score": row.get("confirmation_score"),
        "risk_label": row.get("risk_label"),
        "entry_quality_label": _pick(row, "entry_quality_label", "scenario_entry_quality_label", default=None),
        "stock_setup_score_reason": row.get("stock_setup_score_reason"),
        "vwap": _level_num(row, "vwap"),
        "ema9": _level_num(row, "ema9"),
        "ema20": _level_num(row, "ema20"),
        "option_feed_status": row.get("option_feed_status"),
        "scenario_alert_tier": _alert_tier(row),
        "scenario_alert_block_reason": _pick(row, "scenario_alert_block_reason", default=None),
        "scenario_alert_eligible": row.get("scenario_alert_eligible"),
        "scenario_would_sms": row.get("scenario_would_sms"),
        "scenario_sms_block_reason": scenario_block_reason,
        "sms_allowed_by_stock": row.get("sms_allowed_by_stock"),
        "sms_allowed_by_options": row.get("sms_allowed_by_options"),
        "final_sms_allowed": _pick(row, "sms_allowed", "scenario_sms_allowed", default=None),
        "sms_block_reason": row.get("sms_block_reason"),
        "option_warning": option_warning,
    }


def _symbol_detail(row: Dict[str, Any]) -> Dict[str, Any]:
    scenario_top = row.get("scenario_top") if isinstance(row.get("scenario_top"), dict) else {}
    scenario_second = row.get("scenario_second") if isinstance(row.get("scenario_second"), dict) else {}
    option_warning = _pick(row, "option_warning", "option_block_reason", "sms_block_reason", default=None)
    invalidation_reason = _pick(
        scenario_top,
        "invalidation_reason",
        default=_pick(row, "scenario_sms_block_reason", "sms_block_reason", default=None),
    )
    return {
        "symbol": row.get("symbol"),
        "current_read": {
            "price": row.get("price"),
            "direction": _pick(row, "strategy_direction", "scenario_direction", "direction", default=None),
            "top_scenario": _pick(scenario_top, "scenario_name", default=row.get("primary_setup")),
            "stage": _pick(row, "scenario_stage", default=_pick(scenario_top, "stage", default=None)),
            "scenario_score": row.get("scenario_score"),
            "stock_setup_score": row.get("stock_setup_score"),
            "stock_setup_score_reason": row.get("stock_setup_score_reason"),
            "confirmation_score": row.get("confirmation_score"),
            "risk_label": row.get("risk_label"),
            "entry_quality_label": _pick(row, "entry_quality_label", "scenario_entry_quality_label", default=None),
            "option_feed_status": row.get("option_feed_status"),
            "scenario_alert_tier": _alert_tier(row),
            "scenario_alert_block_reason": _pick(row, "scenario_alert_block_reason", default=None),
        },
        "core": {
            "timestamp": _pick(row, "timestamp", "bar_time", default=None),
            "price": row.get("price"),
            "price_change_pct": _pick(row, "fast_move_pct", "day_move_pct", default=None),
            "volume": _pick(row, "recent_volume", "volume", default=None),
            "rvol": _pick(row, "relative_volume", "rvol_detail", default=None),
            "vwap": _safe_num(row, "vwap"),
            "ema9": _safe_num(row, "ema9"),
            "ema20": _safe_num(row, "ema20"),
            "premarket_high": _pick(row, "premarket_high", default=None),
            "premarket_low": _pick(row, "premarket_low", default=None),
            "previous_day_high": _pick(row, "previous_day_high", "pdh", default=None),
            "previous_day_low": _pick(row, "previous_day_low", "pdl", default=None),
            "opening_range_high": _pick(row, "opening_range_high", "or_5_high", default=None),
            "opening_range_low": _pick(row, "opening_range_low", "or_5_low", default=None),
            "opening_range_15_high": _pick(row, "opening_range_15_high", default=None),
            "opening_range_15_low": _pick(row, "opening_range_15_low", default=None),
            "current_session_high": _pick(row, "session_high", "day_high", "high", default=None),
            "current_session_low": _pick(row, "session_low", "day_low", "low", default=None),
        },
        "phase1": {
            "primary_setup": row.get("primary_setup"),
            "secondary_setups": row.get("secondary_setups") or [],
            "direction": _pick(row, "strategy_direction", "direction", default=None),
            "confidence_score": row.get("strategy_confidence_score"),
            "confidence_label": row.get("strategy_confidence_label"),
            "risk_label": row.get("risk_label"),
            "reasons": row.get("strategy_reasons") or [],
            "warnings": row.get("strategy_warnings") or [],
            "levels": row.get("strategy_levels") or {},
            "stock_setup_score": row.get("stock_setup_score"),
            "stock_setup_score_reason": row.get("stock_setup_score_reason"),
        },
        "phase2": {
            "confirmation_score": row.get("confirmation_score"),
            "confirmation_label": row.get("confirmation_label"),
            "entry_quality_label": row.get("entry_quality_label"),
            "volume_label": row.get("volume_label"),
            "candle_label": row.get("candle_label"),
            "candle_score": row.get("candle_score"),
            "retest_hold_label": _pick((row.get("retest_hold") or {}) if isinstance(row.get("retest_hold"), dict) else {}, "entry_quality_label", default=None),
            "extension_label": row.get("extension_label"),
            "relative_strength_label": row.get("relative_strength_label"),
            "market_regime": row.get("market_regime"),
            "pressure_label": row.get("pressure_label"),
        },
        "phase3": {
            "top_scenario": scenario_top.get("scenario_name"),
            "second_scenario": scenario_second.get("scenario_name"),
            "scenario_stage": row.get("scenario_stage"),
            "scenario_score": row.get("scenario_score"),
            "scenario_conflict": row.get("scenario_conflict"),
            "bullish_score": row.get("bullish_score"),
            "bearish_score": row.get("bearish_score"),
            "chop_score": row.get("chop_score"),
            "fakeout_score": row.get("fakeout_score"),
            "scenario_reasons": row.get("scenario_reasons") or [],
            "scenario_warnings": row.get("scenario_warnings") or [],
            "invalidation_level": _pick(scenario_top, "invalidation_level", default=None),
            "invalidation_reason": invalidation_reason,
            "scenario_alert_eligible": row.get("scenario_alert_eligible"),
            "scenario_would_sms": row.get("scenario_would_sms"),
            "scenario_alert_tier": _alert_tier(row),
            "scenario_alert_block_reason": _pick(row, "scenario_alert_block_reason", default=None),
            "scenario_sms_block_reason": row.get("scenario_sms_block_reason"),
        },
        "options": {
            "stock_setup_score": row.get("stock_setup_score"),
            "option_tradability_score": row.get("option_tradability_score"),
            "option_feed_status": row.get("option_feed_status"),
            "option_tradable": row.get("option_tradable"),
            "option_warning": option_warning,
            "sms_allowed_by_stock": row.get("sms_allowed_by_stock"),
            "sms_allowed_by_options": row.get("sms_allowed_by_options"),
            "final_sms_allowed": _pick(row, "sms_allowed", "scenario_sms_allowed", default=None),
            "sms_block_reason": row.get("sms_block_reason"),
        },
        "why": _short_join(row.get("strategy_reasons") or row.get("scenario_reasons")),
        "warnings": row.get("strategy_warnings") or row.get("scenario_warnings") or [],
        "invalidates": invalidation_reason,
        "raw": redact_payload(row),
    }


def build_export_package(source_state: Dict[str, Any], watchlist_symbols: Sequence[str] = WATCHLIST_SYMBOLS) -> Dict[str, Any]:
    status = redact_payload(source_state.get("dashboard_status") or {})
    symbols_payload = source_state.get("dashboard_symbols_raw") or {}
    if not isinstance(symbols_payload, dict):
        symbols_payload = {}
    symbol_rows = _extract_symbol_rows(symbols_payload)
    symbol_map = _symbol_key_lookup(symbol_rows)
    symbols: Dict[str, Dict[str, Any]] = {}
    summary_rows: List[Dict[str, Any]] = []
    unavailable_fields: List[str] = []
    for symbol in watchlist_symbols:
        row = symbol_map.get(symbol, {"symbol": symbol})
        summary = _symbol_summary_row(row)
        detail = _symbol_detail(row)
        symbols[symbol] = detail
        summary_rows.append(summary)
        for field_name, value in (
            ("scenario_alert_eligible", summary.get("scenario_alert_eligible")),
            ("scenario_would_sms", summary.get("scenario_would_sms")),
            ("scenario_alert_tier", summary.get("scenario_alert_tier")),
            ("scenario_alert_block_reason", detail["phase3"].get("scenario_alert_block_reason")),
            ("stock_setup_score_reason", summary.get("stock_setup_score_reason")),
            ("vwap", summary.get("vwap")),
            ("ema9", summary.get("ema9")),
            ("ema20", summary.get("ema20")),
        ):
            if value is None:
                unavailable_fields.append(f"{symbol}.{field_name}")
    payload = {
        "exported_at": source_state.get("exported_at") or _now_iso(),
        "source": source_state.get("source", DEFAULT_DASHBOARD_URL),
        "dashboard_status": status,
        "watchlist_summary": summary_rows,
        "symbols": symbols,
        "recent_alerts": redact_payload(source_state.get("recent_alert_logs") or []),
        "recent_scenario_records": redact_payload(source_state.get("recent_scenario_records") or []),
        "recent_option_decisions": redact_payload(source_state.get("recent_option_decisions") or []),
        "recent_notification_events": redact_payload(source_state.get("recent_notification_events") or []),
        "dashboard_symbols_raw": redact_payload(symbols_payload),
        "dashboard_alerts_raw": redact_payload(source_state.get("dashboard_alerts_raw") or {}),
        "unavailable_fields": sorted(set(unavailable_fields)),
        "notes": [],
    }
    if not symbol_rows:
        payload["notes"].append("Dashboard symbols endpoint was unavailable; symbol data was filled from defaults.")
    if not source_state.get("recent_alert_logs"):
        payload["notes"].append("alerts.jsonl was missing or empty.")
    if not source_state.get("recent_scenario_records"):
        payload["notes"].append("scenario_engine.jsonl was missing or empty.")
    if not source_state.get("recent_option_decisions"):
        payload["notes"].append("option_quality_decisions.jsonl was missing or empty.")
    return payload


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def cell(value: Any) -> str:
        if value is None:
            text = "unavailable"
        elif isinstance(value, bool):
            text = "Yes" if value else "No"
        elif isinstance(value, (list, tuple)):
            text = _short_join(value, limit=4)
        else:
            text = str(value)
        return text.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(cell(item) for item in row) + " |")
    return "\n".join(lines)


def render_markdown(snapshot: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# Live Dashboard Snapshot")
    lines.append("")
    lines.append("## Timestamp")
    lines.append(str(snapshot.get("exported_at", "unavailable")))
    lines.append("")
    lines.append("## Watchlist Summary")
    rows = []
    for item in snapshot.get("watchlist_summary", []):
        rows.append([
            item.get("symbol"),
            _fmt_number(item.get("price"), 2),
            item.get("direction"),
            item.get("top_scenario"),
            item.get("stage"),
            item.get("scenario_score"),
            item.get("stock_setup_score"),
            item.get("confirmation_score"),
            item.get("risk_label"),
            item.get("entry_quality_label"),
            item.get("option_feed_status"),
            item.get("scenario_alert_tier"),
            _fmt_bool(item.get("scenario_would_sms")),
            item.get("scenario_sms_block_reason") or item.get("sms_block_reason") or "unavailable",
        ])
    lines.append(_md_table(
        [
            "Symbol",
            "Price",
            "Direction",
            "Top Scenario",
            "Stage",
            "Scenario Score",
            "Stock Setup Score",
            "Confirmation Score",
            "Risk",
            "Entry Quality",
            "Option Feed",
            "Alert Tier",
            "Would SMS",
            "SMS Block Reason",
        ],
        rows,
    ))
    lines.append("")
    lines.append("## Symbol Details")
    for symbol in WATCHLIST_SYMBOLS:
        detail = snapshot.get("symbols", {}).get(symbol, {})
        current = detail.get("current_read", {})
        core = detail.get("core", {})
        phase1 = detail.get("phase1", {})
        phase2 = detail.get("phase2", {})
        phase3 = detail.get("phase3", {})
        options = detail.get("options", {})
        lines.append(f"### {symbol}")
        lines.append(f"- Current read: price {_fmt_number(current.get('price'), 2)}, {current.get('direction', 'unavailable')} | top scenario {current.get('top_scenario', 'unavailable')} | stage {current.get('stage', 'unavailable')} | scenario score {current.get('scenario_score', 'unavailable')} | confirmation {current.get('confirmation_score', 'unavailable')}")
        lines.append(f"- Top scenario: {phase3.get('top_scenario', 'unavailable')}")
        lines.append(f"- Why the bot thinks that: {_short_join(phase1.get('reasons') or phase3.get('scenario_reasons'))}")
        lines.append(f"- What would invalidate it: {_truncate(phase3.get('invalidation_reason'))}")
        lines.append(f"- Stage: {phase3.get('scenario_stage', 'unavailable')}")
        lines.append(f"- Alert tier: {phase3.get('scenario_alert_tier', 'unavailable')} | Alert block: {_truncate(phase3.get('scenario_alert_block_reason'))} | SMS block: {_truncate(phase3.get('scenario_sms_block_reason'))}")
        lines.append(f"- Stock setup score: {current.get('stock_setup_score', phase1.get('stock_setup_score', 'unavailable'))} | Reason: {_truncate(current.get('stock_setup_score_reason') or phase1.get('stock_setup_score_reason'))}")
        lines.append(f"- Options/SMS: feed {options.get('option_feed_status', 'unavailable')}, tradability score {options.get('option_tradability_score', 'unavailable')}, final SMS {_fmt_bool(options.get('final_sms_allowed'))}")
        warning_text = _short_join(detail.get("warnings"))
        lines.append(f"- Warnings: {warning_text}")
        lines.append(f"- Key levels: VWAP {_fmt_number(core.get('vwap'), 2)}, EMA9 {_fmt_number(core.get('ema9'), 2)}, EMA20 {_fmt_number(core.get('ema20'), 2)}, PMH {_fmt_number(core.get('premarket_high'), 2)}, PML {_fmt_number(core.get('premarket_low'), 2)}, PDH {_fmt_number(core.get('previous_day_high'), 2)}, PDL {_fmt_number(core.get('previous_day_low'), 2)}, OR high {_fmt_number(core.get('opening_range_high'), 2)}, OR low {_fmt_number(core.get('opening_range_low'), 2)}")
        lines.append("")
    lines.append("## Recent Alerts")
    alert_rows = []
    for item in snapshot.get("recent_alerts", [])[:20]:
        alert_rows.append([
            item.get("timestamp"),
            item.get("symbol"),
            item.get("direction"),
            item.get("primary_setup") or item.get("category"),
            item.get("strategy_confidence_label") or item.get("confidence_label"),
            item.get("confirmation_label"),
            item.get("risk_label"),
            item.get("entry_quality_label"),
            _fmt_bool(item.get("sms_allowed")),
            item.get("text_alert_reason") or item.get("sms_block_reason") or "unavailable",
        ])
    lines.append(_md_table(
        ["Time", "Symbol", "Direction", "Setup", "Confidence", "Confirmation", "Risk", "Entry Quality", "SMS", "Block Reason"],
        alert_rows,
    ))
    lines.append("")
    lines.append("## Recent Scenario Engine Records")
    scenario_rows = []
    for item in snapshot.get("recent_scenario_records", [])[:20]:
        top = item.get("top_scenario") if isinstance(item.get("top_scenario"), dict) else {}
        scenario_rows.append([
            item.get("timestamp"),
            item.get("symbol"),
            top.get("scenario_name") or item.get("scenario") or item.get("scenario_name"),
            item.get("stage") or top.get("stage"),
            item.get("score") or item.get("scenario_score"),
            _short_join(item.get("reasons")),
            _short_join(item.get("warnings")),
            item.get("scenario_alert_tier"),
            item.get("scenario_alert_block_reason") or "unavailable",
            _fmt_bool(item.get("scenario_would_sms")),
            item.get("scenario_sms_block_reason") or item.get("sms_block_reason") or "unavailable",
        ])
    lines.append(_md_table(
        ["Time", "Symbol", "Top Scenario", "Stage", "Score", "Reasons", "Warnings", "Alert Tier", "Alert Block", "Would SMS", "SMS Block Reason"],
        scenario_rows,
    ))
    lines.append("")
    lines.append("## Recent Option Decisions")
    option_rows = []
    for item in snapshot.get("recent_option_decisions", [])[:20]:
        option_rows.append([
            item.get("timestamp"),
            item.get("symbol"),
            item.get("option_feed_status"),
            item.get("option_tradability_score"),
            _fmt_bool(item.get("stock_setup_valid")),
            _fmt_bool(item.get("dashboard_allowed")),
            _fmt_bool(item.get("final_sms_allowed")),
            item.get("option_warning") or item.get("sms_block_reason") or "unavailable",
        ])
    lines.append(_md_table(
        ["Time", "Symbol", "Option Feed", "Tradability Score", "Stock Setup Valid", "Dashboard Allowed", "SMS Allowed", "Block Reason"],
        option_rows,
    ))
    lines.append("")
    if snapshot.get("notes"):
        lines.append("## Notes")
        for note in snapshot.get("notes", []):
            lines.append(f"- {note}")
        lines.append("")
    if snapshot.get("unavailable_fields"):
        lines.append("## Unavailable Fields")
        for field_name in snapshot.get("unavailable_fields", []):
            lines.append(f"- {field_name}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_export_files(snapshot: Dict[str, Any], output_dir: Path = EXPORT_DIR) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "dashboard_snapshot_latest.json"
    md_path = output_dir / "dashboard_snapshot_latest.md"
    json_path.write_text(json.dumps(redact_payload(snapshot), indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown(snapshot), encoding="utf-8")
    return {"json": json_path, "md": md_path}


def export_dashboard_snapshot(
    *,
    base_url: str = DEFAULT_DASHBOARD_URL,
    log_dir: Path = DEFAULT_LOG_DIR,
    output_dir: Path = EXPORT_DIR,
    fetcher: Callable[[str, float], Optional[Any]] = fetch_json,
) -> Dict[str, Any]:
    source_state = collect_live_sources(base_url=base_url, log_dir=log_dir, fetcher=fetcher)
    snapshot = build_export_package(source_state)
    paths = write_export_files(snapshot, output_dir=output_dir)
    return {"snapshot": snapshot, "paths": paths}
