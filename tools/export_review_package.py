#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

try:
    from tools.review_alert_performance import build_report as build_performance_report
    from tools.review_alert_quality import write_alert_quality_review
except ModuleNotFoundError:
    from review_alert_performance import build_report as build_performance_report
    from review_alert_quality import write_alert_quality_review

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = APP_DIR / "logs"
DEFAULT_SNAPSHOT_DIR = APP_DIR / "exports"
DEFAULT_OUTPUT_DIR = APP_DIR / "exports"
ET = ZoneInfo("America/New_York")

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|account[_-]?id|client[_-]?id|client[_-]?secret|authorization|bearer)",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|authorization|bearer)(\s*[:=]\s*)(['\"]?)[^,'\"\s}]+"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a redacted scanner review package for ChatGPT.")
    parser.add_argument("--date", required=True, help="Trading date in YYYY-MM-DD format.")
    parser.add_argument("--start", default="09:00", help="Window start time in ET, HH:MM.")
    parser.add_argument("--end", default="now", help="Window end time in ET, HH:MM or 'now'.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Base exports directory.")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="Scanner logs directory.")
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR), help="Dashboard snapshot exports directory.")
    parser.add_argument("--config-example", default=str(APP_DIR / "config.example.json"), help="Path to config.example.json.")
    return parser.parse_args()


def parse_local_window(day_text: str, start_text: str, end_text: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(day_text)
    start_h, start_m = [int(part) for part in start_text.split(":", 1)]
    if end_text.strip().lower() == "now":
        current = datetime.now(ET)
        end_dt = current if current.date() == day else datetime.combine(day, time(23, 59, 59), ET)
    else:
        end_h, end_m = [int(part) for part in end_text.split(":", 1)]
        end_dt = datetime.combine(day, time(end_h, end_m), ET)
    return (
        datetime.combine(day, time(start_h, start_m), ET),
        end_dt,
    )


def parse_record_time(record: Dict[str, Any]) -> Optional[datetime]:
    raw = (
        record.get("timestamp")
        or record.get("alert_timestamp")
        or record.get("time")
        or record.get("bar_time")
        or record.get("created_at")
        or record.get("scanner_detected_time")
        or record.get("detected_at")
        or record.get("reviewed_at")
        or record.get("original_time")
        or record.get("episode_updated_at")
    )
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    return value


def redact_text(text: str) -> str:
    return SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            records.append({"raw": redact_text(line)})
            continue
        if isinstance(payload, dict):
            records.append(redact_payload(payload))
    return records


def records_for_day(records: Iterable[Dict[str, Any]], day_text: str) -> List[Dict[str, Any]]:
    return [record for record in records if (parse_record_time(record) and parse_record_time(record).date().isoformat() == day_text)]


def records_in_window(records: Iterable[Dict[str, Any]], start_dt: datetime, end_dt: datetime) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for record in records:
        ts = parse_record_time(record)
        if ts and start_dt <= ts <= end_dt:
            selected.append(record)
    return selected


def write_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(redact_payload(record), ensure_ascii=False, sort_keys=True) + "\n")


def copy_redacted_file(source: Path, destination: Path, notes: List[str]) -> bool:
    if not source.exists():
        notes.append(f"Missing source file: {source.name}")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix == ".json":
        try:
            payload = json.loads(source.read_text(encoding="utf-8", errors="replace"))
            destination.write_text(json.dumps(redact_payload(payload), indent=2, sort_keys=True), encoding="utf-8")
            return True
        except json.JSONDecodeError:
            pass
    destination.write_text(redact_text(source.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    return True


def latest_scanner_log(log_dir: Path) -> Optional[Path]:
    candidates = [path for path in log_dir.glob("*scanner*.log") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def compact_list(values: Any, limit: int = 2) -> str:
    if not values:
        return ""
    if isinstance(values, list):
        return "; ".join(str(item) for item in values[:limit])
    return str(values)


def record_stage(record: Dict[str, Any]) -> str:
    return top_scenario_stage(record).upper()


def record_has_text(record: Dict[str, Any], terms: Iterable[str]) -> bool:
    text = json.dumps(record, sort_keys=True).lower()
    return any(term.lower() in text for term in terms)


def scenario_summary_rows(records: Iterable[Dict[str, Any]]) -> List[List[Any]]:
    return [
        [
            format_ts(record),
            record.get("symbol", ""),
            top_scenario_name(record),
            top_scenario_stage(record),
            record.get("score") or record.get("scenario_score", ""),
            record.get("stock_setup_score", ""),
            record.get("confirmation_score", ""),
            record.get("scenario_alert_tier", ""),
            record.get("scenario_would_sms", ""),
            record.get("scenario_alert_block_reason") or record.get("scenario_sms_block_reason", ""),
        ]
        for record in records
    ]


def heads_up_summary_rows(records: Iterable[Dict[str, Any]]) -> List[List[Any]]:
    return [
        [
            format_ts(record),
            record.get("symbol", ""),
            top_scenario_name(record),
            record.get("scenario_stage", ""),
            record.get("scenario_score", ""),
            record.get("stock_setup_score", ""),
            record.get("confirmation_score", ""),
            record.get("phase3_heads_up_eligible", ""),
            record.get("phase3_heads_up_sent", ""),
            record.get("phase3_heads_up_block_reason", ""),
        ]
        for record in records
    ]


def latest_status_for_day(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    timed = [(parse_record_time(record), record) for record in records]
    valid = [(ts, record) for ts, record in timed if ts]
    return max(valid, key=lambda item: item[0])[1] if valid else {}


def top_scenario_name(record: Dict[str, Any]) -> str:
    top = record.get("scenario_top") or record.get("top_scenario") or {}
    if isinstance(top, dict):
        return str(top.get("scenario_name") or top.get("name") or record.get("top_scenario") or "")
    return str(top or "")


def top_scenario_stage(record: Dict[str, Any]) -> str:
    top = record.get("scenario_top") or record.get("top_scenario") or {}
    if isinstance(top, dict):
        return str(record.get("scenario_stage") or top.get("stage") or record.get("stage") or "")
    return str(record.get("scenario_stage") or record.get("stage") or "")


def format_ts(record: Dict[str, Any]) -> str:
    ts = parse_record_time(record)
    return ts.strftime("%H:%M:%S ET") if ts else "unknown"


def markdown_table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_No records found._\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        cells = [str(cell).replace("\n", " ").replace("|", "\\|") for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def build_review_summary(
    *,
    day_text: str,
    start_text: str,
    end_text: str,
    alert_window: List[Dict[str, Any]],
    scenario_window: List[Dict[str, Any]],
    heads_up_window: List[Dict[str, Any]],
    option_window: List[Dict[str, Any]],
    market_data_records: List[Dict[str, Any]],
    notes: List[str],
    chop_records: Optional[List[Dict[str, Any]]] = None,
    missed_entry_records: Optional[List[Dict[str, Any]]] = None,
    openai_records: Optional[List[Dict[str, Any]]] = None,
    liquidity_sweep_records: Optional[List[Dict[str, Any]]] = None,
    orchestrator_records: Optional[List[Dict[str, Any]]] = None,
    whale_records: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> str:
    chop_records = chop_records or []
    missed_entry_records = missed_entry_records or []
    openai_records = openai_records or []
    liquidity_sweep_records = liquidity_sweep_records or []
    orchestrator_records = orchestrator_records or []
    whale_records = whale_records or {}
    whale_scans = whale_records.get("scans", [])
    whale_episodes = whale_records.get("episodes", [])
    whale_outcomes = whale_records.get("outcomes", [])
    whale_oi = whale_records.get("oi_reviews", [])
    coverage_warnings = [row for row in whale_scans if (row.get("coverage_warning") or (row.get("diagnostics") or {}).get("coverage_warning"))]
    market_status = latest_status_for_day(market_data_records)
    alert_rows = [
        [
            format_ts(record),
            record.get("symbol", ""),
            record.get("direction", ""),
            record.get("primary_setup") or record.get("category", ""),
            record.get("strategy_confidence_score", ""),
            record.get("confirmation_score", ""),
            record.get("risk_label", ""),
            record.get("entry_quality_label", ""),
            record.get("scenario_stage", ""),
            record.get("scenario_alert_tier", ""),
            record.get("scenario_would_sms", record.get("sms_allowed", "")),
            record.get("scenario_sms_block_reason") or record.get("text_alert_reason", ""),
        ]
        for record in alert_window
    ]
    scenario_rows = scenario_summary_rows(scenario_window)
    option_rows = [
        [
            format_ts(record),
            record.get("symbol", ""),
            record.get("option_feed_status", ""),
            record.get("option_tradability_score", ""),
            record.get("stock_setup_valid", ""),
            record.get("option_tradable", ""),
            record.get("dashboard_allowed", ""),
            record.get("final_sms_allowed", ""),
            record.get("sms_block_reason", ""),
        ]
        for record in option_window
    ]
    premarket = [record for record in scenario_window if (parse_record_time(record) and parse_record_time(record).time() < time(9, 30))]
    market_open = [
        record
        for record in scenario_window
        if parse_record_time(record) and time(9, 30) <= parse_record_time(record).time() <= time(10, 0)
    ]
    good_position = [record for record in scenario_window if record_stage(record) == "GOOD_POSITION"]
    late_or_chase = [
        record
        for record in scenario_window
        if record_stage(record) in {"LATE", "DO_NOT_CHASE"} or record_has_text(record, ["DO_NOT_CHASE", "late entry"])
    ]
    feed_warnings = [
        str(record.get("feed_warning"))
        for record in market_data_records
        if record.get("feed_warning")
    ]
    stale_or_feed_warnings = [
        record
        for record in alert_window + scenario_window + option_window
        if record_has_text(record, ["stale", "feed warning", "indicative", "opra agreement"])
    ]
    phone_conclusions = [
        str(record.get("phone_conclusion") or record.get("alert_decision_label") or "").upper()
        for record in alert_window
    ]
    conclusion_counts = {
        label: phone_conclusions.count(label)
        for label in (
            "MIXED / NO TRADE",
            "DO NOT CHASE",
            "WATCH ONLY",
            "TRADE QUALITY WATCH",
            "CONTEXT ONLY",
            "RISK WARNING",
        )
    }
    decision_tiers = [
        str(record.get("decision_tier") or "").upper()
        for record in alert_window
        if record.get("decision_tier")
    ]
    decision_counts = {label: decision_tiers.count(label) for label in sorted(set(decision_tiers))}
    suppressed = [record for record in alert_window if record.get("suppressed_by_chop")]
    structure_downgrades = [record for record in alert_window if record.get("bearish_downgraded_by_structure")]
    near_demand = [record for record in alert_window if record.get("market_structure_near_demand")]
    near_supply = [record for record in alert_window if record.get("market_structure_near_supply")]
    inside_chop = [record for record in alert_window if record.get("chop_mode_active")]
    openai_success = sum(1 for record in openai_records if record.get("formatter_success"))
    openai_fallback = sum(1 for record in openai_records if record.get("fallback_used"))
    sweep_statuses = [str(record.get("sweep_status") or "").upper() for record in liquidity_sweep_records]
    confirmed_sweeps = [record for record in liquidity_sweep_records if str(record.get("sweep_status") or "").upper() == "SWEEP_CONFIRMED"]
    sweep_downgrades = [record for record in alert_window if record.get("downgraded_by_liquidity_sweep")]
    bullish_supply_downgrades = [
        record for record in sweep_downgrades
        if str(record.get("direction") or record.get("scenario_direction") or "").upper() == "BULLISH"
        and "supply" in str(record.get("liquidity_sweep_downgrade_reason") or "").lower()
    ]
    bearish_demand_downgrades = [
        record for record in sweep_downgrades
        if str(record.get("direction") or record.get("scenario_direction") or "").upper() == "BEARISH"
        and "demand" in str(record.get("liquidity_sweep_downgrade_reason") or "").lower()
    ]
    important_level_sweeps = sum(
        1 for record in liquidity_sweep_records
        if str(record.get("level_source") or "").lower() in {"hod", "lod", "pmh", "pml", "pdh", "pdl"}
    )
    supply_demand_sweeps = sum(
        1 for record in liquidity_sweep_records
        if "5m_supply" in str(record.get("level_source") or "").lower()
        or "5m_demand" in str(record.get("level_source") or "").lower()
    )
    engine_strategy_sweeps = sum(
        1 for record in alert_window if str(record.get("liquidity_sweep_source") or "").lower() == "engine"
    )
    legacy_strategy_sweeps = sum(
        1 for record in alert_window if str(record.get("liquidity_sweep_source") or "").lower() == "legacy_fallback"
    )
    sweep_map_updates = sum(1 for record in liquidity_sweep_records if record.get("map_only") or record.get("sweep_map_status"))
    sweep_event_candidates = sum(1 for record in liquidity_sweep_records if record.get("event_alert_candidate"))
    sweep_filter_allowed = sum(1 for record in liquidity_sweep_records if record.get("telegram_filter_allowed"))
    sweep_filter_suppressed = sum(1 for record in liquidity_sweep_records if record.get("telegram_filter_allowed") is False)
    same_zone_suppressed = sum(
        1 for record in liquidity_sweep_records if str(record.get("suppression_type") or "") == "same_zone_cooldown"
    )
    repeated_range_periods = sum(1 for record in liquidity_sweep_records if record.get("repeated_range_sweeps"))
    orchestrator_types = [str(record.get("final_alert_type") or "") for record in orchestrator_records]
    notes_text = "\n".join(f"- {note}" for note in notes) if notes else "- No export issues noted."
    return f"""# Bot Review Package — {day_text}

## Market Data Status
- Stock feed requested/status: {market_status.get("stock_feed_requested", "unavailable")} / {market_status.get("stock_feed_status", "unavailable")}
- Options feed requested/status: {market_status.get("options_feed_requested", "unavailable")} / {market_status.get("options_feed_status", "unavailable")}
- OPRA status: {market_status.get("opra_status", "unavailable")}
- Rate limit mode: {market_status.get("api_rate_limit_mode", "unavailable")}
- Websocket symbol mode: {market_status.get("websocket_symbol_limit", "unavailable")}
- Last data check: {market_status.get("last_data_check_time") or market_status.get("timestamp") or "unavailable"}
- Feed warnings: {"; ".join(dict.fromkeys(feed_warnings)) if feed_warnings else "None recorded"}
- Stale/feed-related records in requested window: {len(stale_or_feed_warnings)}

## Watchlist
- AAPL main focus
- SPY/QQQ market confirmation

## What to analyze
1. Did SIP/OPRA improve the bot's data quality?
2. Did the bot catch AAPL setups earlier?
3. Did Phase 3 heads-up alerts fire correctly?
4. Did the bot separate FORMING, CONFIRMED, GOOD_POSITION, LATE, and DO_NOT_CHASE correctly?
5. Did the bot miss any obvious bullish or bearish setup?
6. Did option data still block or warn correctly?
7. Did alerts come too early, on time, or too late?
8. Were any alerts blocked by:
   - scenario stage
   - confirmation score
   - stock setup score
   - risk
   - candle quality
   - market conflict
   - options/OPRA
   - stale data
   - SMS rules

## Data window
- Requested window: {start_text} ET to {end_text} ET
- Full redacted day logs are included where available.
- Window-filtered JSONL files are included for alerts, scenario engine records, Phase 3 heads-up decisions, option decisions, and market-data status.

## Focused Summary
- Premarket scenario records: {len(premarket)}
- Market-open scenario records (9:30–10:00 ET): {len(market_open)}
- Phase 3 heads-up decisions: {len(heads_up_window)}
- Phase 3 heads-up messages sent: {sum(1 for record in heads_up_window if record.get("phase3_heads_up_sent"))}
- GOOD_POSITION scenario records: {len(good_position)}
- LATE / DO_NOT_CHASE records: {len(late_or_chase)}

## Options Whale Data Quality
- Scan passes: {len(whale_scans)}
- Canonical flow episodes: {len(whale_episodes)}
- Episode outcomes: {len(whale_outcomes)}
- Next-day OI reviews: {len(whale_oi)}
- Scan passes with coverage warnings: {len(coverage_warnings)}
- Latest coverage warning: {(coverage_warnings[-1].get("coverage_warning") or (coverage_warnings[-1].get("diagnostics") or {}).get("coverage_warning")) if coverage_warnings else "None"}

## Phone Conclusions
- Active alert types: PHASE3_HEADS_UP, STOCK_ONLY_WARNING, NORMAL_WATCH, NORMAL_SMS
- Mixed / No Trade: {conclusion_counts["MIXED / NO TRADE"]}
- Do Not Chase: {conclusion_counts["DO NOT CHASE"]}
- Watch Only: {conclusion_counts["WATCH ONLY"]}
- Trade Quality Watch: {conclusion_counts["TRADE QUALITY WATCH"]}
- Context Only: {conclusion_counts["CONTEXT ONLY"]}
- Risk Warning: {conclusion_counts["RISK WARNING"]}

## Decision Quality Cleanup
- Decision tier counts: {json.dumps(decision_counts, sort_keys=True)}
- CHOP_MODE records: {sum(1 for record in chop_records if record.get("chop_mode_active"))}
- Alerts suppressed by chop: {len(suppressed)}
- Mixed alerts suppressed by chop: {sum(1 for record in suppressed if str(record.get("phone_conclusion", "")).upper() == "MIXED / NO TRADE")}
- Do-not-chase alerts suppressed by chop: {sum(1 for record in suppressed if str(record.get("phone_conclusion", "")).upper() == "DO NOT CHASE")}
- Missed clean entry records: {len(missed_entry_records)}
- Bearish downgrades by structure: {len(structure_downgrades)}
- Alerts near demand: {len(near_demand)}
- Alerts near supply: {len(near_supply)}
- Alerts inside chop range: {len(inside_chop)}
- OpenAI formatter success / fallback: {openai_success} / {openai_fallback}

## Alert Orchestrator
- Trade-quality decisions: {orchestrator_types.count("TRADE_QUALITY")}
- Trend-context decisions: {orchestrator_types.count("TREND_CONTEXT")}
- Sweep-event decisions: {orchestrator_types.count("SWEEP_EVENT")}
- Chop warnings: {orchestrator_types.count("CHOP_WARNING")}
- Do-not-chase warnings: {orchestrator_types.count("DO_NOT_CHASE")}
- Mixed/no-trade warnings: {orchestrator_types.count("MIXED_NO_TRADE")}
- Dashboard-only decisions: {orchestrator_types.count("DASHBOARD_ONLY")}
- Watch-only decisions allowed through Chop Mode: {sum(1 for record in orchestrator_records if record.get("final_alert_type") == "TREND_CONTEXT" and record.get("watch_only") and (record.get("engine_votes") or {}).get("chop_mode_active"))}
- Decisions blocked/suppressed by Chop Mode: {sum(1 for record in orchestrator_records if "chop" in str(record.get("block_reason") or record.get("suppression_reason") or "").lower())}
- Trend contexts blocked by dedupe: {sum(1 for record in orchestrator_records if "duplicate" in str(record.get("suppression_reason") or "").lower())}

## Liquidity Sweep Review
- Engine-based strategy sweep records: {engine_strategy_sweeps}
- Legacy fallback strategy sweep records: {legacy_strategy_sweeps}
- Sweep map updates: {sweep_map_updates}
- Sweep event candidates: {sweep_event_candidates}
- Telegram filter allowed / suppressed: {sweep_filter_allowed} / {sweep_filter_suppressed}
- Same-zone bucket suppressions: {same_zone_suppressed}
- Repeated range sweep records: {repeated_range_periods}
- Sweep watch records: {sweep_statuses.count("SWEEP_WATCH")}
- Sweep forming records: {sweep_statuses.count("SWEEP_FORMING")}
- Sweep confirmed records: {len(confirmed_sweeps)}
- Confirmed upside sweeps: {sum(1 for record in confirmed_sweeps if str(record.get("sweep_direction") or "").upper() == "ABOVE_LEVEL")}
- Confirmed downside sweeps: {sum(1 for record in confirmed_sweeps if str(record.get("sweep_direction") or "").upper() == "BELOW_LEVEL")}
- Failed / held sweeps: {sweep_statuses.count("SWEEP_FAILED_HELD")}
- Alerts downgraded by liquidity sweep: {len(sweep_downgrades)}
- Bullish alerts downgraded near supply: {len(bullish_supply_downgrades)}
- Bearish alerts downgraded near demand: {len(bearish_demand_downgrades)}
- Chop records strengthened by sweep risk: {sum(1 for record in chop_records if record.get("sweep_risk_active"))}
- Telegram sweep alerts sent: {sum(1 for record in liquidity_sweep_records if record.get("telegram_sent"))}
- Telegram sweep alerts suppressed by cooldown: {sum(1 for record in liquidity_sweep_records if "cooldown" in str(record.get("telegram_suppressed_reason") or "").lower())}
- OpenAI sweep formatter fallbacks: {sum(1 for record in liquidity_sweep_records if record.get("fallback_used"))}
- Sweeps near HOD/LOD/PMH/PML/PDH/PDL: {important_level_sweeps}
- Sweeps near 5m supply/demand: {supply_demand_sweeps}
- Sweeps inside chop range: {sum(1 for record in liquidity_sweep_records if record.get("inside_chop_range"))}

### Premarket
{markdown_table(["Time", "Symbol", "Top Scenario", "Stage", "Score", "Stock", "Confirm", "Tier", "Would SMS", "Block Reason"], scenario_summary_rows(premarket))}

### Market Open
{markdown_table(["Time", "Symbol", "Top Scenario", "Stage", "Score", "Stock", "Confirm", "Tier", "Would SMS", "Block Reason"], scenario_summary_rows(market_open))}

### Phase 3 Heads-Up Alerts
{markdown_table(["Time", "Symbol", "Top Scenario", "Stage", "Scenario", "Stock", "Confirm", "Eligible", "Sent", "Block Reason"], heads_up_summary_rows(heads_up_window))}

### GOOD_POSITION Setups
{markdown_table(["Time", "Symbol", "Top Scenario", "Stage", "Score", "Stock", "Confirm", "Tier", "Would SMS", "Block Reason"], scenario_summary_rows(good_position))}

### LATE / DO_NOT_CHASE Warnings
{markdown_table(["Time", "Symbol", "Top Scenario", "Stage", "Score", "Stock", "Confirm", "Tier", "Would SMS", "Block Reason"], scenario_summary_rows(late_or_chase))}

## Window Alert Records
{markdown_table(["Time", "Symbol", "Dir", "Setup", "Strategy", "Confirm", "Risk", "Entry", "Stage", "Tier", "Would SMS", "Block Reason"], alert_rows)}

## Window Scenario Records
{markdown_table(["Time", "Symbol", "Top Scenario", "Stage", "Score", "Stock", "Confirm", "Tier", "Would SMS", "Block Reason"], scenario_rows)}

## Window Option Decisions
{markdown_table(["Time", "Symbol", "Feed", "Option Score", "Stock Valid", "Option Tradable", "Dashboard", "Final SMS", "Block Reason"], option_rows)}

## Included Files
- `logs/alerts.jsonl`
- `logs/scenario_engine.jsonl`
- `logs/phase3_heads_up.jsonl`
- `logs/option_quality_decisions.jsonl`
- `logs/market_data_status.jsonl`
- `logs/market_regime.jsonl`
- `logs/multi_timeframe_context.jsonl`
- `logs/post_alert_performance.jsonl`
- `logs/news_context.jsonl`
- `logs/support_resistance_levels.jsonl`
- `logs/supply_demand_zones.jsonl`
- `logs/market_structure.jsonl`
- `logs/liquidity_sweeps.jsonl`
- `logs/options_whale_scans.jsonl`
- `logs/options_whale_qualified_events.jsonl`
- `logs/options_whale_alerts.jsonl`
- `logs/options_whale_episodes.jsonl`
- `logs/options_oi_reviews.jsonl`
- `data/options_whale_episode_outcomes.jsonl`
- `logs/chop_mode.jsonl` if available
- `logs/missed_clean_entry.jsonl` if available
- `logs/alert_orchestrator.jsonl` if available
- `logs/market_map_updates.jsonl` if available
- `logs/morning_playbook.jsonl` if available
- `logs/openai_alert_formatter.jsonl` if available
- `logs/premarket_discipline_message.jsonl` if available
- latest scanner log if available
- `dashboard_snapshot_latest.md`
- `dashboard_snapshot_latest.json`
- `config.example.json`
- `window/alerts_window.jsonl`
- `window/scenario_engine_window.jsonl`
- `window/phase3_heads_up_window.jsonl`
- `window/option_quality_decisions_window.jsonl`
- `window/market_data_status_window.jsonl`
- `window/post_alert_performance_window.jsonl`
- `window/news_context_window.jsonl`
- `window/support_resistance_levels_window.jsonl`
- `window/supply_demand_zones_window.jsonl`
- `window/market_structure_window.jsonl`
- `window/liquidity_sweeps_window.jsonl`
- `alert_performance_{day_text}.md` if generated
- `alert_quality_review_{day_text}.md`
- `alert_quality_review_{day_text}.json`

## Export Notes
{notes_text}
"""


def create_zip(package_dir: Path) -> Path:
    zip_path = package_dir / "review_package.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package_dir.rglob("*")):
            if path == zip_path:
                continue
            archive.write(path, path.relative_to(package_dir))
    return zip_path


def export_review_package(
    *,
    day_text: str,
    start_text: str,
    end_text: str,
    output_dir: Path,
    log_dir: Path,
    snapshot_dir: Path,
    config_example: Path,
) -> Dict[str, Path]:
    start_dt, end_dt = parse_local_window(day_text, start_text, end_text)
    package_dir = output_dir / f"review_package_{day_text}"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    logs_out = package_dir / "logs"
    window_out = package_dir / "window"
    package_dir.mkdir(parents=True, exist_ok=True)
    notes: List[str] = []

    alerts = records_for_day(read_jsonl(log_dir / "alerts.jsonl"), day_text)
    scenarios = records_for_day(read_jsonl(log_dir / "scenario_engine.jsonl"), day_text)
    heads_up = records_for_day(read_jsonl(log_dir / "phase3_heads_up.jsonl"), day_text)
    options = records_for_day(read_jsonl(log_dir / "option_quality_decisions.jsonl"), day_text)
    market_data = records_for_day(read_jsonl(log_dir / "market_data_status.jsonl"), day_text)
    market_regimes = records_for_day(read_jsonl(log_dir / "market_regime.jsonl"), day_text)
    market_regime_heartbeats = records_for_day(read_jsonl(log_dir / "market_regime_heartbeat.jsonl"), day_text)
    multi_timeframe = records_for_day(read_jsonl(log_dir / "multi_timeframe_context.jsonl"), day_text)
    notifications = records_for_day(read_jsonl(log_dir / "notification_status.jsonl"), day_text)
    startup_status = records_for_day(read_jsonl(log_dir / "scanner_startup_status.jsonl"), day_text)
    option_diagnostics = records_for_day(read_jsonl(log_dir / "option_freshness_diagnostic.jsonl"), day_text)
    post_alert_performance = records_for_day(read_jsonl(log_dir / "post_alert_performance.jsonl"), day_text)
    news_context = records_for_day(read_jsonl(log_dir / "news_context.jsonl"), day_text)
    support_resistance = records_for_day(read_jsonl(log_dir / "support_resistance_levels.jsonl"), day_text)
    supply_demand = records_for_day(read_jsonl(log_dir / "supply_demand_zones.jsonl"), day_text)
    market_structure = records_for_day(read_jsonl(log_dir / "market_structure.jsonl"), day_text)
    liquidity_sweeps = records_for_day(read_jsonl(log_dir / "liquidity_sweeps.jsonl"), day_text)
    openai_formatter = records_for_day(read_jsonl(log_dir / "openai_alert_formatter.jsonl"), day_text)
    premarket_discipline = records_for_day(read_jsonl(log_dir / "premarket_discipline_message.jsonl"), day_text)
    chop_mode = records_for_day(read_jsonl(log_dir / "chop_mode.jsonl"), day_text)
    missed_clean_entry = records_for_day(read_jsonl(log_dir / "missed_clean_entry.jsonl"), day_text)
    alert_orchestrator = records_for_day(read_jsonl(log_dir / "alert_orchestrator.jsonl"), day_text)
    market_map_updates = records_for_day(read_jsonl(log_dir / "market_map_updates.jsonl"), day_text)
    morning_playbook = records_for_day(read_jsonl(log_dir / "morning_playbook.jsonl"), day_text)
    whale_sources = {
        "scans": log_dir / "options_whale_scans.jsonl",
        "qualified_events": log_dir / "options_whale_qualified_events.jsonl",
        "alerts": log_dir / "options_whale_alerts.jsonl",
        "episodes": log_dir / "options_whale_episodes.jsonl",
        "oi_reviews": log_dir / "options_oi_reviews.jsonl",
        "price_observations": log_dir / "options_price_observations.jsonl",
    }
    whale_records = {name: records_for_day(read_jsonl(path), day_text) for name, path in whale_sources.items()}
    data_dir = log_dir.parent / "data"
    outcome_sources = {
        "outcomes": data_dir / "options_whale_episode_outcomes.jsonl",
        "legacy_outcomes": data_dir / "options_whale_outcomes.jsonl",
    }
    whale_records.update({name: records_for_day(read_jsonl(path), day_text) for name, path in outcome_sources.items()})

    write_jsonl(logs_out / "alerts.jsonl", alerts)
    write_jsonl(logs_out / "scenario_engine.jsonl", scenarios)
    write_jsonl(logs_out / "phase3_heads_up.jsonl", heads_up)
    write_jsonl(logs_out / "option_quality_decisions.jsonl", options)
    write_jsonl(logs_out / "market_data_status.jsonl", market_data)
    write_jsonl(logs_out / "market_regime.jsonl", market_regimes)
    write_jsonl(logs_out / "market_regime_heartbeat.jsonl", market_regime_heartbeats)
    write_jsonl(logs_out / "multi_timeframe_context.jsonl", multi_timeframe)
    write_jsonl(logs_out / "notification_status.jsonl", notifications)
    write_jsonl(logs_out / "scanner_startup_status.jsonl", startup_status)
    write_jsonl(logs_out / "option_freshness_diagnostic.jsonl", option_diagnostics)
    write_jsonl(logs_out / "post_alert_performance.jsonl", post_alert_performance)
    write_jsonl(logs_out / "news_context.jsonl", news_context)
    write_jsonl(logs_out / "support_resistance_levels.jsonl", support_resistance)
    write_jsonl(logs_out / "supply_demand_zones.jsonl", supply_demand)
    write_jsonl(logs_out / "market_structure.jsonl", market_structure)
    write_jsonl(logs_out / "liquidity_sweeps.jsonl", liquidity_sweeps)
    if (log_dir / "openai_alert_formatter.jsonl").exists():
        write_jsonl(logs_out / "openai_alert_formatter.jsonl", openai_formatter)
    if (log_dir / "premarket_discipline_message.jsonl").exists():
        write_jsonl(logs_out / "premarket_discipline_message.jsonl", premarket_discipline)
    if (log_dir / "chop_mode.jsonl").exists():
        write_jsonl(logs_out / "chop_mode.jsonl", chop_mode)
    if (log_dir / "missed_clean_entry.jsonl").exists():
        write_jsonl(logs_out / "missed_clean_entry.jsonl", missed_clean_entry)
    if (log_dir / "alert_orchestrator.jsonl").exists():
        write_jsonl(logs_out / "alert_orchestrator.jsonl", alert_orchestrator)
    if (log_dir / "market_map_updates.jsonl").exists():
        write_jsonl(logs_out / "market_map_updates.jsonl", market_map_updates)
    if (log_dir / "morning_playbook.jsonl").exists():
        write_jsonl(logs_out / "morning_playbook.jsonl", morning_playbook)
    for name, source in whale_sources.items():
        if source.exists():
            write_jsonl(logs_out / source.name, whale_records[name])
            write_jsonl(window_out / f"{source.stem}_window.jsonl", records_in_window(whale_records[name], start_dt, end_dt))
    for name, source in outcome_sources.items():
        if source.exists():
            write_jsonl(package_dir / "data" / source.name, whale_records[name])
    write_jsonl(window_out / "alerts_window.jsonl", records_in_window(alerts, start_dt, end_dt))
    write_jsonl(window_out / "scenario_engine_window.jsonl", records_in_window(scenarios, start_dt, end_dt))
    write_jsonl(window_out / "phase3_heads_up_window.jsonl", records_in_window(heads_up, start_dt, end_dt))
    write_jsonl(window_out / "option_quality_decisions_window.jsonl", records_in_window(options, start_dt, end_dt))
    write_jsonl(window_out / "market_data_status_window.jsonl", records_in_window(market_data, start_dt, end_dt))
    write_jsonl(window_out / "market_regime_window.jsonl", records_in_window(market_regimes, start_dt, end_dt))
    write_jsonl(window_out / "market_regime_heartbeat_window.jsonl", records_in_window(market_regime_heartbeats, start_dt, end_dt))
    write_jsonl(window_out / "multi_timeframe_context_window.jsonl", records_in_window(multi_timeframe, start_dt, end_dt))
    write_jsonl(window_out / "notification_status_window.jsonl", records_in_window(notifications, start_dt, end_dt))
    write_jsonl(window_out / "post_alert_performance_window.jsonl", records_in_window(post_alert_performance, start_dt, end_dt))
    write_jsonl(window_out / "news_context_window.jsonl", records_in_window(news_context, start_dt, end_dt))
    write_jsonl(window_out / "support_resistance_levels_window.jsonl", records_in_window(support_resistance, start_dt, end_dt))
    write_jsonl(window_out / "supply_demand_zones_window.jsonl", records_in_window(supply_demand, start_dt, end_dt))
    write_jsonl(window_out / "market_structure_window.jsonl", records_in_window(market_structure, start_dt, end_dt))
    write_jsonl(window_out / "liquidity_sweeps_window.jsonl", records_in_window(liquidity_sweeps, start_dt, end_dt))
    if chop_mode:
        write_jsonl(window_out / "chop_mode_window.jsonl", records_in_window(chop_mode, start_dt, end_dt))
    if missed_clean_entry:
        write_jsonl(window_out / "missed_clean_entry_window.jsonl", records_in_window(missed_clean_entry, start_dt, end_dt))
    if alert_orchestrator:
        write_jsonl(window_out / "alert_orchestrator_window.jsonl", records_in_window(alert_orchestrator, start_dt, end_dt))

    if not alerts:
        notes.append("No alerts.jsonl records found for the requested date.")
    if not scenarios:
        notes.append("No scenario_engine.jsonl records found for the requested date.")
    if not heads_up:
        notes.append("No phase3_heads_up.jsonl records found for the requested date.")
    if not options:
        notes.append("No option_quality_decisions.jsonl records found for the requested date.")
    if not market_data:
        notes.append("No market_data_status.jsonl records found for the requested date.")
    if not market_regimes:
        notes.append("No market_regime.jsonl records found for the requested date.")
    if not multi_timeframe:
        notes.append("No multi_timeframe_context.jsonl records found for the requested date.")
    if not notifications:
        notes.append("No notification_status.jsonl records found for the requested date.")
    if not post_alert_performance:
        notes.append("No post_alert_performance.jsonl records found for the requested date.")
    if not news_context:
        notes.append("No news_context.jsonl records found for the requested date.")
    if not support_resistance:
        notes.append("No support_resistance_levels.jsonl records found for the requested date.")
    if not supply_demand:
        notes.append("No supply_demand_zones.jsonl records found for the requested date.")
    if not market_structure:
        notes.append("No market_structure.jsonl records found for the requested date.")
    if not liquidity_sweeps:
        notes.append("No liquidity_sweeps.jsonl records found for the requested date.")

    scanner_log = log_dir / "scanner.log"
    if not scanner_log.exists():
        scanner_log = latest_scanner_log(log_dir) or scanner_log
    copy_redacted_file(scanner_log, logs_out / scanner_log.name, notes)
    copy_redacted_file(snapshot_dir / "dashboard_snapshot_latest.md", package_dir / "dashboard_snapshot_latest.md", notes)
    copy_redacted_file(snapshot_dir / "dashboard_snapshot_latest.json", package_dir / "dashboard_snapshot_latest.json", notes)
    copy_redacted_file(config_example, package_dir / "config.example.json", notes)
    performance_report = package_dir / f"alert_performance_{day_text}.md"
    latest_performance = {
        str(record.get("alert_id")): record
        for record in post_alert_performance
        if record.get("alert_id")
    }
    performance_report.write_text(
        build_performance_report(day_text, list(latest_performance.values())),
        encoding="utf-8",
    )
    quality_paths = write_alert_quality_review(day_text, log_dir, package_dir)

    summary = build_review_summary(
        day_text=day_text,
        start_text=start_text,
        end_text=end_text,
        alert_window=records_in_window(alerts, start_dt, end_dt),
        scenario_window=records_in_window(scenarios, start_dt, end_dt),
        heads_up_window=records_in_window(heads_up, start_dt, end_dt),
        option_window=records_in_window(options, start_dt, end_dt),
        market_data_records=market_data,
        notes=notes,
        chop_records=chop_mode,
        missed_entry_records=missed_clean_entry,
        openai_records=openai_formatter,
        liquidity_sweep_records=liquidity_sweeps,
        orchestrator_records=alert_orchestrator,
        whale_records=whale_records,
    )
    summary_path = package_dir / "review_summary.md"
    summary_path.write_text(summary, encoding="utf-8")
    zip_path = create_zip(package_dir)
    return {
        "package_dir": package_dir,
        "summary": summary_path,
        "zip": zip_path,
        "alert_quality_markdown": quality_paths["markdown"],
        "alert_quality_json": quality_paths["json"],
    }


def main() -> int:
    args = parse_args()
    paths = export_review_package(
        day_text=args.date,
        start_text=args.start,
        end_text=args.end,
        output_dir=Path(args.output_dir).resolve(),
        log_dir=Path(args.log_dir).resolve(),
        snapshot_dir=Path(args.snapshot_dir).resolve(),
        config_example=Path(args.config_example).resolve(),
    )
    print(f"Package: {paths['package_dir']}")
    print(f"Markdown: {paths['summary']}")
    print(f"Zip: {paths['zip']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
