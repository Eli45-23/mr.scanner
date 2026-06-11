#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import elite_momentum_scanner as scanner_app
import requests
from tools import dashboard_snapshot_exporter as snapshot_exporter
from tools.preview_market_structure import build_market_structure, write_logs as write_market_structure_logs

APP_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("scanner_dashboard")
ET = ZoneInfo("America/New_York")
MARKET_STRUCTURE_LOG_PATHS = {
    "support_resistance": APP_DIR / "logs" / "support_resistance_levels.jsonl",
    "supply_demand": APP_DIR / "logs" / "supply_demand_zones.jsonl",
    "summary": APP_DIR / "logs" / "market_structure.jsonl",
}


def _read_jsonl_records(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _latest_by_timeframe(records: List[Dict[str, Any]], symbol: str = "AAPL") -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for record in records:
        timeframe = str(record.get("timeframe") or "")
        if timeframe not in {"1m", "5m", "15m"} or str(record.get("symbol") or "").upper() != symbol:
            continue
        latest[timeframe] = record
    return latest


def _market_session_label(now: Optional[datetime] = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(ET)
    minutes = current.hour * 60 + current.minute
    if current.weekday() >= 5 or minutes < 4 * 60 or minutes >= 20 * 60:
        return "After-hours / limited structure"
    if minutes < 9 * 60 + 30 or minutes >= 16 * 60:
        return "After-hours / limited structure"
    return "Live scanner data"


def load_market_structure_dashboard(
    config: Optional[Dict[str, Any]] = None,
    *,
    paths: Optional[Dict[str, Path]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    config = config or scanner_app.load_config(STATE.config_path)
    settings = config.get("market_structure_engines", {})
    enabled = bool(settings.get("enable_dashboard", True))
    support_enabled = bool(settings.get("enable_support_resistance_engine", True))
    supply_enabled = bool(settings.get("enable_supply_demand_engine", True))
    empty = {
        "enabled": enabled,
        "engine_status": "ON" if enabled and (support_enabled or supply_enabled) else "OFF",
        "support_resistance_status": "ON" if support_enabled else "OFF",
        "supply_demand_status": "ON" if supply_enabled else "OFF",
        "last_updated": None,
        "data_mode": "waiting for data",
        "message": "Waiting for market structure data.",
        "summary": {},
        "nearest": {"support": {}, "resistance": {}, "demand": {}, "supply": {}},
        "copy_summary": "Not enough clean data yet",
        "support_resistance": {frame: {} for frame in ("1m", "5m", "15m")},
        "supply_demand": {frame: {} for frame in ("1m", "5m", "15m")},
        "can_upgrade": bool(settings.get("can_upgrade", False)),
        "context_only": True,
    }
    if not enabled:
        return {**empty, "data_mode": "disabled", "message": "Market structure dashboard is disabled."}

    source_paths = paths or MARKET_STRUCTURE_LOG_PATHS
    summary_records = [
        record
        for record in _read_jsonl_records(source_paths["summary"])
        if str(record.get("symbol") or "").upper() == "AAPL"
    ]
    support_records = _latest_by_timeframe(_read_jsonl_records(source_paths["support_resistance"]))
    supply_records = _latest_by_timeframe(_read_jsonl_records(source_paths["supply_demand"]))
    summary = summary_records[-1] if summary_records else {}
    timestamps = [
        str(record.get("timestamp"))
        for record in list(support_records.values()) + list(supply_records.values()) + ([summary] if summary else [])
        if record.get("timestamp")
    ]
    if not summary and not support_records and not supply_records:
        return empty

    current_price = summary.get("current_price")

    def nearest_record(
        records: Dict[str, Dict[str, Any]],
        group: str,
        item_type: str,
        price_field: str,
        below: bool,
    ) -> Dict[str, Any]:
        candidates: List[Dict[str, Any]] = []
        for timeframe, record in records.items():
            for item in (record.get(group) or {}).get(item_type, []):
                value = item.get(price_field)
                if isinstance(value, (int, float)):
                    candidates.append({**item, "timeframe": timeframe})
        if not candidates or not isinstance(current_price, (int, float)):
            return {}
        eligible = [
            item
            for item in candidates
            if (item[price_field] <= current_price if below else item[price_field] >= current_price)
        ]
        return min(eligible, key=lambda item: abs(item[price_field] - current_price), default={})

    nearest = {
        "support": nearest_record(support_records, "levels", "support", "price", True),
        "resistance": nearest_record(support_records, "levels", "resistance", "price", False),
        "demand": nearest_record(supply_records, "zones", "demand", "midpoint", True),
        "supply": nearest_record(supply_records, "zones", "supply", "midpoint", False),
    }

    copy_lines: List[str] = []
    for timeframe in ("1m", "5m", "15m"):
        support_record = support_records.get(timeframe, {})
        supply_record = supply_records.get(timeframe, {})
        for label, key in (("Support", "support"), ("Resistance", "resistance")):
            levels = (support_record.get("levels") or {}).get(key, [])
            if levels:
                item = levels[0]
                copy_lines.append(
                    f"{timeframe} {label}: {float(item['price']):.2f} | {item.get('strength', 'Not enough clean data yet')} | "
                    f"{item.get('source') or item.get('reason') or 'Not enough clean data yet'}"
                )
        for label, key in (("Demand", "demand"), ("Supply", "supply")):
            zones = (supply_record.get("zones") or {}).get(key, [])
            if zones:
                item = zones[0]
                tested = "Fresh" if item.get("fresh") else f"Tested {item.get('times_tested', 0)}x"
                copy_lines.append(
                    f"{timeframe} {label}: {float(item['zone_low']):.2f}-{float(item['zone_high']):.2f} | "
                    f"{item.get('strength', 'Not enough clean data yet')} | {tested} | "
                    f"{item.get('last_reaction') or item.get('reason') or 'Not enough clean data yet'}"
                )
    session_label = _market_session_label(now)
    data_mode = session_label if session_label.startswith("After-hours") else "latest log fallback"
    return {
        **empty,
        "last_updated": max(timestamps) if timestamps else None,
        "data_mode": data_mode,
        "message": "After-hours / limited structure" if session_label.startswith("After-hours") else "Latest market-structure log data",
        "summary": summary,
        "nearest": nearest,
        "copy_summary": "\n".join(copy_lines) if copy_lines else "Not enough clean data yet",
        "support_resistance": {frame: support_records.get(frame, {}) for frame in ("1m", "5m", "15m")},
        "supply_demand": {frame: supply_records.get(frame, {}) for frame in ("1m", "5m", "15m")},
    }


class DashboardState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.running = False
        self.mode = "live"
        self.scope = "watchlist"
        self.started_at: Optional[str] = None
        self.last_scan_at: Optional[str] = None
        self.last_alert_count = 0
        self.last_error: Optional[str] = None
        self.scan_count = 0
        self.last_symbol_count = 0
        self.last_discovery_count = 0
        self.symbol_rows: List[Dict[str, Any]] = []
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.config_path: Optional[Path] = None
        self.market_structure_live: Optional[Dict[str, Any]] = None
        self.market_structure_updated_monotonic = 0.0

    def snapshot(self) -> Dict[str, Any]:
        config = scanner_app.load_config(self.config_path)
        market_data_status = scanner_app.latest_market_data_status(config)
        notification_status = scanner_app.latest_notification_status(config)
        scanner_identity = scanner_app.scanner_identity(config)
        with self.lock:
            live_structure = self.market_structure_live
        fallback_structure = live_structure or load_market_structure_dashboard(config)
        with self.lock:
            market_structure = dict(self.market_structure_live or fallback_structure)
            if _market_session_label().startswith("After-hours") and market_structure.get("last_updated"):
                market_structure["data_mode"] = "After-hours / limited structure"
                market_structure["message"] = "After-hours / limited structure"
            return {
                "running": self.running,
                "mode": self.mode,
                "scope": self.scope,
                "started_at": self.started_at,
                "last_scan_at": self.last_scan_at,
                "last_alert_count": self.last_alert_count,
                "last_error": self.last_error,
                "scan_count": self.scan_count,
                "last_symbol_count": self.last_symbol_count,
                "last_discovery_count": self.last_discovery_count,
                "top_score": self.symbol_rows[0]["score"] if self.symbol_rows else 0,
                "symbols": config["symbols"],
                "interval": config["scan_interval_seconds"],
                "has_alpaca_key": bool(os.getenv("ALPACA_API_KEY")),
                "has_alpaca_secret": bool(os.getenv("ALPACA_SECRET_KEY")),
                "has_openai_key": bool(os.getenv("OPENAI_API_KEY")),
                "has_ai_review_enabled": ai_review_enabled(),
                "has_discord_webhook": bool(os.getenv("DISCORD_WEBHOOK_URL")),
                "has_sms_alerts": bool(os.getenv("ALERT_SMS_PHONE") and config.get("notifications", {}).get("messages_enabled", False)),
                "has_pushover_alerts": bool(os.getenv("PUSHOVER_APP_TOKEN") and os.getenv("PUSHOVER_USER_KEY")),
                "has_desktop_alerts": bool(config.get("notifications", {}).get("mac_desktop_enabled", True)),
                "market_data_status": market_data_status,
                "notification_status": notification_status,
                "scanner_identity": scanner_identity,
                "market_structure": market_structure,
            }


STATE = DashboardState()
ALPACA_HEALTH_CACHE_SECONDS = 30
ALPACA_HEALTH_LOCK = threading.RLock()
ALPACA_HEALTH_CACHE: Dict[str, Any] = {"result": None, "checked_monotonic": 0.0}
OPENAI_ANALYSIS_CACHE_SECONDS = 30
OPENAI_ANALYSIS_LOCK = threading.RLock()
OPENAI_ANALYSIS_CACHE: Dict[str, Any] = {"result": None, "checked_monotonic": 0.0}
AI_REVIEW_ERROR_MISSING_KEY = "AI Review unavailable — OPENAI_API_KEY is not set."
AI_REVIEW_ERROR_DISABLED = "AI Review is disabled."
AI_REVIEW_ERROR_TIMEOUT = "AI Review timed out — scanner data is still available."
AI_REVIEW_ERROR_INVALID_JSON = "AI Review returned an invalid format — scanner data is still available."
AI_REVIEW_ERROR_FAILED = "AI Review failed — scanner data is still available."
AI_REVIEW_DISCLAIMER = (
    "AI Review analyzes scanner output for timing, direction quality, missed context, and possible rule tuning. "
    "It does not place trades, send alerts, change scanner logic, or replace Alpaca data."
)


def refresh_live_market_structure(
    snapshots: Dict[str, scanner_app.SymbolSnapshot],
    config: Dict[str, Any],
    *,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    settings = config.get("market_structure_engines", {})
    if not settings.get("enable_dashboard", True):
        return None
    refresh_seconds = max(1, int(settings.get("refresh_seconds", 15)))
    current_monotonic = time.monotonic()
    with STATE.lock:
        if (
            not force
            and STATE.market_structure_live
            and current_monotonic - STATE.market_structure_updated_monotonic < refresh_seconds
        ):
            return STATE.market_structure_live
    snap = snapshots.get("AAPL")
    if not snap or not snap.recent_bars:
        return None
    payload = build_market_structure("AAPL", snap.recent_bars, daily_bars=snap.daily_bars, config=config)
    write_market_structure_logs(payload, config)
    dashboard_payload = load_market_structure_dashboard(config)
    dashboard_payload["data_mode"] = _market_session_label()
    dashboard_payload["message"] = "Live scanner candle data"
    with STATE.lock:
        STATE.market_structure_live = dashboard_payload
        STATE.market_structure_updated_monotonic = current_monotonic
    return dashboard_payload


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_iso_now() -> str:
    return datetime.now().astimezone().isoformat()


def make_scanner(mode: str) -> scanner_app.EliteScanner:
    config = scanner_app.load_config(STATE.config_path)
    provider = scanner_app.make_provider(mode, list(config["symbols"]), config)
    notifier = scanner_app.make_notifier(config)
    writer = scanner_app.AlertWriter(
        Path(config["outputs"]["csv_log"]),
        Path(config["outputs"]["jsonl_log"]),
    )
    state_store = scanner_app.StateStore(Path(config["outputs"]["state_file"]))
    return scanner_app.EliteScanner(config, provider, notifier, writer, state_store)


def chunked(items: List[str], size: int) -> List[List[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def rank_discovery_candidates(app: scanner_app.EliteScanner, symbols: List[str]) -> List[str]:
    discovery = app.config.get("discovery", {})
    batch_size = max(1, int(discovery.get("batch_size", 150)))
    max_candidates = max(1, int(discovery.get("max_candidates", 100)))
    filters = app.config["filters"]
    ranked: List[tuple[float, str]] = []

    for batch in chunked(symbols, batch_size):
        try:
            latest = app.provider.get_latest_bars(batch)
        except Exception as exc:
            logger.warning("Discovery batch failed: %s", exc)
            continue
        for symbol, bar in latest.items():
            if bar.c < filters["min_price"] or bar.c > filters["max_price"]:
                continue
            dollar_volume = bar.c * bar.v
            ranked.append((dollar_volume, symbol))

    ranked.sort(reverse=True)
    return [symbol for _, symbol in ranked[:max_candidates]]


def apply_scan_scope(app: scanner_app.EliteScanner, scope: str) -> tuple[Dict[str, str], int]:
    watchlist = list(app.symbols)
    sources = {symbol: "Watchlist" for symbol in watchlist}
    if scope == "watchlist":
        return sources, 0
    if scope not in {"discovery", "hybrid"}:
        raise ValueError("Scope must be watchlist, discovery, or hybrid.")

    discovered = app.provider.discover_symbols(app.config)
    candidates = rank_discovery_candidates(app, discovered)
    if scope == "discovery":
        app.symbols = candidates
        return {symbol: "Discovery" for symbol in candidates}, len(candidates)

    merged = list(dict.fromkeys(list(app.symbols) + candidates))
    app.symbols = merged
    for symbol in candidates:
        sources[symbol] = "Both" if symbol in watchlist else "Discovery"
    return sources, len(candidates)


def truncate_text(value: str, limit: int = 1200) -> str:
    clean = value.strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "..."


def alpaca_cli_path() -> Optional[str]:
    configured = os.getenv("ALPACA_CLI_PATH")
    if configured:
        return configured if Path(configured).exists() else None
    return shutil.which("alpaca")


def alpaca_cli_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["ALPACA_QUIET"] = "1"
    for key in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "APCA_API_BASE_URL",
    ):
        env.pop(key, None)
    return env


def alpaca_cli_profile_args() -> List[str]:
    profile = os.getenv("ALPACA_CLI_PROFILE", "").strip()
    return ["--profile", profile] if profile else []


def friendly_command_failure(command_name: str, returncode: Optional[int], stderr: str, stdout: str) -> tuple[str, str]:
    combined = f"{stderr}\n{stdout}".lower()
    if returncode == 2 or "unauthorized" in combined or "auth" in combined or "credential" in combined:
        return "auth_error", f"{command_name} could not authenticate. Check the Alpaca CLI profile before market open."
    return "command_failed", f"{command_name} failed. Check the Alpaca CLI output and profile."


def run_alpaca_cli(
    cli_path: str,
    command_name: str,
    args: List[str],
    timeout: int,
    expect_json: bool = False,
) -> Dict[str, Any]:
    base_result: Dict[str, Any] = {
        "command_name": command_name,
        "status": "error",
        "error_type": None,
        "returncode": None,
        "parsed_json": None,
        "raw_output": "",
        "stderr": "",
        "user_friendly_message": None,
    }
    try:
        completed = subprocess.run(
            [cli_path, "--quiet", *alpaca_cli_profile_args(), *args],
            text=True,
            capture_output=True,
            env=alpaca_cli_env(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = dict(base_result)
        result.update({
            "error_type": "timeout",
            "raw_output": truncate_text(exc.stdout or ""),
            "stderr": truncate_text(exc.stderr or ""),
            "user_friendly_message": f"{command_name} timed out after {timeout} seconds.",
        })
        return result
    except Exception as exc:
        result = dict(base_result)
        result.update({
            "error_type": "subprocess_error",
            "stderr": truncate_text(str(exc)),
            "user_friendly_message": f"{command_name} could not run. Check CLI path and permissions.",
        })
        return result

    stdout = truncate_text(completed.stdout)
    stderr = truncate_text(completed.stderr)
    result = dict(base_result)
    result.update({
        "returncode": completed.returncode,
        "raw_output": stdout,
        "stderr": stderr,
    })

    if completed.returncode != 0:
        error_type, message = friendly_command_failure(command_name, completed.returncode, stderr, stdout)
        result.update({
            "error_type": error_type,
            "user_friendly_message": message,
        })
        return result

    if expect_json:
        if not stdout:
            result.update({
                "error_type": "invalid_json",
                "user_friendly_message": f"{command_name} returned no JSON output.",
            })
            return result
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            result.update({
                "error_type": "invalid_json",
                "user_friendly_message": f"{command_name} returned non-JSON output.",
            })
            return result
        if not isinstance(parsed, dict):
            result.update({
                "error_type": "invalid_json",
                "user_friendly_message": f"{command_name} returned JSON that was not an object.",
            })
            return result
        result["parsed_json"] = parsed

    result["status"] = "ok"
    result["error_type"] = None
    result["user_friendly_message"] = None
    return result


def is_live_trade_mode() -> bool:
    return os.getenv("ALPACA_LIVE_TRADE", "").strip().lower() == "true"


def alpaca_cache_message(age: int) -> str:
    return f"Cached result - checked {age} seconds ago"


def apply_alpaca_cache_fields(result: Dict[str, Any], cached: bool, age: int = 0) -> Dict[str, Any]:
    out = json.loads(json.dumps(result))
    out["cached"] = cached
    out["cache_age_seconds"] = age
    out["cache_message"] = alpaca_cache_message(age) if cached else None
    return out


def reset_alpaca_health_cache() -> None:
    with ALPACA_HEALTH_LOCK:
        ALPACA_HEALTH_CACHE["result"] = None
        ALPACA_HEALTH_CACHE["checked_monotonic"] = 0.0


def alpaca_health_check(force_refresh: bool = False) -> Dict[str, Any]:
    with ALPACA_HEALTH_LOCK:
        cached_result = ALPACA_HEALTH_CACHE.get("result")
        checked_monotonic = float(ALPACA_HEALTH_CACHE.get("checked_monotonic") or 0.0)
        cache_age = int(time.monotonic() - checked_monotonic) if checked_monotonic else 0
        if cached_result and not force_refresh and cache_age < ALPACA_HEALTH_CACHE_SECONDS:
            return apply_alpaca_cache_fields(cached_result, True, cache_age)

        result = build_alpaca_health_check()
        ALPACA_HEALTH_CACHE["result"] = result
        ALPACA_HEALTH_CACHE["checked_monotonic"] = time.monotonic()
        return apply_alpaca_cache_fields(result, False, 0)


def build_alpaca_health_check() -> Dict[str, Any]:
    cli_path = alpaca_cli_path()
    last_checked = local_iso_now()
    live_mode = is_live_trade_mode()
    mode = "LIVE" if live_mode else "PAPER"
    warnings: List[str] = []
    if live_mode:
        warnings.append("WARNING: Alpaca CLI appears configured for LIVE trading.")
    if not cli_path:
        return {
            "last_checked": last_checked,
            "checked_at": last_checked,
            "cli": {"installed": False, "path": None},
            "summary": {
                "alpaca_cli": "ERROR",
                "mode": mode,
                "account": "UNKNOWN",
                "market": "UNKNOWN",
                "next_open": None,
                "buying_power": None,
                "portfolio_value": None,
                "positions": None,
                "errors": "Alpaca CLI not found. Install it or set ALPACA_CLI_PATH.",
            },
            "status": "error",
            "error_type": "missing_cli",
            "ok": False,
            "connection_status": "CLI not found",
            "mode": mode,
            "warnings": warnings,
            "market": None,
            "account": None,
            "commands": {},
            "error": "Alpaca CLI not found. Install it or set ALPACA_CLI_PATH.",
            "user_friendly_message": "Alpaca CLI not found. Install it or set ALPACA_CLI_PATH.",
        }

    doctor = run_alpaca_cli(cli_path, "doctor", ["doctor"], timeout=10)
    clock = run_alpaca_cli(cli_path, "clock", ["clock", "--jq", "."], timeout=8, expect_json=True)
    account = run_alpaca_cli(
        cli_path,
        "account",
        [
            "account",
            "get",
            "--jq",
            "{status: .status, currency: .currency, buying_power: .buying_power, portfolio_value: .portfolio_value, positions: .positions, positions_count: .positions_count, trading_blocked: .trading_blocked, transfers_blocked: .transfers_blocked, account_blocked: .account_blocked}",
        ],
        timeout=8,
        expect_json=True,
    )
    commands = {"doctor": doctor, "clock": clock, "account": account}
    clock_data = clock.get("parsed_json") if clock["status"] == "ok" else None
    account_data = account.get("parsed_json") if account["status"] == "ok" else None
    command_errors = [result for result in commands.values() if result["status"] != "ok"]
    error_parts = [f"{result['command_name']}: {result['user_friendly_message']}" for result in command_errors]

    account_status = str((account_data or {}).get("status") or "UNKNOWN")
    if account_data and account_status.upper() != "ACTIVE":
        warnings.append(f"Alpaca account status is {account_status}.")
    market_open = (clock_data or {}).get("is_open")
    next_open = (clock_data or {}).get("next_open")
    positions = (account_data or {}).get("positions_count")
    if positions is None and isinstance((account_data or {}).get("positions"), list):
        positions = len((account_data or {}).get("positions") or [])
    if positions is None:
        positions = (account_data or {}).get("positions")

    ok = bool(not command_errors)
    status = "ok" if ok else "error"
    error_type = command_errors[0]["error_type"] if command_errors else None
    summary = {
        "alpaca_cli": "OK" if cli_path and doctor["status"] == "ok" else "ERROR",
        "mode": mode,
        "account": account_status if account["status"] == "ok" else "ERROR",
        "market": "OPEN" if market_open is True else "CLOSED" if market_open is False else "UNKNOWN",
        "next_open": next_open,
        "buying_power": (account_data or {}).get("buying_power"),
        "portfolio_value": (account_data or {}).get("portfolio_value"),
        "positions": positions,
        "errors": "; ".join(error_parts) if error_parts else "None",
    }
    return {
        "last_checked": last_checked,
        "checked_at": last_checked,
        "cli": {"installed": True, "path": cli_path},
        "env_sanitized": True,
        "status": status,
        "error_type": error_type,
        "ok": ok,
        "connection_status": "ok" if ok else "error",
        "mode": mode,
        "warnings": warnings,
        "summary": summary,
        "market": clock_data,
        "account": account_data,
        "commands": commands,
        "error": "; ".join(error_parts) if error_parts else None,
        "user_friendly_message": "; ".join(error_parts) if error_parts else None,
    }


def openai_cache_message(age: int) -> str:
    return f"Cached AI review - checked {age} seconds ago"


def apply_openai_cache_fields(result: Dict[str, Any], cached: bool, age: int = 0) -> Dict[str, Any]:
    out = json.loads(json.dumps(result))
    out["cached"] = cached
    out["cache_age_seconds"] = age
    out["cache_message"] = openai_cache_message(age) if cached else None
    return out


def reset_openai_analysis_cache() -> None:
    with OPENAI_ANALYSIS_LOCK:
        OPENAI_ANALYSIS_CACHE["result"] = None
        OPENAI_ANALYSIS_CACHE["checked_monotonic"] = 0.0


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int, minimum: int = 1, maximum: int = 1000) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def ai_review_enabled() -> bool:
    return env_flag("ENABLE_AI_REVIEW", True)


def ai_review_timeout_seconds() -> int:
    return env_int("AI_REVIEW_TIMEOUT_SECONDS", 8, minimum=1, maximum=60)


def ai_review_max_alerts() -> int:
    return env_int("AI_REVIEW_MAX_ALERTS", 10, minimum=0, maximum=100)


def ai_review_max_watchlist_rows() -> int:
    return env_int("AI_REVIEW_MAX_WATCHLIST_ROWS", 20, minimum=0, maximum=200)


def openai_model_name() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"


def compact_option_for_ai(option: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    option = option or {}
    return {
        "quality": option.get("quality"),
        "score": option.get("score"),
        "feed": option.get("feed"),
        "spread_pct": option.get("spread_pct"),
        "volume": option.get("volume"),
        "open_interest": option.get("open_interest"),
        "reasons": option.get("reasons") or [],
    }


def compact_symbol_for_ai(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "price": row.get("price"),
        "bar_time": row.get("bar_time"),
        "bar_age_minutes": row.get("bar_age_minutes"),
        "data_quality": row.get("data_quality"),
        "fast_move_pct": row.get("fast_move_pct"),
        "day_move_pct": row.get("day_move_pct"),
        "recent_rvol": row.get("recent_rvol") or row.get("relative_volume"),
        "premarket_high": row.get("premarket_high"),
        "premarket_low": row.get("premarket_low"),
        "opening_range_high": row.get("opening_range_high"),
        "opening_range_low": row.get("opening_range_low"),
        "flags": row.get("flags") or [],
        "score": row.get("score"),
        "options_score": row.get("options_score"),
        "best_call": compact_option_for_ai(row.get("best_call")),
        "best_put": compact_option_for_ai(row.get("best_put")),
    }


def compact_alert_for_ai(alert: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "timestamp": alert.get("timestamp"),
        "symbol": alert.get("symbol"),
        "category": alert.get("category"),
        "direction": alert.get("direction"),
        "price": alert.get("price"),
        "key_level": alert.get("key_level"),
        "fast_move_pct": alert.get("fast_move_pct"),
        "day_move_pct": alert.get("day_move_pct"),
        "relative_volume": alert.get("relative_volume"),
        "option_quality": alert.get("option_quality"),
        "option_feed": alert.get("option_feed"),
        "option_spread_pct": alert.get("option_spread_pct"),
        "alert_grade": alert.get("alert_grade"),
        "sms_allowed": alert.get("sms_allowed"),
        "watch_allowed": alert.get("watch_allowed"),
        "reason": alert.get("reason"),
        "sms_block_reason": alert.get("sms_block_reason"),
    }


def build_ai_review_payload(request_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    request_body = request_body or {}
    status = STATE.snapshot()
    symbols = request_body.get("watchlist")
    if not isinstance(symbols, list):
        symbols = [compact_symbol_for_ai(row) for row in load_symbol_rows()]
    else:
        symbols = symbols[: ai_review_max_watchlist_rows()]
    alerts = request_body.get("recent_alerts")
    if not isinstance(alerts, list):
        alerts = [compact_alert_for_ai(alert) for alert in load_alerts(limit=ai_review_max_alerts())]
    else:
        alerts = alerts[: ai_review_max_alerts()]
    scanner_status = request_body.get("scanner_status")
    if not isinstance(scanner_status, dict):
        scanner_status = {
            "running": status.get("running"),
            "mode": status.get("mode"),
            "scope": status.get("scope"),
            "last_scan_at": status.get("last_scan_at"),
            "last_error": status.get("last_error"),
            "scan_count": status.get("scan_count"),
            "symbols": status.get("symbols"),
            "interval_seconds": status.get("interval"),
        }
    return {
        "generated_at": local_iso_now(),
        "scanner_status": scanner_status,
        "watchlist": symbols,
        "recent_alerts": alerts,
        "timing_data": request_body.get("timing_data") if isinstance(request_body.get("timing_data"), dict) else {},
        "market_context": request_body.get("market_context") if isinstance(request_body.get("market_context"), dict) else {},
        "rules": {
            "do_not_give_financial_advice": True,
            "confirm_in_webull": True,
            "diagnostic_only": True,
            "openai_does_not_control_alerts": True,
            "judge_signal_at_detection_time_not_later_outcome": True,
        },
    }


def build_openai_analysis_payload() -> Dict[str, Any]:
    return build_ai_review_payload()


def extract_openai_output_text(data: Dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    texts: List[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts).strip()


def parse_openai_analysis_text(text: str) -> tuple[Optional[Dict[str, Any]], str]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.startswith("json"):
            clean = clean[4:].strip()
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        return None, truncate_text(clean, 3000)
    return parsed if isinstance(parsed, dict) else None, truncate_text(clean, 3000)


AI_REVIEW_ENUMS = {
    "timing": {"Good", "Slightly Late", "Late", "Too Early", "Unknown"},
    "direction_label": {"Correct", "Mostly Correct", "Mixed", "Wrong", "Unknown"},
    "missed_setup": {"Yes", "No", "Possible", "Unknown"},
    "rule_strictness": {"Too Strict", "Balanced", "Too Loose", "Unknown"},
    "risk_level": {"Low", "Medium", "High", "Unknown"},
}


def validate_ai_review(parsed: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None
    out: Dict[str, Any] = {}
    for field, allowed in AI_REVIEW_ENUMS.items():
        value = parsed.get(field)
        if value not in allowed:
            return None
        out[field] = value
    for field in ("suggested_tuning", "plain_english_summary", "what_to_watch_next"):
        value = parsed.get(field)
        if not isinstance(value, str):
            return None
        out[field] = truncate_text(value, 500)
    chase = parsed.get("do_not_chase_warning")
    if not isinstance(chase, dict) or not isinstance(chase.get("warning"), bool):
        return None
    reason = chase.get("reason")
    if not isinstance(reason, str):
        return None
    out["do_not_chase_warning"] = {
        "warning": bool(chase["warning"]),
        "reason": truncate_text(reason, 300),
    }
    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        return None
    out["confidence"] = int(round(max(0.0, min(100.0, confidence))))
    return out


def ai_review_error(error: str, error_type: str, last_checked: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "error_type": error_type,
        "last_checked": last_checked or local_iso_now(),
        "model": model or openai_model_name(),
        "ai_review": None,
        "analysis": None,
        "error": error,
        "user_friendly_message": error,
        "disclaimer": AI_REVIEW_DISCLAIMER,
    }


def call_openai_analysis_api(api_key: str, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    system_prompt = (
        "You are a diagnostic reviewer for a stock scanner used by an options trader. "
        "Do not give buy/sell advice. Do not recommend a trade. "
        "Review only scanner timing, direction labels, alert quality, missed watch/alert concerns, and rule tuning. "
        "The scanner remains the engine. You cannot place trades, send alerts, change scanner logic, or replace Alpaca data. "
        "Judge whether the scanner signal made sense at detection time, not whether the trade later won or lost."
    )
    user_prompt = (
        "Review this scanner snapshot and return JSON only. Use exactly this schema: "
        "{\"timing\":\"Good | Slightly Late | Late | Too Early | Unknown\","
        "\"direction_label\":\"Correct | Mostly Correct | Mixed | Wrong | Unknown\","
        "\"missed_setup\":\"Yes | No | Possible | Unknown\","
        "\"rule_strictness\":\"Too Strict | Balanced | Too Loose | Unknown\","
        "\"risk_level\":\"Low | Medium | High | Unknown\","
        "\"suggested_tuning\":\"string\","
        "\"plain_english_summary\":\"string\","
        "\"what_to_watch_next\":\"string\","
        "\"do_not_chase_warning\":{\"warning\":true,\"reason\":\"string\"},"
        "\"confidence\":0}. "
        "Confidence must be 0 to 100. The scanner interval is reported in seconds, not minutes. "
        "Evaluate timing, bullish/bearish label quality, extension/chase risk, RVOL support, fast move support, option quality, strictness, and missed obvious warnings or setups. "
        "Keep text trader-friendly and practical. Do not suggest auto-trading.\n\n"
        + json.dumps(payload, separators=(",", ":"), default=str)
    )
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "max_output_tokens": 900,
            "text": {"format": {"type": "json_object"}},
        },
        timeout=ai_review_timeout_seconds(),
    )
    response.raise_for_status()
    data = response.json()
    text = extract_openai_output_text(data)
    parsed, raw = parse_openai_analysis_text(text)
    return {
        "response_id": data.get("id"),
        "model": model,
        "parsed": validate_ai_review(parsed),
        "raw_output": raw,
        "status": data.get("status"),
    }


def build_ai_review(request_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    last_checked = local_iso_now()
    model = openai_model_name()
    if not ai_review_enabled():
        return ai_review_error(AI_REVIEW_ERROR_DISABLED, "disabled", last_checked, model)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return ai_review_error(AI_REVIEW_ERROR_MISSING_KEY, "missing_key", last_checked, model)

    payload = build_ai_review_payload(request_body)
    try:
        result = call_openai_analysis_api(api_key, model, payload)
    except requests.Timeout:
        return ai_review_error(AI_REVIEW_ERROR_TIMEOUT, "timeout", last_checked, model)
    except requests.RequestException as exc:
        logger.info("AI Review request failed: %s", truncate_text(str(exc), 300))
        return ai_review_error(AI_REVIEW_ERROR_FAILED, "request_error", last_checked, model)
    except Exception as exc:
        logger.info("AI Review failed: %s", truncate_text(str(exc), 300))
        return ai_review_error(AI_REVIEW_ERROR_FAILED, "analysis_error", last_checked, model)

    review = validate_ai_review(result.get("parsed"))
    if not isinstance(review, dict):
        return ai_review_error(AI_REVIEW_ERROR_INVALID_JSON, "invalid_json", last_checked, model)
    return {
        "ok": True,
        "status": "ok",
        "error_type": None,
        "last_checked": last_checked,
        "model": model,
        "ai_review": review,
        "analysis": review,
        "summary": {
            "status": "OK",
            "errors": "None",
            "headline": review.get("plain_english_summary") or "AI diagnostic review completed.",
        },
        "raw_output": result.get("raw_output"),
        "response_id": result.get("response_id"),
        "user_friendly_message": "OpenAI diagnostic review completed. Live alert logic was not changed.",
        "disclaimer": AI_REVIEW_DISCLAIMER,
    }


def ai_review(request_body: Optional[Dict[str, Any]] = None, force_refresh: bool = False) -> Dict[str, Any]:
    with OPENAI_ANALYSIS_LOCK:
        cached_result = OPENAI_ANALYSIS_CACHE.get("result")
        checked_monotonic = float(OPENAI_ANALYSIS_CACHE.get("checked_monotonic") or 0.0)
        cache_age = int(time.monotonic() - checked_monotonic) if checked_monotonic else 0
        if not request_body and cached_result and not force_refresh and cache_age < OPENAI_ANALYSIS_CACHE_SECONDS:
            return apply_openai_cache_fields(cached_result, True, cache_age)

        result = build_ai_review(request_body)
        if not request_body:
            OPENAI_ANALYSIS_CACHE["result"] = result
            OPENAI_ANALYSIS_CACHE["checked_monotonic"] = time.monotonic()
        return apply_openai_cache_fields(result, False, 0)


def openai_analysis(force_refresh: bool = False) -> Dict[str, Any]:
    return ai_review(force_refresh=force_refresh)


def score_symbol(app: scanner_app.EliteScanner, snap: scanner_app.SymbolSnapshot) -> int:
    quality = scanner_app.snapshot_data_quality(snap, app.config)
    if quality in {"Stale", "Incomplete"}:
        return 0
    config = app.config
    bars = snap.recent_bars
    lookback = int(config["lookback_minutes_fast_move"])
    score = 0.0

    if len(bars) >= lookback + 1:
        fast_move = abs(scanner_app.pct_change(snap.latest_bar.c, bars[-(lookback + 1)].c))
        threshold = max(float(config["fast_move_pct_threshold"]), 0.01)
        score += min(30.0, (fast_move / threshold) * 30.0)
        anchor_bar = scanner_app.session_anchor_bar(bars, config)
        if anchor_bar:
            day_move = abs(scanner_app.pct_change(snap.latest_bar.c, anchor_bar.o))
            day_threshold = max(float(config["day_move_pct_threshold"]), 0.01)
            score += min(20.0, (day_move / day_threshold) * 20.0)

    rel_vol = app.compute_relative_volume(bars)
    if rel_vol is not None:
        rv_threshold = max(float(config["relative_volume_threshold"]), 0.01)
        score += min(30.0, (rel_vol / rv_threshold) * 30.0)

    price = snap.latest_bar.c
    buffer_pct = float(config["opening_range_break_buffer_pct"]) / 100.0
    if snap.premarket_high is not None and price > snap.premarket_high:
        score += 5.0
    if snap.premarket_low is not None and price < snap.premarket_low:
        score += 5.0
    if snap.opening_range_high is not None and price > snap.opening_range_high * (1 + buffer_pct):
        score += 5.0
    if snap.opening_range_low is not None and price < snap.opening_range_low * (1 - buffer_pct):
        score += 5.0

    return int(round(max(0.0, min(100.0, score))))


def option_selection_to_dict(selection: scanner_app.OptionSelection) -> Dict[str, Any]:
    contract = selection.contract
    if not contract:
        return {
            "quality": selection.quality,
            "score": selection.score,
            "reasons": selection.reasons,
            **selection.details,
        }
    return {
        "symbol": contract.symbol,
        "type": "CALL" if contract.option_type == "C" else "PUT",
        "expiration": contract.expiration_date.isoformat(),
        "strike": contract.strike,
        "bid": contract.bid,
        "ask": contract.ask,
        "mid": contract.mid,
        "spread_pct": contract.spread_pct,
        "delta": contract.delta,
        "iv": contract.implied_volatility,
        "volume": contract.volume,
        "open_interest": contract.open_interest,
        "quality": selection.quality,
        "message": selection.details.get("message"),
        "score": selection.score,
        "reasons": selection.reasons,
        "quote_age_seconds": selection.details.get("quote_age_seconds"),
        "timestamp_source_field": selection.details.get("timestamp_source_field"),
        "days_to_expiration": selection.details.get("days_to_expiration"),
        "is_0dte": selection.details.get("is_0dte"),
        "strike_distance_pct": selection.details.get("strike_distance_pct"),
        "liquidity_state": selection.details.get("liquidity_state"),
        "time_state": selection.details.get("time_state"),
        "trade_ready_allowed": selection.details.get("trade_ready_allowed", selection.is_tradable()),
        "stock_only_allowed": selection.details.get("stock_only_allowed", True),
        "simulated": contract.is_simulated,
        "feed": "simulated" if contract.is_simulated else contract.feed,
    }


def options_score_for_row(snap: scanner_app.SymbolSnapshot, fast_move: Optional[float], day_move: Optional[float]) -> int:
    bullish_weight = max(fast_move or 0, day_move or 0, 0)
    bearish_weight = abs(min(fast_move or 0, day_move or 0, 0))
    preferred = snap.best_call if bullish_weight >= bearish_weight else snap.best_put
    return preferred.score


def option_feed_status_for_snapshot(snap: scanner_app.SymbolSnapshot) -> str:
    feeds: List[str] = []
    for selection in (snap.best_call, snap.best_put):
        contract = selection.contract
        if not contract:
            continue
        if contract.is_simulated:
            return "SIMULATED"
        if contract.feed == "opra":
            feeds.append("OPRA")
        elif contract.feed == "indicative":
            feeds.append("INDICATIVE")
    if not feeds:
        return "UNAVAILABLE"
    if "INDICATIVE" in feeds:
        return "INDICATIVE"
    return "OPRA"


def build_symbol_rows(
    app: scanner_app.EliteScanner,
    snapshots: Dict[str, scanner_app.SymbolSnapshot],
    source_map: Dict[str, str],
    market_context: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    lookback = int(app.config["lookback_minutes_fast_move"])
    for symbol in app.symbols:
        snap = snapshots.get(symbol)
        if not snap:
            continue
        latest = snap.latest_bar
        bars = snap.recent_bars
        fast_move = None
        day_move = None
        if latest and len(bars) >= lookback + 1:
            fast_move = scanner_app.pct_change(latest.c, bars[-(lookback + 1)].c)
            anchor_bar = scanner_app.session_anchor_bar(bars, app.config)
            if anchor_bar:
                day_move = scanner_app.pct_change(latest.c, anchor_bar.o)
        rel_vol = app.compute_relative_volume(bars)
        quality = scanner_app.snapshot_data_quality(snap, app.config)
        age_minutes = scanner_app.bar_age_minutes(latest)
        direction = "BULLISH" if (fast_move or day_move or 0) > 0 else "BEARISH" if (fast_move or day_move or 0) < 0 else "NEUTRAL"
        option_feed_status = option_feed_status_for_snapshot(snap)
        preferred_option = snap.best_call if direction != "BEARISH" else snap.best_put
        preferred_option_details = preferred_option.details or {}
        strategy_summary: Dict[str, Any] = {}
        if latest and bars and app.config.get("strategy_engine", {}).get("enabled", True):
            market_bars = {
                symbol: market_snap.recent_bars
                for symbol, market_snap in snapshots.items()
                if symbol in {"SPY", "QQQ"} and market_snap.recent_bars
            }
            strategy_summary = scanner_app.evaluate_strategy_suite(
                snap.symbol,
                bars,
                latest,
                app.config,
                app.strategy_levels_for_snapshot(snap),
                rel_vol,
                app.market_alignment_for(direction, market_context),
                market_bars,
                option_context={
                    "option_feed_status": option_feed_status,
                    "option_tradability_score": options_score_for_row(snap, fast_move, day_move),
                    "option_tradable": snap.best_call.is_tradable() or snap.best_put.is_tradable(),
                },
            )
        flags: List[str] = []
        if snap.latest_news:
            flags.append("News")
        if quality != "Fresh":
            flags.append(quality)
        if latest and snap.premarket_high is not None and latest.c > snap.premarket_high:
            flags.append("PM high")
        if latest and snap.premarket_low is not None and latest.c < snap.premarket_low:
            flags.append("PM low")
        if latest and snap.opening_range_high is not None and latest.c > snap.opening_range_high:
            flags.append("OR high")
        if latest and snap.opening_range_low is not None and latest.c < snap.opening_range_low:
            flags.append("OR low")
        heads_up_alert = None
        if latest and strategy_summary.get("scenario_top"):
            heads_up_alert = scanner_app.Alert(
                symbol=snap.symbol,
                timestamp=scanner_app.now_utc(),
                category="DASHBOARD PHASE 3 HEADS-UP CHECK",
                price=latest.c,
                direction=(strategy_summary.get("scenario_direction") or strategy_summary.get("direction") or "").upper(),
                relative_volume=rel_vol,
                primary_setup=strategy_summary.get("primary_setup"),
                strategy_direction=strategy_summary.get("direction"),
                strategy_confidence_score=strategy_summary.get("confidence_score"),
                risk_label=strategy_summary.get("risk_label"),
                confirmation_score=strategy_summary.get("confirmation_score"),
                entry_quality_label=strategy_summary.get("entry_quality_label"),
                extension_label=strategy_summary.get("extension_label"),
                scenario_top=strategy_summary.get("scenario_top"),
                scenario_score=strategy_summary.get("scenario_score"),
                scenario_stage=strategy_summary.get("scenario_stage"),
                scenario_direction=strategy_summary.get("scenario_direction"),
                scenario_risk_label=strategy_summary.get("scenario_risk_label"),
                scenario_reasons=list(strategy_summary.get("scenario_reasons") or []),
                scenario_warnings=list(strategy_summary.get("scenario_warnings") or []),
                scenario_conflict=strategy_summary.get("scenario_conflict"),
                stock_setup_score=strategy_summary.get("stock_setup_score"),
                option_feed_status=option_feed_status,
                strategy_results=list(strategy_summary.get("strategy_results") or []),
            )
            app.evaluate_phase3_heads_up(heads_up_alert, snap, quality, market_context)
            scanner_app.apply_risk_invalidation(heads_up_alert)
            scanner_app.assign_professional_alert_tier(heads_up_alert)
        rows.append({
            "symbol": snap.symbol,
            "source": source_map.get(snap.symbol, "Discovery"),
            "price": latest.c if latest else None,
            "stock_setup_score": strategy_summary.get("stock_setup_score", strategy_summary.get("strategy_confidence_score")),
            "scenario_score": strategy_summary.get("scenario_score"),
            "scenario_stage": strategy_summary.get("scenario_stage"),
            "scenario_direction": strategy_summary.get("scenario_direction"),
            "scenario_confidence_label": strategy_summary.get("scenario_confidence_label"),
            "scenario_entry_quality_label": strategy_summary.get("scenario_entry_quality_label"),
            "scenario_risk_label": strategy_summary.get("scenario_risk_label"),
            "scenario_alert_tier": strategy_summary.get("scenario_alert_tier"),
            "alert_tier": heads_up_alert.alert_tier if heads_up_alert else None,
            "alert_tier_reason": heads_up_alert.alert_tier_reason if heads_up_alert else None,
            "phone_conclusion": heads_up_alert.phone_conclusion if heads_up_alert else None,
            "phone_conclusion_reason": heads_up_alert.phone_conclusion_reason if heads_up_alert else None,
            "alert_decision_label": heads_up_alert.alert_decision_label if heads_up_alert else None,
            "mixed_signal_no_trade": heads_up_alert.mixed_signal_no_trade if heads_up_alert else None,
            "alert_source": heads_up_alert.alert_source if heads_up_alert else None,
            "message_source_path": heads_up_alert.message_source_path if heads_up_alert else None,
            "invalidation_level": heads_up_alert.invalidation_level if heads_up_alert else None,
            "invalidation_reason": heads_up_alert.invalidation_reason if heads_up_alert else None,
            "stop_logic_description": heads_up_alert.stop_logic_description if heads_up_alert else None,
            "pullback_required": heads_up_alert.pullback_required if heads_up_alert else None,
            "do_not_chase_warning": heads_up_alert.do_not_chase_warning if heads_up_alert else None,
            "entry_timing_label": heads_up_alert.entry_timing_label if heads_up_alert else None,
            "scenario_alert_block_reason": strategy_summary.get("scenario_alert_block_reason"),
            "scenario_reasons": strategy_summary.get("scenario_reasons", []),
            "scenario_warnings": strategy_summary.get("scenario_warnings", []),
            "scenario_top": strategy_summary.get("scenario_top"),
            "scenario_second": strategy_summary.get("scenario_second"),
            "scenario_conflict": strategy_summary.get("scenario_conflict"),
            "bar_time": latest.t.isoformat() if latest else None,
            "bar_age_minutes": age_minutes,
            "data_quality": quality,
            "is_stale": quality == "Stale",
            "fast_move_pct": fast_move,
            "day_move_pct": day_move,
            "vwap": strategy_summary.get("vwap"),
            "ema9": strategy_summary.get("ema9"),
            "ema20": strategy_summary.get("ema20"),
            "recent_rvol": rel_vol,
            "relative_volume": rel_vol,
            "recent_volume": sum(b.v for b in bars),
            "premarket_high": snap.premarket_high,
            "premarket_low": snap.premarket_low,
            "opening_range_high": snap.opening_range_high,
            "opening_range_low": snap.opening_range_low,
            "opening_range_15_high": snap.opening_range_15_high,
            "opening_range_15_low": snap.opening_range_15_low,
            "headline": snap.latest_news.headline if snap.latest_news else None,
            "url": snap.latest_news.url if snap.latest_news else None,
            "news_context_present": bool(snap.latest_news),
            "latest_headline": snap.latest_news.headline if snap.latest_news else None,
            "news_source": snap.latest_news.source if snap.latest_news else None,
            "news_age_minutes": round(
                max(0.0, (scanner_app.now_utc() - snap.latest_news.published_at).total_seconds() / 60.0),
                2,
            ) if snap.latest_news else None,
            "news_sentiment_guess": scanner_app.news_sentiment_guess(snap.latest_news.headline) if snap.latest_news else None,
            "news_used_for_context_only": bool(snap.latest_news),
            "news_upgraded_alert": False,
            "flags": flags,
            "passes_filters": app.passes_basic_filters(snap),
            "score": score_symbol(app, snap),
            "options_score": options_score_for_row(snap, fast_move, day_move),
            "primary_setup": strategy_summary.get("primary_setup"),
            "secondary_setups": strategy_summary.get("secondary_setups", []),
            "strategy_direction": strategy_summary.get("direction"),
            "strategy_confidence_score": strategy_summary.get("confidence_score"),
            "strategy_confidence_label": strategy_summary.get("confidence_label"),
            "confirmation_score": strategy_summary.get("confirmation_score"),
            "confirmation_label": strategy_summary.get("confirmation_label"),
            "entry_quality_label": strategy_summary.get("entry_quality_label"),
            "volume_label": strategy_summary.get("volume_label"),
            "rvol_detail": strategy_summary.get("rvol"),
            "candle_label": strategy_summary.get("candle_label"),
            "candle_score": strategy_summary.get("candle_score"),
            "extension_label": strategy_summary.get("extension_label"),
            "extension_score": strategy_summary.get("extension_score"),
            "relative_strength_label": strategy_summary.get("relative_strength_label"),
            "relative_strength_score": strategy_summary.get("relative_strength_score"),
            "market_regime": strategy_summary.get("market_regime"),
            "regime_score": strategy_summary.get("regime_score", strategy_summary.get("market_score")),
            "market_score": strategy_summary.get("market_score"),
            "regime_reason": strategy_summary.get("regime_reason"),
            "spy_alignment": strategy_summary.get("spy_alignment"),
            "qqq_alignment": strategy_summary.get("qqq_alignment"),
            "aapl_relative_strength": strategy_summary.get("aapl_relative_strength"),
            "volume_state": strategy_summary.get("volume_state"),
            "volatility_state": strategy_summary.get("volatility_state"),
            "trend_1m": snap.multi_timeframe_context.get("trend_1m"),
            "trend_5m": snap.multi_timeframe_context.get("trend_5m"),
            "trend_15m": snap.multi_timeframe_context.get("trend_15m"),
            "daily_trend": snap.multi_timeframe_context.get("daily_trend"),
            "current_structure_bias": snap.multi_timeframe_context.get("current_bias"),
            "structure_key_warning": snap.multi_timeframe_context.get("key_warning"),
            "nearest_level_name": snap.multi_timeframe_context.get("nearest_level_name"),
            "nearest_level_price": snap.multi_timeframe_context.get("nearest_level_price"),
            "distance_to_key_level_pct": snap.multi_timeframe_context.get("distance_to_key_level_pct"),
            "nearest_support": snap.multi_timeframe_context.get("nearest_support"),
            "nearest_resistance": snap.multi_timeframe_context.get("nearest_resistance"),
            "demand_zones": snap.multi_timeframe_context.get("demand_zones", []),
            "supply_zones": snap.multi_timeframe_context.get("supply_zones", []),
            "liquidity_above_highs": snap.multi_timeframe_context.get("liquidity_above_highs", []),
            "liquidity_below_lows": snap.multi_timeframe_context.get("liquidity_below_lows", []),
            "multi_timeframe_levels": snap.multi_timeframe_context.get("levels", {}),
            "professional_setup": strategy_summary.get("professional_setup", {}),
            "setup_name": strategy_summary.get("setup_name"),
            "setup_code": strategy_summary.get("setup_code"),
            "setup_direction": strategy_summary.get("setup_direction"),
            "setup_stage": strategy_summary.get("setup_stage"),
            "setup_score": strategy_summary.get("setup_score"),
            "setup_confidence": strategy_summary.get("setup_confidence"),
            "setup_reason": strategy_summary.get("setup_reason"),
            "setup_invalidation_level": strategy_summary.get("setup_invalidation_level"),
            "setup_entry_quality": strategy_summary.get("setup_entry_quality"),
            "setup_risk_label": strategy_summary.get("setup_risk_label"),
            "setup_watch_text": strategy_summary.get("setup_watch_text"),
            "setup_block_reason": strategy_summary.get("setup_block_reason"),
            "pressure_label": strategy_summary.get("pressure_label"),
            "pressure_score": strategy_summary.get("pressure_score"),
            "option_feed_status": option_feed_status,
            "option_tradability_score": options_score_for_row(snap, fast_move, day_move),
            "option_tradable": (snap.best_call.is_tradable() or snap.best_put.is_tradable()),
            "option_quality": scanner_app.normalize_option_quality_label(preferred_option.quality),
            "option_quality_message": preferred_option_details.get(
                "message", scanner_app.option_quality_message(preferred_option.quality)
            ),
            "option_quality_reasons": list(preferred_option.reasons),
            "option_bid": preferred_option.contract.bid if preferred_option.contract else None,
            "option_ask": preferred_option.contract.ask if preferred_option.contract else None,
            "option_mid": preferred_option.contract.mid if preferred_option.contract else None,
            "option_spread_pct": preferred_option.contract.spread_pct if preferred_option.contract else None,
            "option_quote_age_seconds": preferred_option_details.get("quote_age_seconds"),
            "option_timestamp_source_field": preferred_option_details.get("timestamp_source_field"),
            "option_expiration": preferred_option.contract.expiration_date.isoformat() if preferred_option.contract else None,
            "option_days_to_expiration": preferred_option_details.get("days_to_expiration"),
            "option_is_0dte": preferred_option_details.get("is_0dte"),
            "option_strike_distance_pct": preferred_option_details.get("strike_distance_pct"),
            "option_liquidity_state": preferred_option_details.get("liquidity_state"),
            "option_time_state": preferred_option_details.get("time_state"),
            "option_stock_only_allowed": preferred_option_details.get("stock_only_allowed", True),
            "sms_block_reason": strategy_summary.get("sms_block_reason"),
            "scenario_alert_eligible": strategy_summary.get("scenario_alert_eligible"),
            "scenario_would_sms": strategy_summary.get("scenario_would_sms"),
            "scenario_sms_block_reason": strategy_summary.get("scenario_sms_block_reason"),
            "phase3_heads_up_eligible": heads_up_alert.phase3_heads_up_eligible if heads_up_alert else None,
            "phase3_heads_up_sent": heads_up_alert.phase3_heads_up_sent if heads_up_alert else None,
            "phase3_heads_up_block_reason": heads_up_alert.phase3_heads_up_block_reason if heads_up_alert else None,
            "phase3_heads_up_type": heads_up_alert.phase3_heads_up_type if heads_up_alert else None,
            "phase3_heads_up_dedupe_key": heads_up_alert.phase3_heads_up_dedupe_key if heads_up_alert else None,
            "phase3_heads_up_message_fingerprint": heads_up_alert.phase3_heads_up_message_fingerprint if heads_up_alert else None,
            "phase3_heads_up_dedupe_blocked": heads_up_alert.phase3_heads_up_dedupe_blocked if heads_up_alert else None,
            "phase3_heads_up_dedupe_reason": heads_up_alert.phase3_heads_up_dedupe_reason if heads_up_alert else None,
            "phase3_heads_up_last_sent_time": heads_up_alert.phase3_heads_up_last_sent_time if heads_up_alert else None,
            "phase3_heads_up_next_eligible_time": heads_up_alert.phase3_heads_up_next_eligible_time if heads_up_alert else None,
            "phase3_heads_up_dedupe_minutes_remaining": heads_up_alert.phase3_heads_up_dedupe_minutes_remaining if heads_up_alert else None,
            "market_confirmation_status": heads_up_alert.market_confirmation_status if heads_up_alert else None,
            "context_symbols_available": heads_up_alert.context_symbols_available if heads_up_alert else [],
            "risk_label": strategy_summary.get("risk_label"),
            "stock_setup_score_reason": strategy_summary.get("stock_setup_score_reason"),
            "strategy_reasons": strategy_summary.get("reasons", []),
            "strategy_warnings": strategy_summary.get("warnings", []),
            "strategy_levels": strategy_summary.get("levels", {}),
            "best_call": option_selection_to_dict(snap.best_call),
            "best_put": option_selection_to_dict(snap.best_put),
        })
    rows.sort(key=lambda item: (item["score"], item["relative_volume"] or 0, abs(item["fast_move_pct"] or 0)), reverse=True)
    return rows


def scan_once(app: scanner_app.EliteScanner, source_map: Dict[str, str]) -> tuple[int, List[Dict[str, Any]]]:
    if not scanner_app.in_extended_or_regular_session(app.config):
        logger.info("Outside scan window. No scan performed.")
        return 0, []
    snapshots = app.build_snapshots()
    try:
        refresh_live_market_structure(snapshots, app.config)
    except Exception as exc:
        logger.warning("Market-structure dashboard refresh failed without affecting scanner alerts: %s", exc)
    market_context = app.build_market_context(snapshots)
    market_bars = {
        symbol: snap.recent_bars
        for symbol, snap in snapshots.items()
        if symbol in {"SPY", "QQQ"} and snap.recent_bars
    }
    rows = build_symbol_rows(app, snapshots, source_map, market_context)
    count = 0
    for symbol in app.symbols:
        snap = snapshots.get(symbol)
        if not snap:
            continue
        for alert in app.evaluate_symbol(snap, market_context, market_bars):
            if app.process_alert(alert):
                count += 1
    return count, rows


def worker_loop(mode: str, scope: str) -> None:
    app = make_scanner(mode)
    interval = int(app.config["scan_interval_seconds"])
    source_map, discovery_count = apply_scan_scope(app, scope)
    logger.info("Dashboard scanner started in %s mode with %s scope", mode, scope)

    while not STATE.stop_event.is_set():
        try:
            count, rows = scan_once(app, source_map)
            with STATE.lock:
                STATE.last_alert_count = count
                STATE.symbol_rows = rows
                STATE.last_symbol_count = len(rows)
                STATE.last_discovery_count = discovery_count
                STATE.last_scan_at = iso_now()
                STATE.last_error = None
                STATE.scan_count += 1
        except Exception as exc:
            logger.exception("Dashboard scan failed: %s", exc)
            with STATE.lock:
                STATE.last_error = str(exc)
                STATE.last_scan_at = iso_now()
        STATE.stop_event.wait(interval)

    with STATE.lock:
        STATE.running = False
    logger.info("Dashboard scanner stopped")


def start_scanner(mode: str, scope: str) -> Dict[str, Any]:
    if mode not in {"live", "dry-run"}:
        raise ValueError("Mode must be live or dry-run.")
    if scope not in {"watchlist", "discovery", "hybrid"}:
        raise ValueError("Scope must be watchlist, discovery, or hybrid.")
    with STATE.lock:
        if STATE.running:
            return STATE.snapshot()
        STATE.running = True
        STATE.mode = mode
        STATE.scope = scope
        STATE.started_at = iso_now()
        STATE.last_error = None
        STATE.stop_event.clear()
        STATE.worker = threading.Thread(target=worker_loop, args=(mode, scope), daemon=True)
        STATE.worker.start()
        return STATE.snapshot()


def stop_scanner() -> Dict[str, Any]:
    with STATE.lock:
        STATE.stop_event.set()
        STATE.running = False
        return STATE.snapshot()


def run_once(mode: str, scope: str) -> Dict[str, Any]:
    app = make_scanner(mode)
    source_map, discovery_count = apply_scan_scope(app, scope)
    count, rows = scan_once(app, source_map)
    with STATE.lock:
        STATE.mode = mode
        STATE.scope = scope
        STATE.last_alert_count = count
        STATE.symbol_rows = rows
        STATE.last_symbol_count = len(rows)
        STATE.last_discovery_count = discovery_count
        STATE.last_scan_at = iso_now()
        STATE.last_error = None
        STATE.scan_count += 1
    return STATE.snapshot()


def clear_dashboard_data() -> Dict[str, Any]:
    config = scanner_app.load_config(STATE.config_path)
    with STATE.lock:
        STATE.stop_event.set()
        STATE.running = False
        STATE.started_at = None
        STATE.last_scan_at = None
        STATE.last_alert_count = 0
        STATE.last_error = None
        STATE.scan_count = 0
        STATE.last_symbol_count = 0
        STATE.last_discovery_count = 0
        STATE.symbol_rows = []
        STATE.market_structure_live = None
        STATE.market_structure_updated_monotonic = 0.0

    csv_path = Path(config["outputs"]["csv_log"])
    jsonl_path = Path(config["outputs"]["jsonl_log"])
    state_path = Path(config["outputs"]["state_file"])

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    if jsonl_path.exists():
        jsonl_path.unlink()
    if csv_path.exists():
        csv_path.unlink()
    scanner_app.AlertWriter(csv_path, jsonl_path)
    state_path.write_text(json.dumps({"last_alert_times": {}}, indent=2), encoding="utf-8")

    worker = STATE.worker
    if worker and worker.is_alive():
        worker.join(timeout=2)

    return STATE.snapshot()


def parse_jsonl_line(line: str) -> Optional[Dict[str, Any]]:
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return None
    return item if isinstance(item, dict) else None


def load_alerts(limit: int = 100) -> List[Dict[str, Any]]:
    config = scanner_app.load_config(STATE.config_path)
    path = Path(config["outputs"]["jsonl_log"])
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in reversed(lines[-limit:]):
        item = parse_jsonl_line(line)
        if item:
            out.append(item)
    return out


def load_symbol_rows() -> List[Dict[str, Any]]:
    with STATE.lock:
        return list(STATE.symbol_rows)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Elite Momentum Scanner</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f8;
      --ink: #182026;
      --muted: #66727d;
      --line: #d9e0e4;
      --panel: #ffffff;
      --teal: #087f8c;
      --teal-strong: #05606a;
      --amber: #b66d00;
      --red: #b3261e;
      --green: #0b7a44;
      --shadow: 0 1px 2px rgba(24, 32, 38, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    .bar, main {
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
    }
    .bar {
      min-height: 68px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.1;
      font-weight: 720;
    }
    .sub {
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
    }
    main {
      padding: 22px 0 32px;
      display: grid;
      grid-template-columns: 340px 1fr;
      gap: 16px;
    }
    section, aside {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    aside {
      padding: 16px;
      align-self: start;
    }
    section {
      min-width: 0;
    }
    .content {
      min-width: 0;
      display: grid;
      gap: 16px;
    }
    .structure-body {
      padding: 14px 16px 18px;
      display: grid;
      gap: 14px;
    }
    .structure-status,
    .structure-summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }
    .structure-status > div,
    .structure-summary > div {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 9px;
      min-width: 0;
    }
    .structure-wide {
      grid-column: 1 / -1;
    }
    .structure-table-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }
    .structure-table {
      border: 1px solid var(--line);
      border-radius: 7px;
      overflow-x: auto;
    }
    .structure-table h3 {
      margin: 0;
      padding: 9px 10px;
      font-size: 13px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }
    .structure-table table {
      min-width: 760px;
      font-size: 12px;
    }
    .structure-table th,
    .structure-table td {
      padding: 7px 8px;
    }
    .copy-levels {
      margin: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfd;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .panel-title {
      margin: 0 0 14px;
      font-size: 15px;
      font-weight: 700;
    }
    .controls {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    button, select {
      min-height: 38px;
      border-radius: 7px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      font: inherit;
    }
    button {
      cursor: pointer;
      font-weight: 650;
    }
    button.primary {
      background: var(--teal);
      border-color: var(--teal);
      color: #fff;
    }
    button.primary:hover { background: var(--teal-strong); }
    button.warn {
      color: var(--red);
      border-color: #efc9c6;
    }
    button.danger {
      background: #fff5f4;
      color: var(--red);
      border-color: #efc9c6;
    }
    button:disabled {
      cursor: default;
      opacity: 0.55;
    }
    select {
      width: 100%;
      padding: 0 10px;
      margin-bottom: 10px;
    }
    .stats {
      margin-top: 16px;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .stat {
      min-height: 70px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
    }
    .value {
      margin-top: 7px;
      font-size: 18px;
      font-weight: 720;
      overflow-wrap: anywhere;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 6px 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--muted);
    }
    .dot.on { background: var(--green); }
    .dot.err { background: var(--red); }
    .symbols {
      margin-top: 16px;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 8px;
      font-size: 12px;
      background: #fbfcfd;
    }
    .table-head {
      min-height: 50px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .table-head h2 {
      margin: 0;
      font-size: 16px;
    }
    .alerts {
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
    }
    .market table {
      min-width: 920px;
    }
    th, td {
      padding: 11px 12px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 680;
      background: #fbfcfd;
    }
    td strong { font-weight: 760; }
    .category {
      display: inline-block;
      border-radius: 999px;
      padding: 4px 7px;
      background: #e8f5f6;
      color: var(--teal-strong);
      font-size: 12px;
      font-weight: 720;
      white-space: nowrap;
    }
    .grade {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 42px;
      min-height: 26px;
      border-radius: 7px;
      padding: 3px 7px;
      font-size: 12px;
      font-weight: 760;
      background: #eef3f5;
      color: var(--muted);
      white-space: nowrap;
    }
    .grade.a-plus,
    .grade.a {
      background: #edf6ee;
      color: var(--green);
    }
    .grade.b {
      background: #fff3df;
      color: var(--amber);
    }
    .grade.c,
    .grade.avoid {
      background: #fff0ee;
      color: var(--red);
    }
    .score {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 42px;
      min-height: 28px;
      border-radius: 7px;
      background: #edf6ee;
      color: var(--green);
      font-weight: 760;
    }
    .score.hot {
      background: #fff3df;
      color: var(--amber);
    }
    .score.very-hot {
      background: #fff0ee;
      color: var(--red);
    }
    .flag {
      display: inline-block;
      margin: 0 4px 4px 0;
      padding: 3px 6px;
      border-radius: 999px;
      background: #eef3f5;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      white-space: nowrap;
    }
    .quality {
      display: inline-block;
      padding: 4px 7px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 720;
      white-space: nowrap;
      background: #edf6ee;
      color: var(--green);
    }
    .quality.stale,
    .quality.incomplete {
      background: #fff0ee;
      color: var(--red);
    }
    .quality.low-volume {
      background: #fff3df;
      color: var(--amber);
    }
    .quality.wide-spread,
    .quality.stale-quote,
    .quality.no-clean-contract {
      background: #fff0ee;
      color: var(--red);
    }
    .options-row td {
      background: #fbfcfd;
      padding-top: 8px;
      padding-bottom: 12px;
    }
    .options-view {
      display: grid;
      grid-template-columns: repeat(2, minmax(260px, 1fr));
      gap: 10px;
    }
    .option-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      min-width: 0;
    }
    .option-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
      font-weight: 720;
    }
    .risk-panel {
      margin: 10px 0;
      padding: 10px 0;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      display: grid;
      gap: 5px;
    }
    .option-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      font-size: 12px;
    }
    .option-grid strong {
      display: block;
      margin-top: 2px;
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .feed-badge {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 999px;
      padding: 3px 7px;
      font-size: 11px;
      font-weight: 760;
      text-transform: uppercase;
      white-space: nowrap;
      background: #eef3f5;
      color: var(--muted);
    }
    .feed-badge.opra {
      background: #edf6ee;
      color: var(--green);
    }
    .feed-badge.indicative {
      background: #fff3df;
      color: var(--amber);
    }
    .feed-badge.simulated {
      background: #eef3f5;
      color: var(--muted);
    }
    .muted { color: var(--muted); }
    .error {
      display: none;
      margin-top: 14px;
      border: 1px solid #efc9c6;
      background: #fff7f6;
      color: var(--red);
      border-radius: 8px;
      padding: 10px;
      overflow-wrap: anywhere;
    }
    .health {
      margin-top: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfd;
    }
    .health.good { border-color: #cce6d7; background: #f3fbf6; }
    .health.bad { border-color: #efc9c6; background: #fff7f6; }
    .health.warn { border-color: #f0d59b; background: #fffaf0; }
    .health-grid {
      display: grid;
      gap: 7px;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .health-warning {
      margin-top: 8px;
      border: 1px solid #efc36b;
      background: #fff4dc;
      color: #8a5a00;
      border-radius: 6px;
      padding: 8px;
      font-weight: 760;
      overflow-wrap: anywhere;
    }
    .cache-note {
      margin-top: 8px;
      color: var(--muted);
      font-size: 11px;
    }
    .health-grid strong {
      color: var(--ink);
      font-weight: 720;
    }
    .health pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 8px 0 0;
      max-height: 150px;
      overflow: auto;
      color: var(--muted);
      font-size: 11px;
    }
    .empty {
      padding: 36px 16px;
      color: var(--muted);
      text-align: center;
    }
    a { color: var(--teal-strong); }
    @media (max-width: 860px) {
      .bar { align-items: flex-start; flex-direction: column; padding: 14px 0; }
      main { grid-template-columns: 1fr; }
      .controls { grid-template-columns: 1fr; }
      .options-view { grid-template-columns: 1fr; }
      .structure-status, .structure-summary { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <div>
        <h1>Elite Momentum Scanner</h1>
        <div class="sub" id="subtitle">Loading</div>
      </div>
      <div class="status-pill"><span class="dot" id="statusDot"></span><span id="statusText">Loading</span></div>
    </div>
  </header>
  <main>
    <aside>
      <h2 class="panel-title">Scanner</h2>
      <select id="mode">
        <option value="live">Live</option>
        <option value="dry-run">Dry Run</option>
      </select>
      <select id="scope">
        <option value="watchlist">Watchlist</option>
        <option value="hybrid">Hybrid</option>
        <option value="discovery">Discovery</option>
      </select>
      <div class="controls">
        <button class="primary" id="startBtn">Start</button>
        <button class="warn" id="stopBtn">Stop</button>
        <button id="scanBtn">Scan Once</button>
        <button id="refreshBtn">Refresh</button>
        <button id="alpacaHealthBtn">Alpaca Health</button>
        <button id="openaiReviewBtn">AI Review</button>
        <button class="danger" id="clearBtn">Clear</button>
      </div>
      <div class="stats">
        <div class="stat"><div class="label">Scans</div><div class="value" id="scanCount">0</div></div>
        <div class="stat"><div class="label">Last Alerts</div><div class="value" id="lastAlerts">0</div></div>
        <div class="stat"><div class="label">Symbols</div><div class="value" id="symbolCount">0</div></div>
        <div class="stat"><div class="label">Discovery</div><div class="value" id="discoveryCount">0</div></div>
        <div class="stat"><div class="label">Top Score</div><div class="value" id="topScore">0</div></div>
        <div class="stat"><div class="label">Interval</div><div class="value" id="interval">--</div></div>
        <div class="stat"><div class="label">Discord</div><div class="value" id="discord">Off</div></div>
        <div class="stat"><div class="label">Computer Alerts</div><div class="value" id="sms">Off</div></div>
        <div class="stat"><div class="label">OpenAI</div><div class="value" id="openai">Off</div></div>
      </div>
      <div class="health" id="marketDataStatus">
        <div class="label">Market Data Status</div>
        <div class="value" id="marketDataStatusValue">Unknown</div>
        <div class="health-grid" id="marketDataStatusDetails"></div>
      </div>
      <div class="health" id="scannerIdentityStatus">
        <div class="label">Official Scanner Profile</div>
        <div class="value" id="scannerIdentityStatusValue">Unknown</div>
        <div class="health-grid" id="scannerIdentityStatusDetails"></div>
      </div>
      <div class="health" id="notificationStatus">
        <div class="label">Notification Status</div>
        <div class="value" id="notificationStatusValue">Unknown</div>
        <div class="health-grid" id="notificationStatusDetails"></div>
      </div>
      <div class="health" id="alpacaHealth">
        <div class="label">Alpaca CLI Health</div>
        <div class="value" id="alpacaHealthStatus">Not checked</div>
        <div class="health-grid" id="alpacaHealthDetails"></div>
      </div>
      <div class="health" id="openaiReview">
        <div class="label">OpenAI Diagnostic Review</div>
        <div class="value" id="openaiReviewStatus">Not checked</div>
        <div class="cache-note">AI Review analyzes scanner output for timing, direction quality, missed context, and possible rule tuning. It does not place trades, send alerts, change scanner logic, or replace Alpaca data.</div>
        <div class="health-grid" id="openaiReviewDetails"></div>
      </div>
      <div class="error" id="errorBox"></div>
      <div class="symbols" id="symbols"></div>
    </aside>
    <div class="content">
      <section>
        <div class="table-head">
          <h2>Live Market Structure</h2>
          <span class="muted" id="marketStructureUpdated">Waiting for data</span>
        </div>
        <div class="structure-body" id="liveMarketStructure"></div>
      </section>
      <section>
        <div class="table-head">
          <h2>Market View</h2>
          <span class="muted" id="lastScan">No scan yet</span>
        </div>
        <div class="alerts market" id="market"></div>
      </section>
      <section>
        <div class="table-head">
          <h2>Alerts</h2>
          <span class="muted" id="alertCount">No alerts</span>
        </div>
        <div class="alerts" id="alerts"></div>
      </section>
    </div>
  </main>
  <script>
    const els = {
      mode: document.getElementById('mode'),
      scope: document.getElementById('scope'),
      start: document.getElementById('startBtn'),
      stop: document.getElementById('stopBtn'),
      scan: document.getElementById('scanBtn'),
      refresh: document.getElementById('refreshBtn'),
      alpacaHealthBtn: document.getElementById('alpacaHealthBtn'),
      openaiReviewBtn: document.getElementById('openaiReviewBtn'),
      alpacaHealth: document.getElementById('alpacaHealth'),
      alpacaHealthStatus: document.getElementById('alpacaHealthStatus'),
      alpacaHealthDetails: document.getElementById('alpacaHealthDetails'),
      openaiReview: document.getElementById('openaiReview'),
      openaiReviewStatus: document.getElementById('openaiReviewStatus'),
      openaiReviewDetails: document.getElementById('openaiReviewDetails'),
      marketDataStatus: document.getElementById('marketDataStatus'),
      marketDataStatusValue: document.getElementById('marketDataStatusValue'),
      marketDataStatusDetails: document.getElementById('marketDataStatusDetails'),
      scannerIdentityStatus: document.getElementById('scannerIdentityStatus'),
      scannerIdentityStatusValue: document.getElementById('scannerIdentityStatusValue'),
      scannerIdentityStatusDetails: document.getElementById('scannerIdentityStatusDetails'),
      notificationStatus: document.getElementById('notificationStatus'),
      notificationStatusValue: document.getElementById('notificationStatusValue'),
      notificationStatusDetails: document.getElementById('notificationStatusDetails'),
      clear: document.getElementById('clearBtn'),
      dot: document.getElementById('statusDot'),
      status: document.getElementById('statusText'),
      subtitle: document.getElementById('subtitle'),
      scanCount: document.getElementById('scanCount'),
      lastAlerts: document.getElementById('lastAlerts'),
      symbolCount: document.getElementById('symbolCount'),
      discoveryCount: document.getElementById('discoveryCount'),
      topScore: document.getElementById('topScore'),
      interval: document.getElementById('interval'),
      discord: document.getElementById('discord'),
      sms: document.getElementById('sms'),
      openai: document.getElementById('openai'),
      error: document.getElementById('errorBox'),
      symbols: document.getElementById('symbols'),
      lastScan: document.getElementById('lastScan'),
      alertCount: document.getElementById('alertCount'),
      market: document.getElementById('market'),
      alerts: document.getElementById('alerts'),
      liveMarketStructure: document.getElementById('liveMarketStructure'),
      marketStructureUpdated: document.getElementById('marketStructureUpdated'),
    };

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    function fmtTime(value) {
      if (!value) return 'No scan yet';
      return new Date(value).toLocaleString();
    }

    function fmtPct(value) {
      if (value === null || value === undefined) return '';
      const sign = Number(value) > 0 ? '+' : '';
      return `${sign}${Number(value).toFixed(2)}%`;
    }

    function fmtNum(value, suffix = '') {
      if (value === null || value === undefined) return '';
      return `${Number(value).toFixed(2)}${suffix}`;
    }

    function fmtMoney(value) {
      if (value === null || value === undefined) return '';
      return `$${Number(value).toFixed(2)}`;
    }

    function fmtIv(value) {
      if (value === null || value === undefined) return '';
      return `${(Number(value) * 100).toFixed(0)}%`;
    }

    function fmtInt(value) {
      if (value === null || value === undefined) return '';
      return Number(value).toLocaleString();
    }

    function fmtAge(value) {
      if (value === null || value === undefined) return '';
      if (Number(value) < 1) return '<1m';
      return `${Math.round(Number(value))}m`;
    }

    function esc(value) {
      return String(value ?? '').replace(/[&<>"']/g, (char) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;',
      }[char]));
    }

    function scoreClass(score) {
      if (score >= 75) return 'very-hot';
      if (score >= 45) return 'hot';
      return '';
    }

    function qualityClass(value) {
      return String(value || '').toLowerCase().replace(/\s+/g, '-');
    }

    function gradeClass(value) {
      return String(value || '').toLowerCase().replace('+', '-plus').replace(/\s+/g, '-');
    }

    function feedLabel(feed) {
      const value = String(feed || '').toLowerCase();
      if (value === 'opra') return 'OPRA';
      if (value === 'indicative') return 'Indicative';
      if (value === 'simulated') return 'Simulated';
      return 'Unknown';
    }

    function feedClass(feed) {
      return String(feed || 'unknown').toLowerCase().replace(/\s+/g, '-');
    }

    function renderList(items) {
      const values = (items || []).filter(Boolean);
      if (!values.length) return '<span class="muted">None</span>';
      return `<ul>${values.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>`;
    }

    function structureValue(value) {
      return value === null || value === undefined || value === '' ? 'Not enough clean data yet' : value;
    }

    function structureMoney(value) {
      return value === null || value === undefined ? 'Not enough clean data yet' : Number(value).toFixed(2);
    }

    function renderNearestLevel(label, item, zone = false) {
      if (!item || !Object.keys(item).length) return `${label}: Not enough clean data yet`;
      const price = zone
        ? `${structureMoney(item.zone_low)}-${structureMoney(item.zone_high)}`
        : structureMoney(item.price);
      return `${label}: ${price} | ${structureValue(item.timeframe)} | ${structureValue(item.strength)}`;
    }

    function renderStructureLevelTable(timeframe, record) {
      const support = ((record || {}).levels || {}).support || [];
      const resistance = ((record || {}).levels || {}).resistance || [];
      const rows = [
        ...support.map((item) => ({...item, type: 'Support'})),
        ...resistance.map((item) => ({...item, type: 'Resistance'})),
      ];
      if (!rows.length) {
        return `<div class="empty">No clean ${esc(timeframe)} support or resistance detected yet</div>`;
      }
      return `<table>
        <thead><tr><th>Type</th><th>Price</th><th>Strength</th><th>Score</th><th>Tested</th><th>Fresh</th><th>Source</th><th>Reason</th></tr></thead>
        <tbody>${rows.map((item) => `<tr>
          <td>${esc(item.type)}</td><td>${structureMoney(item.price)}</td><td>${esc(structureValue(item.strength))}</td>
          <td>${esc(structureValue(item.score))}</td><td>${esc(structureValue(item.times_tested))}</td>
          <td>${item.fresh ? 'Fresh' : 'Tested'}</td><td>${esc(structureValue(item.source))}</td><td>${esc(structureValue(item.reason))}</td>
        </tr>`).join('')}</tbody>
      </table>`;
    }

    function renderStructureZoneTable(timeframe, record) {
      const demand = ((record || {}).zones || {}).demand || [];
      const supply = ((record || {}).zones || {}).supply || [];
      const rows = [
        ...demand.map((item) => ({...item, type: 'Demand'})),
        ...supply.map((item) => ({...item, type: 'Supply'})),
      ];
      if (!rows.length) return `<div class="empty">No clean ${esc(timeframe)} zones detected yet</div>`;
      return `<table>
        <thead><tr><th>Type</th><th>Zone</th><th>Strength</th><th>Score</th><th>Tested</th><th>Fresh</th><th>Reaction</th><th>Invalidation</th><th>Reason</th></tr></thead>
        <tbody>${rows.map((item) => `<tr>
          <td>${esc(item.type)}</td><td>${structureMoney(item.zone_low)}-${structureMoney(item.zone_high)}</td>
          <td>${esc(structureValue(item.strength))}</td><td>${esc(structureValue(item.score))}</td>
          <td>${esc(structureValue(item.times_tested))}</td><td>${item.fresh ? 'Fresh' : 'Tested'}</td>
          <td>${esc(structureValue(item.last_reaction))}</td><td>${esc(structureValue(item.invalidation))}</td>
          <td>${esc(structureValue(item.reason))}</td>
        </tr>`).join('')}</tbody>
      </table>`;
    }

    function renderLiveMarketStructure(data) {
      const summary = data.summary || {};
      const nearest = data.nearest || {};
      const sr = data.support_resistance || {};
      const sd = data.supply_demand || {};
      els.marketStructureUpdated.textContent = data.last_updated ? `Last updated ${fmtTime(data.last_updated)}` : 'Waiting for data';
      if (!data.enabled || !data.last_updated) {
        els.liveMarketStructure.innerHTML = `<div class="empty">${esc(data.message || 'Waiting for market structure data.')}</div>`;
        return;
      }
      els.liveMarketStructure.innerHTML = `
        <div class="structure-status">
          <div><span class="label">Market Structure Engine</span><div><strong>${esc(structureValue(data.engine_status))}</strong></div></div>
          <div><span class="label">Support / Resistance</span><div><strong>${esc(structureValue(data.support_resistance_status))}</strong></div></div>
          <div><span class="label">Supply / Demand</span><div><strong>${esc(structureValue(data.supply_demand_status))}</strong></div></div>
          <div><span class="label">Data Mode</span><div><strong>${esc(structureValue(data.data_mode))}</strong></div></div>
        </div>
        <div class="structure-summary">
          <div><span class="label">Current Price</span><div><strong>${structureMoney(summary.current_price)}</strong></div></div>
          <div><span class="label">Structure</span><div><strong>${esc(structureValue(summary.market_structure_bias))}</strong></div></div>
          <div><span class="label">Quality</span><div><strong>${esc(structureValue(summary.structure_quality))}</strong></div></div>
          <div><span class="label">Warning</span><div><strong>${esc(structureValue(summary.structure_warning))}</strong></div></div>
          <div class="structure-wide"><span class="label">Location</span><div><strong>${esc(structureValue(summary.current_price_location_summary))}</strong></div></div>
          <div class="structure-wide"><span class="label">Nearest Levels</span><div>
            ${esc(renderNearestLevel('Support', nearest.support))}<br>
            ${esc(renderNearestLevel('Resistance', nearest.resistance))}<br>
            ${esc(renderNearestLevel('Demand', nearest.demand, true))}<br>
            ${esc(renderNearestLevel('Supply', nearest.supply, true))}
          </div></div>
          <div class="structure-wide"><span class="label">Confluence</span><div><strong>${esc(structureValue(summary.confluence_reason))}</strong></div></div>
        </div>
        <div class="structure-table-grid">
          ${['1m', '5m', '15m'].map((frame) => `<div class="structure-table"><h3>${frame} Support / Resistance</h3>${renderStructureLevelTable(frame, sr[frame])}</div>`).join('')}
          ${['1m', '5m', '15m'].map((frame) => `<div class="structure-table"><h3>${frame} Supply / Demand</h3>${renderStructureZoneTable(frame, sd[frame])}</div>`).join('')}
        </div>
        <div>
          <h3>Copy Levels</h3>
          <pre class="copy-levels">${esc(structureValue(data.copy_summary))}</pre>
        </div>
      `;
    }

    function renderLevels(levels) {
      const entries = Object.entries(levels || {}).filter(([, value]) => Number.isFinite(Number(value)));
      if (!entries.length) return '<span class="muted">No key levels</span>';
      return entries.slice(0, 10).map(([key, value]) => (
        `<span class="flag">${esc(key.replaceAll('_', ' '))}: ${fmtNum(value)}</span>`
      )).join('');
    }

    function riskText(risk) {
      return risk === 'DO_NOT_CHASE' ? 'RISK: DO_NOT_CHASE — valid setup may be late' : (risk || '');
    }

    function renderPhase2Summary(item) {
      const volume = `${item.volume_label || ''}${item.rvol_detail !== undefined && item.rvol_detail !== null ? ` / RVOL ${fmtNum(item.rvol_detail, 'x')}` : ''}`;
      return `
        <div class="muted">
          Phase 2: Vol ${esc(volume)} | Candle ${esc(item.candle_label || '')} | Entry ${esc(item.entry_quality_label || '')} |
          Market ${esc(item.market_regime || '')} | RS ${esc(item.relative_strength_label || '')} |
          Confirm ${item.confirmation_score ?? ''} ${esc(item.confirmation_label || '')} | ${esc(riskText(item.risk_label))}
        </div>
      `;
    }

    function renderPhase3Summary(item) {
      const scenario = item.scenario_top?.scenario_name || item.primary_setup || 'None';
      const stage = item.scenario_stage || item.scenario_top?.stage || '';
      const optionFeed = item.option_feed_status || 'UNAVAILABLE';
      return `
        <div class="muted">
          Phase 3: Scenario ${esc(scenario)} ${esc(stage)} | Stock ${item.stock_setup_score ?? item.strategy_confidence_score ?? ''} |
          Scenario ${item.scenario_score ?? ''} | Option ${item.option_tradability_score ?? ''} ${esc(optionFeed)} |
          Alert Tier ${esc(item.alert_tier || '')} | ${esc(item.alert_tier_reason || '')} |
          Decision ${esc(item.phone_conclusion || '')} | ${esc(item.phone_conclusion_reason || '')} |
          Tier ${esc(item.scenario_alert_tier || '')} | Alert Block ${esc(item.scenario_alert_block_reason || '')} | Eligible ${item.scenario_alert_eligible ? 'Yes' : 'No'} | Would SMS ${item.scenario_would_sms ? 'Yes' : 'No'} |
          Heads-Up ${esc(item.phase3_heads_up_type || 'BLOCKED')} / ${item.phase3_heads_up_eligible ? 'Eligible' : 'No'} / Sent ${item.phase3_heads_up_sent ? 'Yes' : 'No'} |
          ${esc(item.phase3_heads_up_block_reason || '')} |
          Dedupe ${item.phase3_heads_up_dedupe_blocked ? 'Blocked' : 'Clear'} ${item.phase3_heads_up_dedupe_minutes_remaining ?? ''} |
          Market Confirm ${esc(item.market_confirmation_status || 'UNKNOWN')} |
          ${esc(item.scenario_sms_block_reason || item.sms_block_reason || (item.scenario_conflict ? 'Scenario conflict' : ''))}
        </div>
      `;
    }

    function renderMarketRegimePanel(item) {
      return `
        <div class="risk-panel">
          <div class="option-title"><span>Market Regime</span><span>${esc(item.market_regime || 'UNKNOWN')} ${item.regime_score ?? item.market_score ?? ''}</span></div>
          <div><span class="muted">Environment</span> ${esc(item.regime_reason || 'Insufficient evidence for a clear regime')}</div>
          <div><span class="muted">SPY / QQQ alignment</span> ${esc(item.spy_alignment || 'UNKNOWN')} / ${esc(item.qqq_alignment || 'UNKNOWN')}</div>
          <div><span class="muted">AAPL relative strength</span> ${esc(item.aapl_relative_strength || item.relative_strength_label || 'UNKNOWN')}</div>
          <div><span class="muted">Volume / Volatility</span> ${esc(item.volume_state || item.volume_label || 'UNKNOWN')} / ${esc(item.volatility_state || 'UNKNOWN')}</div>
          ${item.market_regime === 'CHOPPY' ? '<div><span class="muted">Action</span> WATCH ONLY — choppy regime downgrades setup quality</div>' : ''}
        </div>
      `;
    }

    function renderMarketStructurePanel(item) {
      return `
        <div class="risk-panel">
          <div class="option-title"><span>Market Structure</span><span>${esc(item.current_structure_bias || 'UNKNOWN')}</span></div>
          <div><span class="muted">1m / 5m / 15m</span> ${esc(item.trend_1m || 'UNKNOWN')} / ${esc(item.trend_5m || 'UNKNOWN')} / ${esc(item.trend_15m || 'UNKNOWN')}</div>
          <div><span class="muted">Nearest level</span> ${esc(item.nearest_level_name || 'Unavailable')} ${item.nearest_level_price !== undefined && item.nearest_level_price !== null ? fmtMoney(item.nearest_level_price) : ''} ${item.distance_to_key_level_pct !== undefined && item.distance_to_key_level_pct !== null ? `(${fmtNum(item.distance_to_key_level_pct, '%')})` : ''}</div>
          <div><span class="muted">Support / Resistance</span> ${item.nearest_support !== undefined && item.nearest_support !== null ? fmtMoney(item.nearest_support) : 'Unavailable'} / ${item.nearest_resistance !== undefined && item.nearest_resistance !== null ? fmtMoney(item.nearest_resistance) : 'Unavailable'}</div>
          <div><span class="muted">Key warning</span> ${esc(item.structure_key_warning || 'Structure aligned')}</div>
        </div>
      `;
    }

    function renderProfessionalSetupPanel(item) {
      return `
        <div class="risk-panel">
          <div class="option-title"><span>Professional Setup</span><span>${esc(item.setup_name || 'Low Quality Setup')}</span></div>
          <div><span class="muted">Direction / Stage</span> ${esc(item.setup_direction || 'neutral')} / ${esc(item.setup_stage || 'WATCHING')}</div>
          <div><span class="muted">Score / Confidence</span> ${item.setup_score ?? 0} / ${esc(item.setup_confidence || 'LOW')}</div>
          <div><span class="muted">Reason</span> ${esc(item.setup_reason || 'No clear setup reason')}</div>
          <div><span class="muted">Entry / Risk</span> ${esc(item.setup_entry_quality || 'UNKNOWN')} / ${esc(item.setup_risk_label || 'MEDIUM')}</div>
          <div><span class="muted">Watch</span> ${esc(item.setup_watch_text || 'Confirm manually on chart.')}</div>
          ${item.setup_block_reason ? `<div><span class="muted">Block</span> ${esc(item.setup_block_reason)}</div>` : ''}
        </div>
      `;
    }

    function renderStrategySummary(item) {
      if (!item?.primary_setup) {
        return `${renderProfessionalSetupPanel(item)}${renderMarketRegimePanel(item)}${renderMarketStructurePanel(item)}${renderOptionQualityPanel(item)}${renderNewsContextPanel(item)}<div class="muted">No active Phase 1 strategy setup.</div>`;
      }
      return `
        <div class="option-card">
          <div class="option-title">
            <span>${esc(item.primary_setup)}</span>
            <span>${esc(item.strategy_confidence_label || '')} ${item.strategy_confidence_score ?? 0} | ${esc(riskText(item.risk_label))}</span>
          </div>
          ${renderPhase2Summary(item)}
          ${renderPhase3Summary(item)}
          ${renderProfessionalSetupPanel(item)}
          ${renderMarketRegimePanel(item)}
          ${renderMarketStructurePanel(item)}
          ${renderOptionQualityPanel(item)}
          ${renderNewsContextPanel(item)}
          <div class="risk-panel">
            <div class="option-title"><span>Risk / Invalidation</span><span>${esc(item.entry_timing_label || item.entry_quality_label || 'EARLY')}</span></div>
            <div><span class="muted">Idea is wrong</span> ${item.invalidation_level !== undefined && item.invalidation_level !== null ? fmtMoney(item.invalidation_level) : 'No clean level — watch only'}</div>
            <div><span class="muted">Reason</span> ${esc(item.invalidation_reason || 'Unavailable')}</div>
            <div><span class="muted">Stop logic</span> ${esc(item.stop_logic_description || 'Unavailable')}</div>
            <div><span class="muted">Pullback required</span> ${item.pullback_required ? 'Yes' : 'No'}</div>
            <div><span class="muted">Do not chase</span> ${item.do_not_chase_warning ? 'Yes' : 'No'}</div>
          </div>
          ${item.secondary_setups?.length ? `<div><span class="muted">Secondary</span> ${item.secondary_setups.map(esc).join(', ')}</div>` : ''}
          <div><span class="muted">Direction</span> ${esc(item.strategy_direction || item.direction || '')}</div>
          <div><span class="muted">Scenario</span> ${esc(item.scenario_top?.scenario_name || '')} ${esc(item.scenario_stage || '')}</div>
          <div><span class="muted">Stock Setup</span> ${item.stock_setup_score ?? item.strategy_confidence_score ?? ''}</div>
          <div><span class="muted">Stock Reason</span> ${esc(item.stock_setup_score_reason || '')}</div>
          <div><span class="muted">Scenario Score</span> ${item.scenario_score ?? ''} ${esc(item.scenario_confidence_label || '')}</div>
          <div><span class="muted">Scenario Tier</span> ${esc(item.scenario_alert_tier || '')}</div>
          <div><span class="muted">Professional Alert Tier</span> ${esc(item.alert_tier || '')}</div>
          <div><span class="muted">Alert Tier Reason</span> ${esc(item.alert_tier_reason || '')}</div>
          <div><span class="muted">Phone Conclusion</span> ${esc(item.phone_conclusion || '')}</div>
          <div><span class="muted">Decision Explanation</span> ${esc(item.phone_conclusion_reason || '')}</div>
          <div><span class="muted">Scenario Eligible</span> ${item.scenario_alert_eligible ? 'Yes' : 'No'}</div>
          <div><span class="muted">Would SMS</span> ${item.scenario_would_sms ? 'Yes' : 'No'}</div>
          <div><span class="muted">Scenario Alert Block</span> ${esc(item.scenario_alert_block_reason || '')}</div>
          <div><span class="muted">Scenario SMS Block</span> ${esc(item.scenario_sms_block_reason || '')}</div>
          <div><span class="muted">Phase 3 Heads-Up Eligible</span> ${item.phase3_heads_up_eligible ? 'Yes' : 'No'}</div>
          <div><span class="muted">Phase 3 Heads-Up Sent</span> ${item.phase3_heads_up_sent ? 'Yes' : 'No'}</div>
          <div><span class="muted">Phase 3 Heads-Up Type</span> ${esc(item.phase3_heads_up_type || 'BLOCKED')}</div>
          <div><span class="muted">Phase 3 Heads-Up Block</span> ${esc(item.phase3_heads_up_block_reason || '')}</div>
          <div><span class="muted">Heads-Up Dedupe</span> ${item.phase3_heads_up_dedupe_blocked ? 'Blocked' : 'Clear'} ${item.phase3_heads_up_dedupe_minutes_remaining ?? ''}</div>
          <div><span class="muted">Last / Next Heads-Up</span> ${esc(item.phase3_heads_up_last_sent_time || '')} / ${esc(item.phase3_heads_up_next_eligible_time || '')}</div>
          <div><span class="muted">Market Confirmation</span> ${esc(item.market_confirmation_status || 'UNKNOWN')} (${esc((item.context_symbols_available || []).join(', ') || 'none')})</div>
          <div><span class="muted">Confirmation</span> ${item.confirmation_score ?? ''} ${esc(item.confirmation_label || '')}</div>
          <div><span class="muted">Entry</span> ${esc(item.entry_quality_label || '')}</div>
          <div><span class="muted">Volume</span> ${esc(item.volume_label || '')} ${item.rvol_detail !== undefined && item.rvol_detail !== null ? `RVOL ${fmtNum(item.rvol_detail, 'x')}` : ''}</div>
          <div><span class="muted">Candle</span> ${esc(item.candle_label || '')} ${item.candle_score ?? ''}</div>
          <div><span class="muted">Extension</span> ${esc(item.extension_label || '')} ${item.extension_score ?? ''}</div>
          <div><span class="muted">Rel Strength</span> ${esc(item.relative_strength_label || '')} ${item.relative_strength_score ?? ''}</div>
          <div><span class="muted">Market</span> ${esc(item.market_regime || '')} ${item.market_score ?? ''}</div>
          <div><span class="muted">Pressure</span> ${esc(item.pressure_label || '')} ${item.pressure_score ?? ''}</div>
          <div><span class="muted">Option Feed</span> ${esc(item.option_feed_status || '')} ${item.option_tradability_score ?? ''}</div>
          <div><span class="muted">SMS Block</span> ${esc(item.sms_block_reason || '')}</div>
          <div><span class="muted">Reasons</span>${renderList(item.strategy_reasons)}</div>
          <div><span class="muted">Warnings</span>${renderList(item.strategy_warnings)}</div>
          <div><span class="muted">Levels</span><div>${renderLevels(item.strategy_levels)}</div></div>
        </div>
      `;
    }

    function renderOptionQualityPanel(item) {
      const quality = item.option_quality || 'UNAVAILABLE';
      const message = item.option_quality_message || (
        quality === 'TRADABLE' ? 'Option tradable' : `${quality} — stock setup only`
      );
      return `
        <div class="risk-panel">
          <div class="option-title"><span>Option Quality</span><span class="quality ${qualityClass(quality)}">${esc(quality)}</span></div>
          <div><span class="muted">Read</span> ${esc(message)}</div>
          <div><span class="muted">Bid / Ask / Mid</span> ${fmtMoney(item.option_bid)} / ${fmtMoney(item.option_ask)} / ${fmtMoney(item.option_mid)}</div>
          <div><span class="muted">Spread / Quote age</span> ${fmtNum(item.option_spread_pct, '%')} / ${item.option_quote_age_seconds !== undefined && item.option_quote_age_seconds !== null ? fmtNum(item.option_quote_age_seconds, 's') : 'Unavailable'}</div>
          <div><span class="muted">Timestamp source</span> ${esc(item.option_timestamp_source_field || 'Unavailable')}</div>
          <div><span class="muted">Expiration / 0DTE / Strike distance</span> ${esc(item.option_expiration || 'Unavailable')} / ${item.option_is_0dte ? 'Yes' : 'No'} / ${item.option_strike_distance_pct !== undefined && item.option_strike_distance_pct !== null ? fmtNum(item.option_strike_distance_pct, '%') : 'Unavailable'}</div>
          <div><span class="muted">Liquidity / Session</span> ${esc(item.option_liquidity_state || 'UNKNOWN')} / ${esc(item.option_time_state || 'UNKNOWN')}</div>
          <div><span class="muted">Trade-ready / Stock-only</span> ${item.option_tradable ? 'Allowed' : 'Blocked'} / ${item.option_stock_only_allowed === false ? 'Blocked' : 'Allowed'}</div>
        </div>
      `;
    }

    function renderNewsContextPanel(item) {
      return `
        <div class="risk-panel">
          <div class="option-title"><span>News Context</span><span>${item.news_context_present ? 'PRESENT' : 'NONE'}</span></div>
          <div><span class="muted">Latest headline</span> ${esc(item.latest_headline || item.headline || 'No fresh AAPL news')}</div>
          <div><span class="muted">Source / Age / Sentiment</span> ${esc(item.news_source || 'Unavailable')} / ${item.news_age_minutes !== undefined && item.news_age_minutes !== null ? fmtNum(item.news_age_minutes, 'm') : 'Unavailable'} / ${esc(item.news_sentiment_guess || 'NEUTRAL')}</div>
          <div><span class="muted">Use</span> Context only — confirm price reaction</div>
          <div><span class="muted">Upgraded alert</span> ${item.news_upgraded_alert ? 'Yes' : 'No'}</div>
        </div>
      `;
    }

    function renderOptionCard(label, option) {
      const quality = option?.quality || 'INVALID';
      if (!option || !option.symbol) {
        return `
          <div class="option-card">
            <div class="option-title"><span>${label}</span><span class="quality ${qualityClass(quality)}">${esc(quality)}</span></div>
            <div class="muted">${esc((option?.reasons || []).join(', ') || 'No contract selected')}</div>
          </div>
        `;
      }
      return `
        <div class="option-card">
          <div class="option-title">
            <span>${label}: ${esc(option.symbol)}</span>
            <span>
              <span class="feed-badge ${feedClass(option.feed)}">${feedLabel(option.feed)}</span>
              <span class="quality ${qualityClass(quality)}">${esc(quality)}</span>
            </span>
          </div>
          <div class="option-grid">
            <div><span class="muted">Exp</span><strong>${esc(option.expiration)}</strong></div>
            <div><span class="muted">Strike</span><strong>${fmtMoney(option.strike)}</strong></div>
            <div><span class="muted">Bid/Ask</span><strong>${fmtMoney(option.bid)} / ${fmtMoney(option.ask)}</strong></div>
            <div><span class="muted">Spread</span><strong>${fmtNum(option.spread_pct, '%')}</strong></div>
            <div><span class="muted">Delta</span><strong>${fmtNum(option.delta)}</strong></div>
            <div><span class="muted">IV</span><strong>${fmtIv(option.iv)}</strong></div>
            <div><span class="muted">Vol/OI</span><strong>${fmtInt(option.volume)} / ${fmtInt(option.open_interest)}</strong></div>
            <div><span class="muted">Opt Score</span><strong>${option.score || 0}</strong></div>
            <div><span class="muted">Quote age/source</span><strong>${option.quote_age_seconds !== undefined && option.quote_age_seconds !== null ? fmtNum(option.quote_age_seconds, 's') : 'Unavailable'} / ${esc(option.timestamp_source_field || 'Unavailable')}</strong></div>
            <div><span class="muted">DTE / Strike dist</span><strong>${option.days_to_expiration ?? 'Unavailable'} / ${option.strike_distance_pct !== undefined && option.strike_distance_pct !== null ? fmtNum(option.strike_distance_pct, '%') : 'Unavailable'}</strong></div>
            <div><span class="muted">Liquidity / Session</span><strong>${esc(option.liquidity_state || 'UNKNOWN')} / ${esc(option.time_state || 'UNKNOWN')}</strong></div>
          </div>
          <div class="muted">${esc(option.message || '')}</div>
          ${option.feed === 'simulated' ? '<div class="muted">Simulated dry-run option data</div>' : ''}
          ${option.feed === 'indicative' ? '<div class="muted">Indicative options data is not official OPRA bid/ask.</div>' : ''}
        </div>
      `;
    }

    function renderStatus(data) {
      els.mode.value = data.mode || 'live';
      els.scope.value = data.scope || 'watchlist';
      els.dot.className = `dot ${data.last_error ? 'err' : data.running ? 'on' : ''}`;
      els.status.textContent = data.last_error ? 'Needs Attention' : data.running ? 'Running' : 'Stopped';
      els.subtitle.textContent = `${(data.symbols || []).length} watchlist symbols | ${data.mode || 'live'} mode | ${data.scope || 'watchlist'} scope`;
      els.scanCount.textContent = data.scan_count || 0;
      els.lastAlerts.textContent = data.last_alert_count || 0;
      els.symbolCount.textContent = data.last_symbol_count || 0;
      els.discoveryCount.textContent = data.last_discovery_count || 0;
      els.topScore.textContent = data.top_score || 0;
      els.interval.textContent = `${data.interval || 0}s`;
      els.discord.textContent = data.has_discord_webhook ? 'On' : 'Off';
      els.sms.textContent = data.has_desktop_alerts ? 'On' : (data.has_pushover_alerts ? 'Pushover' : (data.has_sms_alerts ? 'Messages' : 'Off'));
      els.openai.textContent = data.has_ai_review_enabled ? (data.has_openai_key ? 'On' : 'No Key') : 'Disabled';
      els.lastScan.textContent = fmtTime(data.last_scan_at);
      els.start.disabled = data.running;
      els.stop.disabled = !data.running;
      els.scan.disabled = data.running;
      els.error.style.display = data.last_error ? 'block' : 'none';
      els.error.textContent = data.last_error || '';
      els.symbols.innerHTML = (data.symbols || []).map((s) => `<span class="chip">${s}</span>`).join('');
      renderMarketDataStatus(data.market_data_status || {});
      renderScannerIdentityStatus(data.scanner_identity || {}, data.market_data_status || {});
      renderNotificationStatus(data.notification_status || {});
      renderLiveMarketStructure(data.market_structure || {});
    }

    function renderScannerIdentityStatus(identity, market) {
      const profile = identity.scanner_alert_profile || 'unknown';
      const alerts = identity.alert_symbols || [];
      const context = identity.context_symbols || [];
      const stock = market.stock_feed_status || market.stock_feed_requested || 'unknown';
      const options = market.options_feed_status || market.options_feed_requested || 'unknown';
      const official = profile === 'AAPL_TESTING'
        && alerts.length === 1 && alerts[0] === 'AAPL'
        && context.includes('SPY') && context.includes('QQQ');
      els.scannerIdentityStatus.className = `health ${official ? 'good' : 'warn'}`;
      els.scannerIdentityStatusValue.textContent = profile;
      els.scannerIdentityStatusDetails.innerHTML = `
        <div>Machine: <strong>${esc(identity.scanner_instance_name || identity.hostname || 'unknown')}</strong></div>
        <div>Commit: <strong>${esc(identity.git_commit || 'unknown')}</strong></div>
        <div>Alerts: <strong>${esc(alerts.join(', ') || 'none')} alert-only</strong></div>
        <div>Context: <strong>${esc(context.join(', ') || 'none')} context-only</strong></div>
        <div>Alert types: <strong>${esc((identity.alert_types_enabled || []).join(', ') || 'none')}</strong></div>
        <div>Telegram: <strong>${esc(identity.telegram_destination_type || 'unknown')} ${identity.telegram_chat_id_last4 ? `/*${esc(identity.telegram_chat_id_last4)}` : ''}</strong></div>
        <div>Feeds: <strong>${esc(stock)} / ${esc(options)}</strong></div>
      `;
    }

    function renderNotificationStatus(status) {
      const enabled = Boolean(status.telegram_enabled);
      const configured = Boolean(status.telegram_configured);
      const error = status.last_telegram_error || '';
      els.notificationStatus.className = `health ${enabled && configured && !error ? 'good' : enabled && (!configured || error) ? 'warn' : ''}`;
      els.notificationStatusValue.textContent = enabled ? (configured ? 'Telegram Ready' : 'Telegram Needs Configuration') : 'Telegram Disabled';
      els.notificationStatusDetails.innerHTML = `
        <div>Telegram enabled: <strong>${enabled ? 'Yes' : 'No'}</strong></div>
        <div>Telegram configured: <strong>${configured ? 'Yes' : 'No'}</strong></div>
        <div>Last Telegram alert: <strong>${fmtTime(status.last_telegram_alert_time)}</strong></div>
        <div>Duplicate blocked: <strong>${status.telegram_duplicate_blocked ? 'Yes' : 'No'}</strong></div>
        <div>Active channels: <strong>${esc((status.active_alert_channels || []).join(', ') || 'None')}</strong></div>
        ${error ? `<div class="muted">Last error: ${esc(error)}</div>` : ''}
      `;
    }

    function renderMarketDataStatus(status) {
      const stock = status.stock_feed_status || status.stock_feed_requested || 'unknown';
      const options = status.options_feed_status || status.options_feed_requested || 'unknown';
      const opra = status.opra_status || 'unknown';
      const warning = status.feed_warning || '';
      const good = String(stock).toUpperCase() === 'SIP' && ['ENABLED', 'OPRA'].includes(String(opra).toUpperCase());
      const warn = Boolean(warning) || String(options).toUpperCase() === 'INDICATIVE';
      els.marketDataStatus.className = `health ${good && !warn ? 'good' : warn ? 'warn' : ''}`;
      els.marketDataStatusValue.textContent = warning ? 'Warning' : `${stock} / ${options}`;
      els.marketDataStatusDetails.innerHTML = `
        <div>Stock feed: <strong>${esc(stock)}</strong></div>
        <div>Options feed: <strong>${esc(options)}</strong></div>
        <div>OPRA agreement: <strong>${esc(opra)}</strong></div>
        <div>Last check: <strong>${fmtTime(status.last_data_check_time || status.timestamp)}</strong></div>
        <div>Rate limit: <strong>${esc(status.api_rate_limit_mode || 'unknown')}</strong></div>
        <div>Websocket symbols: <strong>${esc(status.websocket_symbol_limit || 'unknown')}</strong></div>
        ${warning ? `<div class="muted">${esc(warning)}</div>` : ''}
      `;
    }

    function renderMarket(rows) {
      if (!rows.length) {
        els.market.innerHTML = '<div class="empty">Run a scan to populate the market view.</div>';
        return;
      }
      els.market.innerHTML = `
        <table>
          <thead>
            <tr>
	              <th>Score</th>
	              <th>Opt Score</th>
	              <th>Symbol</th>
	              <th>Setup</th>
	              <th>Risk</th>
	              <th>Quality</th>
              <th>Age</th>
              <th>Source</th>
              <th>Price</th>
              <th>Fast</th>
              <th>Day</th>
              <th>Recent RVOL</th>
              <th>Volume</th>
              <th>Signals</th>
              <th>Latest News</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((row) => `
              <tr>
	                <td><span class="score ${scoreClass(row.score || 0)}">${row.score || 0}</span></td>
	                <td><span class="score ${scoreClass(row.options_score || 0)}">${row.options_score || 0}</span></td>
	                <td><strong>${esc(row.symbol)}</strong><div class="muted">${fmtTime(row.bar_time)}</div></td>
	                <td>${esc(row.primary_setup || '')}<div class="muted">${row.strategy_confidence_score ?? ''} ${esc(row.strategy_confidence_label || '')}</div></td>
	                <td><span class="quality ${qualityClass(row.risk_label)}">${esc(riskText(row.risk_label))}</span></td>
	                <td><span class="quality ${qualityClass(row.data_quality)}">${esc(row.data_quality || '')}</span></td>
                <td>${fmtAge(row.bar_age_minutes)}</td>
                <td>${esc(row.source || '')}</td>
                <td>${fmtNum(row.price)}</td>
                <td>${fmtPct(row.fast_move_pct)}</td>
                <td>${fmtPct(row.day_move_pct)}</td>
                <td>${fmtNum(row.recent_rvol, 'x')}</td>
                <td>${fmtInt(row.recent_volume)}</td>
                <td>${(row.flags || []).map((flag) => `<span class="flag">${esc(flag)}</span>`).join('')}</td>
                <td>${row.url ? `<a href="${esc(row.url)}" target="_blank" rel="noreferrer">${esc(row.headline || row.url)}</a>` : esc(row.headline || '')}</td>
              </tr>
	              <tr class="options-row">
	                <td colspan="15">
	                  ${renderStrategySummary(row)}
	                  <div class="options-view">
                    ${renderOptionCard('Best Call', row.best_call)}
                    ${renderOptionCard('Best Put', row.best_put)}
                  </div>
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }

    function renderAlerts(alerts) {
      els.alertCount.textContent = alerts.length ? `${alerts.length} recent` : 'No alerts';
      if (!alerts.length) {
        els.alerts.innerHTML = '<div class="empty">No alerts logged yet.</div>';
        return;
      }
      els.alerts.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Symbol</th>
	              <th>Grade</th>
	              <th>Setup</th>
	              <th>Risk</th>
	              <th>Category</th>
              <th>Price</th>
              <th>Move</th>
              <th>Volume</th>
              <th>Option</th>
              <th>Headline</th>
            </tr>
          </thead>
          <tbody>
            ${alerts.map((a) => `
              <tr>
                <td class="muted">${fmtTime(a.timestamp)}</td>
	                <td><strong>${a.symbol || ''}</strong></td>
	                <td><span class="grade ${gradeClass(a.alert_grade)}">${esc(a.alert_grade || '')}</span><div class="muted">${a.alert_score ?? ''}</div></td>
	                <td>${esc(a.primary_setup || '')}<div class="muted">${a.strategy_confidence_score ?? ''} ${esc(a.strategy_confidence_label || '')}</div></td>
	                <td><span class="quality ${qualityClass(a.risk_label)}">${esc(riskText(a.risk_label))}</span></td>
	                <td><span class="category">${a.category || ''}</span></td>
                <td>${fmtNum(a.price)}</td>
                <td>${fmtPct(a.fast_move_pct)} <span class="muted">${fmtPct(a.day_move_pct)}</span></td>
                <td>${fmtNum(a.relative_volume, 'x')}</td>
                <td>${a.option_contract ? `${esc(a.option_contract)}<div class="muted">${esc(a.direction || '')} | ${esc(a.option_quality || '')} ${a.options_score ? `| ${a.options_score}` : ''}</div><div class="muted">${esc(a.market_alignment || '')}</div>` : esc(a.option_quality || '')}</td>
	                <td>${a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noreferrer">${esc(a.headline || a.url)}</a>` : esc(a.headline || '')}</td>
	              </tr>
	              <tr class="options-row">
	                <td colspan="11">${renderStrategySummary(a)}</td>
	              </tr>
	            `).join('')}
          </tbody>
        </table>
      `;
    }

    function renderAlpacaHealth(data) {
      const summary = data.summary || {};
      const warnings = data.warnings || [];
      const checked = data.last_checked ? fmtTime(data.last_checked) : data.checked_at ? fmtTime(data.checked_at) : 'Not checked';
      const hasLiveWarning = String(data.mode || summary.mode || '').toUpperCase() === 'LIVE';
      els.alpacaHealth.className = `health ${data.ok ? (hasLiveWarning ? 'warn' : 'good') : 'bad'}`;
      els.alpacaHealthStatus.textContent = data.ok ? (hasLiveWarning ? 'Healthy with LIVE warning' : 'Healthy') : (data.connection_status || 'Needs attention');
      const rawProblem = Object.values(data.commands || {}).find((command) => command && command.status !== 'ok' && (command.stderr || command.raw_output));
      const positions = summary.positions === null || summary.positions === undefined ? 'UNKNOWN' : summary.positions;
      els.alpacaHealthDetails.innerHTML = `
        <div>Alpaca CLI: <strong>${esc(summary.alpaca_cli || (data.cli?.installed ? 'OK' : 'ERROR'))}</strong>${data.cli?.path ? ` <span class="muted">${esc(data.cli.path)}</span>` : ''}</div>
        <div>Mode: <strong>${esc(summary.mode || data.mode || 'PAPER')}</strong></div>
        <div>Account: <strong>${esc(summary.account || 'UNKNOWN')}</strong></div>
        <div>Market: <strong>${esc(summary.market || 'UNKNOWN')}</strong></div>
        <div>Next Open: <strong>${summary.next_open ? esc(fmtTime(summary.next_open)) : 'UNKNOWN'}</strong></div>
        <div>Buying Power: <strong>${summary.buying_power ? esc(fmtMoney(summary.buying_power)) : 'UNKNOWN'}</strong></div>
        <div>Portfolio Value: <strong>${summary.portfolio_value ? esc(fmtMoney(summary.portfolio_value)) : 'UNKNOWN'}</strong></div>
        <div>Positions: <strong>${esc(positions)}</strong></div>
        <div>Last Checked: <strong>${esc(checked)}</strong></div>
        <div>Errors: <strong>${esc(summary.errors || data.error || 'None')}</strong></div>
        ${warnings.map((warning) => `<div class="health-warning">${esc(warning)}</div>`).join('')}
        ${data.cache_message ? `<div class="cache-note">${esc(data.cache_message)}</div>` : ''}
        ${rawProblem ? `<pre>${esc(rawProblem.stderr || rawProblem.raw_output || '')}</pre>` : ''}
      `;
    }

    async function checkAlpacaHealth() {
      els.alpacaHealthBtn.disabled = true;
      els.alpacaHealthStatus.textContent = 'Checking';
      els.alpacaHealthDetails.innerHTML = '<div class="muted">Running alpaca doctor, clock, and account checks...</div>';
      try {
        const health = await api('/api/alpaca-health');
        renderAlpacaHealth(health);
      } catch (err) {
        els.alpacaHealth.className = 'health bad';
        els.alpacaHealthStatus.textContent = 'Needs attention';
        els.alpacaHealthDetails.innerHTML = `<div>Error: <strong>${esc(err.message)}</strong></div>`;
      } finally {
        els.alpacaHealthBtn.disabled = false;
      }
    }

    function renderOpenAiReview(data) {
      const review = data.ai_review || data.analysis || {};
      const checked = data.last_checked ? fmtTime(data.last_checked) : 'Not checked';
      const risk = String(review.risk_level || '').toLowerCase();
      const reviewClass = !data.ok ? 'bad' : risk === 'high' ? 'warn' : 'good';
      els.openaiReview.className = `health ${reviewClass}`;
      els.openaiReviewStatus.textContent = data.ok ? 'Ready' : 'Needs attention';
      if (!data.ok) {
        els.openaiReviewDetails.innerHTML = `
          <div>Last Checked: <strong>${esc(checked)}</strong></div>
          <div>Error: <strong>${esc(data.error || data.user_friendly_message || 'AI Review failed — scanner data is still available.')}</strong></div>
          ${data.cache_message ? `<div class="cache-note">${esc(data.cache_message)}</div>` : ''}
        `;
        return;
      }
      const chase = review.do_not_chase_warning || {};
      els.openaiReviewDetails.innerHTML = `
        <div>Model: <strong>${esc(data.model || 'UNKNOWN')}</strong></div>
        <div>Last Checked: <strong>${esc(checked)}</strong></div>
        <div>Timing: <strong>${esc(review.timing || 'Unknown')}</strong></div>
        <div>Direction Label: <strong>${esc(review.direction_label || 'Unknown')}</strong></div>
        <div>Missed Setup: <strong>${esc(review.missed_setup || 'Unknown')}</strong></div>
        <div>Rule Strictness: <strong>${esc(review.rule_strictness || 'Unknown')}</strong></div>
        <div>Risk Level: <strong>${esc(review.risk_level || 'Unknown')}</strong></div>
        <div>Confidence: <strong>${esc(review.confidence ?? 0)} / 100</strong></div>
        <div>Suggested Tuning: <strong>${esc(review.suggested_tuning || '')}</strong></div>
        <div>Plain-English Summary: <strong>${esc(review.plain_english_summary || '')}</strong></div>
        <div>What To Watch Next: <strong>${esc(review.what_to_watch_next || '')}</strong></div>
        <div>Do Not Chase Warning: <strong>${chase.warning ? 'Yes' : 'No'}${chase.reason ? ` - ${esc(chase.reason)}` : ''}</strong></div>
        <div>Errors: <strong>${esc(data.summary?.errors || data.error || 'None')}</strong></div>
        ${data.cache_message ? `<div class="cache-note">${esc(data.cache_message)}</div>` : ''}
      `;
    }

    async function checkOpenAiReview() {
      els.openaiReviewBtn.disabled = true;
      els.openaiReviewStatus.textContent = 'Reviewing';
      els.openaiReviewDetails.innerHTML = '<div class="muted">Reviewing current scanner rows and recent alerts. This does not trigger texts.</div>';
      try {
        const review = await api('/api/ai-review', { method: 'POST', body: '{}' });
        renderOpenAiReview(review);
      } catch (err) {
        els.openaiReview.className = 'health bad';
        els.openaiReviewStatus.textContent = 'Needs attention';
        els.openaiReviewDetails.innerHTML = `<div>Error: <strong>${esc(err.message)}</strong></div>`;
      } finally {
        els.openaiReviewBtn.disabled = false;
      }
    }

    async function refresh() {
      try {
        const [status, alerts, symbols] = await Promise.all([api('/api/status'), api('/api/alerts'), api('/api/symbols')]);
        renderStatus(status);
        renderMarket(symbols.symbols || []);
        renderAlerts(alerts.alerts || []);
      } catch (err) {
        els.error.style.display = 'block';
        els.error.textContent = err.message;
      }
    }

    els.start.addEventListener('click', async () => {
      await api('/api/start', { method: 'POST', body: JSON.stringify({ mode: els.mode.value, scope: els.scope.value }) });
      refresh();
    });
    els.stop.addEventListener('click', async () => {
      await api('/api/stop', { method: 'POST', body: '{}' });
      refresh();
    });
    els.scan.addEventListener('click', async () => {
      els.scan.disabled = true;
      try {
        await api('/api/scan-once', { method: 'POST', body: JSON.stringify({ mode: els.mode.value, scope: els.scope.value }) });
      } finally {
        refresh();
      }
    });
    els.refresh.addEventListener('click', refresh);
    els.alpacaHealthBtn.addEventListener('click', checkAlpacaHealth);
    els.openaiReviewBtn.addEventListener('click', checkOpenAiReview);
    els.clear.addEventListener('click', async () => {
      const confirmed = window.confirm('Clear alerts, market rows, counters, and alert cooldowns?');
      if (!confirmed) return;
      els.clear.disabled = true;
      try {
        await api('/api/clear', { method: 'POST', body: '{}' });
      } finally {
        els.clear.disabled = false;
        refresh();
      }
    });
    setInterval(refresh, 3000);
    refresh();
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "EliteScannerDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def send_json(self, data: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_error(self, exc: Exception) -> None:
        logger.exception("Request failed: %s", exc)
        self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_html(INDEX_HTML)
            elif parsed.path == "/api/status":
                self.send_json(STATE.snapshot())
            elif parsed.path == "/api/alerts":
                limit = int(parse_qs(parsed.query).get("limit", ["100"])[0])
                self.send_json({"alerts": load_alerts(limit=limit)})
            elif parsed.path == "/api/symbols":
                self.send_json({"symbols": load_symbol_rows()})
            elif parsed.path == "/api/alpaca-health":
                force_refresh = parse_qs(parsed.query).get("force_refresh", ["false"])[0].lower() == "true"
                self.send_json(alpaca_health_check(force_refresh=force_refresh))
            elif parsed.path == "/api/openai-analysis":
                force_refresh = parse_qs(parsed.query).get("force_refresh", ["false"])[0].lower() == "true"
                self.send_json(openai_analysis(force_refresh=force_refresh))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            body = self.read_json()
            mode = body.get("mode", "live")
            scope = body.get("scope", "watchlist")
            if parsed.path == "/api/start":
                self.send_json(start_scanner(mode, scope))
            elif parsed.path == "/api/stop":
                self.send_json(stop_scanner())
            elif parsed.path == "/api/scan-once":
                self.send_json(run_once(mode, scope))
            elif parsed.path == "/api/ai-review":
                self.send_json(ai_review(body))
            elif parsed.path == "/api/clear":
                self.send_json(clear_dashboard_data())
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.handle_error(exc)


def main() -> int:
    scanner_app.load_dotenv()
    parser = argparse.ArgumentParser(description="Elite Momentum Scanner dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=str, help="Optional JSON config path")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in a browser")
    args = parser.parse_args()

    if args.config:
        STATE.config_path = Path(args.config).resolve()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"
    logger.info("Dashboard running at %s", url)
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard stopped")
    finally:
        STATE.stop_event.set()
        server.server_close()
    return 0


def run_tests() -> int:
    import unittest
    from unittest import mock

    class DashboardTests(unittest.TestCase):
        def setUp(self) -> None:
            reset_alpaca_health_cache()
            reset_openai_analysis_cache()

        def write_fake_cli(self, temp_dir: str, body: str) -> Path:
            fake_cli = Path(temp_dir) / "alpaca"
            fake_cli.write_text(body, encoding="utf-8")
            fake_cli.chmod(0o755)
            return fake_cli

        def with_cli_path(self, path: Path) -> tuple[Optional[str], Optional[str]]:
            old_path = os.environ.get("ALPACA_CLI_PATH")
            old_live = os.environ.get("ALPACA_LIVE_TRADE")
            os.environ["ALPACA_CLI_PATH"] = str(path)
            os.environ.pop("ALPACA_LIVE_TRADE", None)
            return old_path, old_live

        def restore_cli_env(self, old_path: Optional[str], old_live: Optional[str]) -> None:
            if old_path is None:
                os.environ.pop("ALPACA_CLI_PATH", None)
            else:
                os.environ["ALPACA_CLI_PATH"] = old_path
            if old_live is None:
                os.environ.pop("ALPACA_LIVE_TRADE", None)
            else:
                os.environ["ALPACA_LIVE_TRADE"] = old_live

        def valid_ai_review(self, confidence: int = 80) -> Dict[str, Any]:
            return {
                "timing": "Good",
                "direction_label": "Correct",
                "missed_setup": "No",
                "rule_strictness": "Balanced",
                "risk_level": "Medium",
                "suggested_tuning": "Keep current thresholds and keep watching RVOL confirmation.",
                "plain_english_summary": "The scanner call makes sense for the data available at detection time.",
                "what_to_watch_next": "Watch whether price holds the key level with volume.",
                "do_not_chase_warning": {"warning": False, "reason": ""},
                "confidence": confidence,
            }

        def test_score_zero_for_stale_snapshot(self) -> None:
            config = scanner_app.load_config(None)
            config["symbols"] = ["ASTS"]
            config["symbols_with_options"] = ["ASTS"]
            old = scanner_app.now_utc() - scanner_app.timedelta(minutes=60)
            bars = [
                scanner_app.Bar(t=old + scanner_app.timedelta(minutes=i), o=100.0, h=101.0, l=99.0, c=100.0, v=200000)
                for i in range(12)
            ]
            snap = scanner_app.SymbolSnapshot(symbol="ASTS", latest_bar=bars[-1], recent_bars=bars)
            app = scanner_app.EliteScanner(
                config,
                scanner_app.MockProvider(["ASTS"]),
                scanner_app.DiscordNotifier(None),
                scanner_app.AlertWriter(scanner_app.LOG_DIR / "dashboard_test.csv", scanner_app.LOG_DIR / "dashboard_test.jsonl"),
                scanner_app.StateStore(scanner_app.STATE_DIR / "dashboard_test_state.json"),
            )
            self.assertEqual(score_symbol(app, snap), 0)

        def test_phase9_news_context_does_not_upgrade_dashboard_score(self) -> None:
            config = scanner_app.load_config(None)
            app = scanner_app.EliteScanner(
                config,
                scanner_app.MockProvider(["AAPL"]),
                scanner_app.DiscordNotifier(None),
                scanner_app.AlertWriter(scanner_app.LOG_DIR / "dashboard_news_score.csv", scanner_app.LOG_DIR / "dashboard_news_score.jsonl"),
                scanner_app.StateStore(scanner_app.STATE_DIR / "dashboard_news_score_state.json"),
            )
            now = scanner_app.now_utc()
            bars = [
                scanner_app.Bar(t=now - timedelta(minutes=10 - i), o=100, h=100.1, l=99.9, c=100, v=100000)
                for i in range(11)
            ]
            plain = scanner_app.SymbolSnapshot(symbol="AAPL", latest_bar=bars[-1], recent_bars=bars)
            with_news = scanner_app.SymbolSnapshot(
                symbol="AAPL",
                latest_bar=bars[-1],
                recent_bars=bars,
                latest_news=scanner_app.NewsItem(
                    symbol="AAPL",
                    headline="Apple raises outlook",
                    url="https://example.com/aapl",
                    published_at=now,
                    source="TestWire",
                ),
            )
            self.assertEqual(score_symbol(app, plain), score_symbol(app, with_news))

        def test_phase2h_dashboard_rows_include_confirmation_fields(self) -> None:
            config = scanner_app.load_config(None)
            config["symbols"] = ["AAPL"]
            config["symbols_with_options"] = ["AAPL"]
            now = scanner_app.now_utc()
            bars = [
                scanner_app.Bar(t=now - scanner_app.timedelta(minutes=5 - i), o=100 + i * 0.1, h=100.2 + i * 0.1, l=99.8 + i * 0.1, c=100.1 + i * 0.1, v=200000)
                for i in range(6)
            ]
            snap = scanner_app.SymbolSnapshot(
                symbol="AAPL",
                latest_bar=bars[-1],
                recent_bars=bars,
                best_call=scanner_app.OptionSelection(
                    scanner_app.OptionContractSnapshot(
                        symbol=scanner_app.option_symbol("AAPL", scanner_app.now_et().date(), "C", 100.0),
                        underlying_symbol="AAPL",
                        option_type="C",
                        expiration_date=scanner_app.now_et().date(),
                        strike=100.0,
                        bid=1.95,
                        ask=2.05,
                        quote_time=scanner_app.now_utc(),
                        feed="indicative",
                    ),
                    "Tradable",
                    88,
                ),
            )
            app = scanner_app.EliteScanner(
                config,
                scanner_app.MockProvider(["AAPL"]),
                scanner_app.DiscordNotifier(None),
                scanner_app.AlertWriter(scanner_app.LOG_DIR / "dashboard_phase2h.csv", scanner_app.LOG_DIR / "dashboard_phase2h.jsonl"),
                scanner_app.StateStore(scanner_app.STATE_DIR / "dashboard_phase2h_state.json"),
            )
            old_eval = scanner_app.evaluate_strategy_suite

            def fake_strategy_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
                return {
                    "primary_setup": "Breakout Retest Holding",
                    "secondary_setups": ["VWAP Hold"],
                    "direction": "bullish",
                    "confidence_score": 84,
                    "confidence_label": "HIGH",
                    "confirmation_score": 78,
                    "confirmation_label": "STRONG",
                    "entry_quality_label": "GOOD_POSITION",
                    "volume_label": "STRONG",
                    "rvol": 2.1,
                    "candle_label": "BUYER_CONTROL",
                    "candle_score": 82,
                    "extension_label": "NORMAL",
                    "extension_score": 20,
                    "relative_strength_label": "STRONG",
                    "relative_strength_score": 75,
                    "market_regime": "TRENDING_UP",
                    "regime_score": 80,
                    "market_score": 80,
                    "regime_reason": "SPY and QQQ are above VWAP with rising EMA structure",
                    "spy_alignment": "ALIGNED",
                    "qqq_alignment": "ALIGNED",
                    "aapl_relative_strength": "STRONG",
                    "volume_state": "STRONG",
                    "volatility_state": "NORMAL",
                    "pressure_label": "UNKNOWN",
                    "pressure_score": 50,
                    "risk_label": "MEDIUM",
                    "scenario_top": {"scenario_name": "Bullish Trend Continuation", "stage": "GOOD_POSITION"},
                    "scenario_second": {"scenario_name": "Pullback Holding", "stage": "FORMING"},
                    "scenario_score": 84,
                    "scenario_stage": "GOOD_POSITION",
                    "scenario_direction": "bullish",
                    "scenario_confidence_label": "HIGH",
                    "scenario_entry_quality_label": "GOOD_POSITION",
                    "scenario_risk_label": "LOW",
                    "scenario_reasons": ["Price above VWAP"],
                    "scenario_warnings": [],
                    "scenario_levels": {"vwap": 100.5},
                    "bullish_score": 84,
                    "bearish_score": 22,
                    "chop_score": 15,
                    "fakeout_score": 8,
                    "scenario_conflict": False,
                    "scenario_alert_tier": "DASHBOARD_ALERT",
                    "scenario_alert_block_reason": "Option feed is indicative",
                    "all_scenarios": [],
                    "stock_setup_score": 82,
                    "stock_setup_score_reason": "Breakout Retest Holding is strong",
                    "stock_setup_valid": True,
                    "option_tradability_score": 88,
                    "option_feed_status": "INDICATIVE",
                    "option_tradable": False,
                    "scenario_alert_eligible": True,
                    "scenario_would_sms": False,
                    "scenario_sms_block_reason": "Option feed is indicative",
                    "sms_block_reason": "Option feed is indicative",
                    "strategy_results": [],
                    "vwap": 100.5,
                    "ema9": 100.3,
                    "ema20": 100.1,
                    "reasons": ["Volume confirms"],
                    "warnings": [],
                    "levels": {"vwap": 100.5},
                }

            try:
                scanner_app.evaluate_strategy_suite = fake_strategy_summary
                rows = build_symbol_rows(app, {"AAPL": snap}, {"AAPL": "watchlist"}, {"SPY": "BULLISH", "QQQ": "BULLISH"})
            finally:
                scanner_app.evaluate_strategy_suite = old_eval
            self.assertEqual(rows[0]["confirmation_label"], "STRONG")
            self.assertEqual(rows[0]["entry_quality_label"], "GOOD_POSITION")
            self.assertEqual(rows[0]["candle_label"], "BUYER_CONTROL")
            self.assertEqual(rows[0]["relative_strength_label"], "STRONG")
            self.assertEqual(rows[0]["market_regime"], "TRENDING_UP")
            self.assertEqual(rows[0]["spy_alignment"], "ALIGNED")
            self.assertEqual(rows[0]["qqq_alignment"], "ALIGNED")
            self.assertEqual(rows[0]["aapl_relative_strength"], "STRONG")
            self.assertEqual(rows[0]["regime_reason"], "SPY and QQQ are above VWAP with rising EMA structure")
            self.assertEqual(rows[0]["scenario_top"]["scenario_name"], "Bullish Trend Continuation")
            self.assertEqual(rows[0]["option_feed_status"], "INDICATIVE")
            self.assertEqual(rows[0]["stock_setup_score"], 82)
            self.assertEqual(rows[0]["scenario_alert_tier"], "DASHBOARD_ALERT")
            self.assertIn(rows[0]["alert_tier"], {"SETUP_CONFIRMED", "SETUP_FORMING", "RISK_WARNING"})
            self.assertTrue(rows[0]["alert_tier_reason"])
            self.assertTrue(rows[0]["alert_source"])
            self.assertTrue(rows[0]["message_source_path"])
            self.assertIn("invalidation_reason", rows[0])
            self.assertIn("stop_logic_description", rows[0])
            self.assertIn("entry_timing_label", rows[0])
            self.assertEqual(rows[0]["stock_setup_score_reason"], "Breakout Retest Holding is strong")
            self.assertTrue(rows[0]["scenario_alert_eligible"])
            self.assertFalse(rows[0]["scenario_would_sms"])
            self.assertEqual(rows[0]["scenario_sms_block_reason"], "Option feed is indicative")
            self.assertIn("phase3_heads_up_eligible", rows[0])
            self.assertIn("phase3_heads_up_sent", rows[0])
            self.assertIn("phase3_heads_up_block_reason", rows[0])
            self.assertIn("phase3_heads_up_type", rows[0])
            self.assertIn("phase3_heads_up_dedupe_blocked", rows[0])
            self.assertIn("phase3_heads_up_dedupe_reason", rows[0])
            self.assertIn("phase3_heads_up_message_fingerprint", rows[0])
            self.assertIn("market_confirmation_status", rows[0])
            self.assertEqual(rows[0]["market_confirmation_status"], "AVAILABLE")

        def test_cleanup_dashboard_has_compact_phase2_and_loud_do_not_chase(self) -> None:
            self.assertIn("function renderPhase2Summary", INDEX_HTML)
            self.assertIn("function renderPhase3Summary", INDEX_HTML)
            self.assertIn("Phase 3 Heads-Up Type", INDEX_HTML)
            self.assertIn("Heads-Up Dedupe", INDEX_HTML)
            self.assertIn("Market Confirmation", INDEX_HTML)
            self.assertIn("Notification Status", INDEX_HTML)
            self.assertIn("Telegram configured", INDEX_HTML)
            self.assertIn("Duplicate blocked", INDEX_HTML)
            self.assertIn("Active channels", INDEX_HTML)
            self.assertIn("Official Scanner Profile", INDEX_HTML)
            self.assertIn("renderScannerIdentityStatus", INDEX_HTML)
            self.assertIn("alert-only", INDEX_HTML)
            self.assertIn("context-only", INDEX_HTML)
            self.assertIn("Professional Alert Tier", INDEX_HTML)
            self.assertIn("Alert Tier Reason", INDEX_HTML)
            self.assertIn("Risk / Invalidation", INDEX_HTML)
            self.assertIn("Market Regime", INDEX_HTML)
            self.assertIn("SPY / QQQ alignment", INDEX_HTML)
            self.assertIn("AAPL relative strength", INDEX_HTML)
            self.assertIn("WATCH ONLY — choppy regime downgrades setup quality", INDEX_HTML)
            self.assertIn("Market Structure", INDEX_HTML)
            self.assertIn("1m / 5m / 15m", INDEX_HTML)
            self.assertIn("Nearest level", INDEX_HTML)
            self.assertIn("Key warning", INDEX_HTML)
            self.assertIn("Professional Setup", INDEX_HTML)
            self.assertIn("Option Quality", INDEX_HTML)
            self.assertIn("Trade-ready / Stock-only", INDEX_HTML)
            self.assertIn("Quote age/source", INDEX_HTML)
            self.assertIn("News Context", INDEX_HTML)
            self.assertIn("Context only — confirm price reaction", INDEX_HTML)
            self.assertIn("Upgraded alert", INDEX_HTML)
            self.assertIn("Direction / Stage", INDEX_HTML)
            self.assertIn("Score / Confidence", INDEX_HTML)
            self.assertIn("Idea is wrong", INDEX_HTML)
            self.assertIn("Pullback required", INDEX_HTML)

        def test_live_market_structure_dashboard_reads_logs_and_renders_sections(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                paths = {
                    "support_resistance": root / "support.jsonl",
                    "supply_demand": root / "zones.jsonl",
                    "summary": root / "summary.jsonl",
                }
                timestamp = "2026-06-10T20:05:00+00:00"
                paths["support_resistance"].write_text(
                    "\n".join(
                        json.dumps(
                            {
                                "timestamp": timestamp,
                                "symbol": "AAPL",
                                "timeframe": frame,
                                "current_price": 291.25,
                                "levels": {
                                    "support": [{"price": 290.2, "strength": "HIGH", "score": 82, "times_tested": 3, "fresh": False, "source": "swing_low/VWAP", "reason": "Bounced three times"}],
                                    "resistance": [{"price": 291.6, "strength": "HIGH", "score": 78, "times_tested": 2, "fresh": True, "source": "swing_high", "reason": "Rejected twice"}],
                                },
                            }
                        )
                        for frame in ("1m", "5m")
                    ),
                    encoding="utf-8",
                )
                paths["supply_demand"].write_text(
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "symbol": "AAPL",
                            "timeframe": "5m",
                            "current_price": 291.25,
                            "zones": {
                                "demand": [{"zone_low": 289.8, "zone_high": 290.1, "midpoint": 289.95, "strength": "HIGH", "score": 84, "times_tested": 0, "fresh": True, "last_reaction": "bullish_impulse", "invalidation": "Clean break below 289.80", "reason": "Buyers defended zone"}],
                                "supply": [{"zone_low": 291.5, "zone_high": 291.8, "midpoint": 291.65, "strength": "MEDIUM", "score": 68, "times_tested": 2, "fresh": False, "last_reaction": "bearish_rejection", "invalidation": "Clean hold above 291.80", "reason": "Sellers rejected zone"}],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                paths["summary"].write_text(
                    json.dumps(
                        {
                            "timestamp": timestamp,
                            "symbol": "AAPL",
                            "current_price": 291.25,
                            "market_structure_bias": "MIXED",
                            "structure_quality": "MEDIUM",
                            "structure_warning": "near demand",
                            "current_price_location_summary": "AAPL is between 5m demand near 290.00 and 5m supply near 291.70",
                            "confluence_reason": "5m demand overlaps with support near 290.00",
                            "can_approve_trades": False,
                            "context_only": True,
                        }
                    ),
                    encoding="utf-8",
                )
                payload = load_market_structure_dashboard(
                    scanner_app.load_config(None),
                    paths=paths,
                    now=datetime(2026, 6, 10, 22, 0, tzinfo=timezone.utc),
                )
                self.assertEqual(payload["summary"]["current_price"], 291.25)
                self.assertEqual(payload["summary"]["market_structure_bias"], "MIXED")
                self.assertEqual(payload["summary"]["structure_warning"], "near demand")
                self.assertEqual(payload["nearest"]["support"]["price"], 290.2)
                self.assertEqual(payload["nearest"]["resistance"]["price"], 291.6)
                self.assertEqual(payload["nearest"]["demand"]["zone_low"], 289.8)
                self.assertEqual(payload["nearest"]["supply"]["zone_high"], 291.8)
                self.assertEqual(payload["support_resistance"]["15m"], {})
                self.assertIn("After-hours / limited structure", payload["data_mode"])
                self.assertIn("5m Demand:", payload["copy_summary"])
                self.assertFalse(payload["can_upgrade"])
                serialized = json.dumps(payload)
                self.assertNotIn("ALPACA_API_KEY", serialized)
                self.assertNotIn("TELEGRAM_BOT_TOKEN", serialized)
                self.assertNotIn("OPENAI_API_KEY", serialized)

            for text in (
                "Live Market Structure",
                "renderLiveMarketStructure",
                "Current Price",
                "Structure",
                "Quality",
                "Warning",
                "Nearest Levels",
                "Confluence",
                "['1m', '5m', '15m']",
                "${frame} Support / Resistance",
                "${frame} Supply / Demand",
                "Copy Levels",
                "No clean ${esc(timeframe)} zones detected yet",
                "Not enough clean data yet",
            ):
                self.assertIn(text, INDEX_HTML)

        def test_live_market_structure_dashboard_missing_data_is_clean(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                payload = load_market_structure_dashboard(
                    scanner_app.load_config(None),
                    paths={
                        "support_resistance": root / "missing-support.jsonl",
                        "supply_demand": root / "missing-zones.jsonl",
                        "summary": root / "missing-summary.jsonl",
                    },
                )
            self.assertEqual(payload["data_mode"], "waiting for data")
            self.assertEqual(payload["message"], "Waiting for market structure data.")
            self.assertEqual(payload["copy_summary"], "Not enough clean data yet")
            self.assertFalse(payload["can_upgrade"])

        def test_live_market_structure_refresh_reuses_aapl_snapshot_without_alert_changes(self) -> None:
            config = scanner_app.load_config(None)
            now = scanner_app.now_utc()
            bars = [
                scanner_app.Bar(t=now - timedelta(minutes=20 - index), o=100, h=100.2, l=99.8, c=100.0 + index * 0.01, v=100000)
                for index in range(21)
            ]
            snapshots = {"AAPL": scanner_app.SymbolSnapshot(symbol="AAPL", latest_bar=bars[-1], recent_bars=bars)}
            live_payload = {"symbol": "AAPL", "timestamp": now.isoformat()}
            dashboard_payload = {
                "last_updated": now.isoformat(),
                "data_mode": "latest log fallback",
                "message": "Latest market-structure log data",
                "context_only": True,
                "can_upgrade": False,
            }
            with (
                mock.patch.object(sys.modules[__name__], "build_market_structure", return_value=live_payload) as build,
                mock.patch.object(sys.modules[__name__], "write_market_structure_logs") as write,
                mock.patch.object(sys.modules[__name__], "load_market_structure_dashboard", return_value=dashboard_payload),
            ):
                with STATE.lock:
                    STATE.market_structure_live = None
                    STATE.market_structure_updated_monotonic = 0.0
                result = refresh_live_market_structure(snapshots, config, force=True)
            self.assertIn(result["data_mode"], {"Live scanner data", "After-hours / limited structure"})
            self.assertEqual(result["message"], "Live scanner candle data")
            self.assertTrue(result["context_only"])
            self.assertFalse(result["can_upgrade"])
            build.assert_called_once()
            write.assert_called_once()

        def test_dashboard_status_includes_telegram_without_exposing_token(self) -> None:
            old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
            old_chat = os.environ.get("TELEGRAM_CHAT_ID")
            old_enabled = os.environ.get("ENABLE_TELEGRAM_ALERTS")
            os.environ["TELEGRAM_BOT_TOKEN"] = "dashboard-secret-token"
            os.environ["TELEGRAM_CHAT_ID"] = "123"
            os.environ["ENABLE_TELEGRAM_ALERTS"] = "true"
            try:
                snapshot = STATE.snapshot()
                self.assertTrue(snapshot["notification_status"]["telegram_enabled"])
                self.assertTrue(snapshot["notification_status"]["telegram_configured"])
                self.assertNotIn("dashboard-secret-token", json.dumps(snapshot))
            finally:
                for name, value in {
                    "TELEGRAM_BOT_TOKEN": old_token,
                    "TELEGRAM_CHAT_ID": old_chat,
                    "ENABLE_TELEGRAM_ALERTS": old_enabled,
                }.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
            self.assertIn("Vol ${esc(volume)}", INDEX_HTML)
            self.assertIn("Candle ${esc(item.candle_label", INDEX_HTML)
            self.assertIn("Entry ${esc(item.entry_quality_label", INDEX_HTML)
            self.assertIn("Market ${esc(item.market_regime", INDEX_HTML)
            self.assertIn("RS ${esc(item.relative_strength_label", INDEX_HTML)
            self.assertIn("Confirm ${item.confirmation_score", INDEX_HTML)
            self.assertIn("Scenario Tier", INDEX_HTML)
            self.assertIn("Stock Reason", INDEX_HTML)
            self.assertIn("RISK: DO_NOT_CHASE", INDEX_HTML)
            self.assertIn("Phase 3 Heads-Up Eligible", INDEX_HTML)
            self.assertIn("phase3_heads_up_block_reason", INDEX_HTML)
            self.assertIn("Market Data Status", INDEX_HTML)
            self.assertIn("renderMarketDataStatus", INDEX_HTML)
            self.assertIn("OPRA agreement", INDEX_HTML)

        def test_dashboard_status_includes_official_scanner_identity_without_secrets(self) -> None:
            previous = {
                name: os.environ.get(name)
                for name in (
                    "SCANNER_ALERT_PROFILE",
                    "ALERT_SYMBOLS",
                    "MARKET_CONTEXT_SYMBOLS",
                    "TELEGRAM_BOT_TOKEN",
                    "TELEGRAM_CHAT_ID",
                    "ALPACA_API_KEY",
                    "ALPACA_SECRET_KEY",
                )
            }
            os.environ.update(
                {
                    "SCANNER_ALERT_PROFILE": "AAPL_TESTING",
                    "ALERT_SYMBOLS": "AAPL",
                    "MARKET_CONTEXT_SYMBOLS": "SPY,QQQ",
                    "TELEGRAM_BOT_TOKEN": "dashboard-profile-secret-token",
                    "TELEGRAM_CHAT_ID": "-5213422925",
                    "ALPACA_API_KEY": "dashboard-profile-secret-key",
                    "ALPACA_SECRET_KEY": "dashboard-profile-secret-value",
                }
            )
            try:
                snapshot = STATE.snapshot()
                identity = snapshot["scanner_identity"]
                self.assertEqual(identity["scanner_alert_profile"], "AAPL_TESTING")
                self.assertEqual(identity["alert_symbols"], ["AAPL"])
                self.assertEqual(identity["context_symbols"], ["SPY", "QQQ"])
                self.assertEqual(identity["telegram_destination_type"], "group")
                self.assertEqual(identity["telegram_chat_id_last4"], "2925")
                serialized = json.dumps(snapshot)
                self.assertNotIn("dashboard-profile-secret-token", serialized)
                self.assertNotIn("dashboard-profile-secret-key", serialized)
                self.assertNotIn("dashboard-profile-secret-value", serialized)
                self.assertNotIn("-5213422925", serialized)
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

        def test_dashboard_snapshot_export_handles_missing_state_and_redacts_secrets(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                source_state = snapshot_exporter.collect_live_sources(
                    base_url="http://127.0.0.1:9",
                    log_dir=temp_root,
                    fetcher=lambda *_args, **_kwargs: None,
                )
                snapshot = snapshot_exporter.build_export_package(source_state)
                paths = snapshot_exporter.write_export_files(snapshot, output_dir=temp_root / "exports")
                self.assertTrue(paths["json"].exists())
                self.assertTrue(paths["md"].exists())
                payload = json.loads(paths["json"].read_text(encoding="utf-8"))
                self.assertEqual(payload["symbols"].keys(), {"AAPL", "QQQ", "SPY"})
                self.assertIn("missing or empty", " ".join(payload["notes"]).lower())
                redacted = snapshot_exporter.redact_payload({"api_key": "x", "nested": {"client_secret": "y"}})
                self.assertEqual(redacted["api_key"], "[REDACTED]")
                self.assertEqual(redacted["nested"]["client_secret"], "[REDACTED]")
                md = paths["md"].read_text(encoding="utf-8")
                self.assertIn("# Live Dashboard Snapshot", md)
                self.assertIn("## Watchlist Summary", md)

        def test_dashboard_snapshot_export_cli_runs_without_live_state(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                script = Path(__file__).resolve().parent / "tools" / "export_dashboard_snapshot.py"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "--base-url",
                        "http://127.0.0.1:9",
                        "--log-dir",
                        str(temp_root),
                        "--output-dir",
                        str(temp_root / "exports"),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((temp_root / "exports" / "dashboard_snapshot_latest.json").exists())
                self.assertTrue((temp_root / "exports" / "dashboard_snapshot_latest.md").exists())

        def test_review_package_export_handles_missing_logs_and_redacts_secrets(self) -> None:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                log_dir = temp_root / "logs"
                snapshot_dir = temp_root / "snapshots"
                log_dir.mkdir()
                snapshot_dir.mkdir()
                (log_dir / "alerts.jsonl").write_text(
                    json.dumps(
                        {
                            "timestamp": "2026-06-04T16:02:00+00:00",
                            "symbol": "AAPL",
                            "primary_setup": "Bullish Liquidity Sweep Reclaim",
                            "api_key": "super-secret-key",
                            "scenario_would_sms": False,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (log_dir / "phase3_heads_up.jsonl").write_text(
                    json.dumps(
                        {
                            "timestamp": "2026-06-04T16:03:00+00:00",
                            "symbol": "AAPL",
                            "top_scenario": {"scenario_name": "Pullback Holding"},
                            "scenario_stage": "CONFIRMED",
                            "phase3_heads_up_sent": True,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (log_dir / "market_data_status.jsonl").write_text(
                    json.dumps(
                        {
                            "timestamp": "2026-06-04T13:01:00+00:00",
                            "stock_feed_requested": "SIP",
                            "stock_feed_status": "SIP",
                            "options_feed_requested": "OPRA",
                            "options_feed_status": "OPRA",
                            "opra_status": "enabled",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                for name in (
                    "support_resistance_levels.jsonl",
                    "supply_demand_zones.jsonl",
                    "market_structure.jsonl",
                    "openai_alert_formatter.jsonl",
                    "premarket_discipline_message.jsonl",
                ):
                    (log_dir / name).write_text(
                        json.dumps({"timestamp": "2026-06-04T16:04:00+00:00", "symbol": "AAPL", "token": "super-secret-token"}) + "\n",
                        encoding="utf-8",
                    )
                (snapshot_dir / "dashboard_snapshot_latest.md").write_text("token=super-secret-token", encoding="utf-8")
                (snapshot_dir / "dashboard_snapshot_latest.json").write_text(
                    json.dumps({"client_secret": "super-secret-client"}),
                    encoding="utf-8",
                )
                config_example = temp_root / "config.example.json"
                config_example.write_text(json.dumps({"symbols": ["AAPL"]}), encoding="utf-8")
                script = Path(__file__).resolve().parent / "tools" / "export_review_package.py"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(script),
                        "--date",
                        "2026-06-04",
                        "--start",
                        "11:45",
                        "--end",
                        "now",
                        "--log-dir",
                        str(log_dir),
                        "--snapshot-dir",
                        str(snapshot_dir),
                        "--config-example",
                        str(config_example),
                        "--output-dir",
                        str(temp_root / "exports"),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                package_dir = temp_root / "exports" / "review_package_2026-06-04"
                summary = package_dir / "review_summary.md"
                zip_path = package_dir / "review_package.zip"
                self.assertTrue(summary.exists())
                self.assertTrue(zip_path.exists())
                exported_text = "\n".join(
                    path.read_text(encoding="utf-8", errors="replace")
                    for path in package_dir.rglob("*")
                    if path.is_file() and path.suffix in {".json", ".jsonl", ".md"}
                )
                self.assertNotIn("super-secret", exported_text)
                self.assertIn("[REDACTED]", exported_text)
                self.assertIn("Missing source file", summary.read_text(encoding="utf-8"))
                self.assertIn("## Market Data Status", summary.read_text(encoding="utf-8"))
                self.assertTrue((package_dir / "logs" / "phase3_heads_up.jsonl").exists())
                self.assertTrue((package_dir / "logs" / "market_data_status.jsonl").exists())
                self.assertTrue((package_dir / "window" / "phase3_heads_up_window.jsonl").exists())
                self.assertTrue((package_dir / "logs" / "support_resistance_levels.jsonl").exists())
                self.assertTrue((package_dir / "logs" / "supply_demand_zones.jsonl").exists())
                self.assertTrue((package_dir / "logs" / "market_structure.jsonl").exists())
                self.assertTrue((package_dir / "logs" / "openai_alert_formatter.jsonl").exists())
                self.assertTrue((package_dir / "logs" / "premarket_discipline_message.jsonl").exists())

        def test_watchlist_scope_keeps_configured_symbols(self) -> None:
            config = scanner_app.load_config(None)
            provider = scanner_app.MockProvider(list(config["symbols"]))
            app = scanner_app.EliteScanner(
                config,
                provider,
                scanner_app.DiscordNotifier(None),
                scanner_app.AlertWriter(scanner_app.LOG_DIR / "dashboard_watchlist.csv", scanner_app.LOG_DIR / "dashboard_watchlist.jsonl"),
                scanner_app.StateStore(scanner_app.STATE_DIR / "dashboard_watchlist_state.json"),
            )
            before = list(app.symbols)
            sources, discovery_count = apply_scan_scope(app, "watchlist")
            self.assertEqual(app.symbols, before)
            self.assertEqual(discovery_count, 0)
            self.assertTrue(all(value == "Watchlist" for value in sources.values()))

        def test_hybrid_scope_adds_discovery_candidates(self) -> None:
            config = scanner_app.load_config(None)
            provider = scanner_app.MockProvider(list(config["symbols"]))
            app = scanner_app.EliteScanner(
                config,
                provider,
                scanner_app.DiscordNotifier(None),
                scanner_app.AlertWriter(scanner_app.LOG_DIR / "dashboard_hybrid.csv", scanner_app.LOG_DIR / "dashboard_hybrid.jsonl"),
                scanner_app.StateStore(scanner_app.STATE_DIR / "dashboard_hybrid_state.json"),
            )
            sources, discovery_count = apply_scan_scope(app, "hybrid")
            self.assertGreater(discovery_count, 0)
            self.assertIn("COIN", app.symbols)
            self.assertEqual(sources["AAPL"], "Both")
            self.assertEqual(sources["COIN"], "Discovery")

        def test_option_selection_serializes_for_dashboard(self) -> None:
            expiration = scanner_app.now_et().date()
            contract = scanner_app.OptionContractSnapshot(
                symbol=scanner_app.option_symbol("AAPL", expiration, "C", 100.0),
                underlying_symbol="AAPL",
                option_type="C",
                expiration_date=expiration,
                strike=100.0,
                bid=1.00,
                ask=1.10,
                quote_time=scanner_app.now_utc(),
                volume=500,
                open_interest=1000,
                delta=0.45,
                implied_volatility=0.55,
                feed="indicative",
            )
            item = option_selection_to_dict(scanner_app.OptionSelection(contract, "Tradable", 92))
            self.assertEqual(item["type"], "CALL")
            self.assertEqual(item["quality"], "Tradable")
            self.assertEqual(item["feed"], "indicative")
            self.assertAlmostEqual(item["mid"], 1.05)
            self.assertGreater(item["spread_pct"], 0)

        def test_alpaca_health_reports_missing_cli_cleanly(self) -> None:
            old_path = os.environ.get("ALPACA_CLI_PATH")
            os.environ["ALPACA_CLI_PATH"] = "/tmp/definitely-missing-alpaca-cli"
            try:
                health = alpaca_health_check()
            finally:
                if old_path is None:
                    os.environ.pop("ALPACA_CLI_PATH", None)
                else:
                    os.environ["ALPACA_CLI_PATH"] = old_path
            self.assertFalse(health["ok"])
            self.assertFalse(health["cli"]["installed"])
            self.assertEqual(health["connection_status"], "CLI not found")
            self.assertEqual(health["error_type"], "missing_cli")

        def test_alpaca_health_successful_check(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                fake_cli = self.write_fake_cli(
                    tmp,
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "if os.environ.get('ALPACA_QUIET') != '1':\n"
                    "    sys.exit(5)\n"
                    "args = sys.argv[1:]\n"
                    "if 'doctor' in args:\n"
                    "    print('doctor ok')\n"
                    "elif 'clock' in args:\n"
                    "    print(json.dumps({'is_open': False, 'next_open': '2026-06-02T09:30:00-04:00'}))\n"
                    "elif 'account' in args:\n"
                    "    print(json.dumps({'status': 'ACTIVE', 'buying_power': '1234.56', 'portfolio_value': '7890.12', 'positions_count': 2}))\n"
                    "else:\n"
                    "    sys.exit(1)\n",
                )
                old_path, old_live = self.with_cli_path(fake_cli)
                try:
                    health = alpaca_health_check(force_refresh=True)
                finally:
                    self.restore_cli_env(old_path, old_live)
            self.assertTrue(health["ok"])
            self.assertEqual(health["summary"]["alpaca_cli"], "OK")
            self.assertEqual(health["summary"]["mode"], "PAPER")
            self.assertEqual(health["summary"]["account"], "ACTIVE")
            self.assertEqual(health["summary"]["market"], "CLOSED")
            self.assertEqual(health["summary"]["positions"], 2)
            self.assertFalse(health["cached"])

        def test_alpaca_health_auth_error_exit_code_two(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                fake_cli = self.write_fake_cli(
                    tmp,
                    "#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "args = sys.argv[1:]\n"
                    "if 'doctor' in args:\n"
                    "    print('doctor ok')\n"
                    "elif 'clock' in args:\n"
                    "    print(json.dumps({'is_open': True}))\n"
                    "elif 'account' in args:\n"
                    "    print('unauthorized', file=sys.stderr)\n"
                    "    sys.exit(2)\n",
                )
                old_path, old_live = self.with_cli_path(fake_cli)
                try:
                    health = alpaca_health_check(force_refresh=True)
                finally:
                    self.restore_cli_env(old_path, old_live)
            self.assertFalse(health["ok"])
            self.assertEqual(health["error_type"], "auth_error")
            self.assertEqual(health["summary"]["account"], "ERROR")
            self.assertIn("authenticate", health["commands"]["account"]["user_friendly_message"])

        def test_alpaca_cli_timeout_result_is_structured(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                fake_cli = self.write_fake_cli(
                    tmp,
                    "#!/usr/bin/env python3\n"
                    "import time\n"
                    "time.sleep(2)\n",
                )
                result = run_alpaca_cli(str(fake_cli), "doctor", ["doctor"], timeout=1)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_type"], "timeout")
            self.assertEqual(result["command_name"], "doctor")
            self.assertIn("timed out", result["user_friendly_message"])

        def test_alpaca_health_invalid_json_keeps_raw_output(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                fake_cli = self.write_fake_cli(
                    tmp,
                    "#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "args = sys.argv[1:]\n"
                    "if 'doctor' in args:\n"
                    "    print('doctor ok')\n"
                    "elif 'clock' in args:\n"
                    "    print('not json')\n"
                    "elif 'account' in args:\n"
                    "    print(json.dumps({'status': 'ACTIVE'}))\n",
                )
                old_path, old_live = self.with_cli_path(fake_cli)
                try:
                    health = alpaca_health_check(force_refresh=True)
                finally:
                    self.restore_cli_env(old_path, old_live)
            self.assertFalse(health["ok"])
            self.assertEqual(health["error_type"], "invalid_json")
            self.assertEqual(health["commands"]["clock"]["raw_output"], "not json")
            self.assertEqual(health["summary"]["market"], "UNKNOWN")

        def test_alpaca_health_live_trade_warning(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                fake_cli = self.write_fake_cli(
                    tmp,
                    "#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "args = sys.argv[1:]\n"
                    "if 'doctor' in args:\n"
                    "    print('doctor ok')\n"
                    "elif 'clock' in args:\n"
                    "    print(json.dumps({'is_open': False}))\n"
                    "elif 'account' in args:\n"
                    "    print(json.dumps({'status': 'ACTIVE'}))\n",
                )
                old_path, old_live = self.with_cli_path(fake_cli)
                os.environ["ALPACA_LIVE_TRADE"] = "true"
                try:
                    health = alpaca_health_check(force_refresh=True)
                finally:
                    self.restore_cli_env(old_path, old_live)
            self.assertTrue(health["ok"])
            self.assertEqual(health["mode"], "LIVE")
            self.assertIn("WARNING: Alpaca CLI appears configured for LIVE trading.", health["warnings"])

        def test_alpaca_health_cached_result_reused(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                count_file = Path(tmp) / "count.txt"
                fake_cli = self.write_fake_cli(
                    tmp,
                    "#!/usr/bin/env python3\n"
                    "import json, pathlib, sys\n"
                    f"path = pathlib.Path({str(count_file)!r})\n"
                    "count = int(path.read_text() or '0') if path.exists() else 0\n"
                    "path.write_text(str(count + 1))\n"
                    "args = sys.argv[1:]\n"
                    "if 'doctor' in args:\n"
                    "    print('doctor ok')\n"
                    "elif 'clock' in args:\n"
                    "    print(json.dumps({'is_open': False}))\n"
                    "elif 'account' in args:\n"
                    "    print(json.dumps({'status': 'ACTIVE'}))\n",
                )
                old_path, old_live = self.with_cli_path(fake_cli)
                try:
                    first = alpaca_health_check(force_refresh=True)
                    second = alpaca_health_check()
                finally:
                    self.restore_cli_env(old_path, old_live)
                calls = int(count_file.read_text())
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(calls, 3)

        def test_alpaca_health_force_refresh_bypasses_cache(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                count_file = Path(tmp) / "count.txt"
                fake_cli = self.write_fake_cli(
                    tmp,
                    "#!/usr/bin/env python3\n"
                    "import json, pathlib, sys\n"
                    f"path = pathlib.Path({str(count_file)!r})\n"
                    "count = int(path.read_text() or '0') if path.exists() else 0\n"
                    "path.write_text(str(count + 1))\n"
                    "args = sys.argv[1:]\n"
                    "if 'doctor' in args:\n"
                    "    print('doctor ok')\n"
                    "elif 'clock' in args:\n"
                    "    print(json.dumps({'is_open': False}))\n"
                    "elif 'account' in args:\n"
                    "    print(json.dumps({'status': 'ACTIVE'}))\n",
                )
                old_path, old_live = self.with_cli_path(fake_cli)
                try:
                    alpaca_health_check(force_refresh=True)
                    second = alpaca_health_check(force_refresh=True)
                finally:
                    self.restore_cli_env(old_path, old_live)
                calls = int(count_file.read_text())
            self.assertFalse(second["cached"])
            self.assertEqual(calls, 6)

        def test_alpaca_health_account_not_active_warning(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                fake_cli = self.write_fake_cli(
                    tmp,
                    "#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "args = sys.argv[1:]\n"
                    "if 'doctor' in args:\n"
                    "    print('doctor ok')\n"
                    "elif 'clock' in args:\n"
                    "    print(json.dumps({'is_open': False}))\n"
                    "elif 'account' in args:\n"
                    "    print(json.dumps({'status': 'ACCOUNT_UPDATED'}))\n",
                )
                old_path, old_live = self.with_cli_path(fake_cli)
                try:
                    health = alpaca_health_check(force_refresh=True)
                finally:
                    self.restore_cli_env(old_path, old_live)
            self.assertTrue(health["ok"])
            self.assertEqual(health["summary"]["account"], "ACCOUNT_UPDATED")
            self.assertIn("Alpaca account status is ACCOUNT_UPDATED.", health["warnings"])

        def test_alpaca_cli_runner_strips_scanner_credentials(self) -> None:
            old_key = os.environ.get("ALPACA_API_KEY")
            old_secret = os.environ.get("ALPACA_SECRET_KEY")
            os.environ["ALPACA_API_KEY"] = "scanner-key"
            os.environ["ALPACA_SECRET_KEY"] = "scanner-secret"
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    fake_cli = Path(tmp) / "alpaca"
                    fake_cli.write_text(
                        "#!/bin/sh\n"
                        "if [ -n \"$ALPACA_API_KEY\" ] || [ -n \"$ALPACA_SECRET_KEY\" ]; then\n"
                        "  echo leaked\n"
                        "  exit 3\n"
                        "fi\n"
                        "echo '{}'\n",
                        encoding="utf-8",
                    )
                    fake_cli.chmod(0o755)
                    result = run_alpaca_cli(str(fake_cli), "clock", ["clock"], timeout=8)
            finally:
                if old_key is None:
                    os.environ.pop("ALPACA_API_KEY", None)
                else:
                    os.environ["ALPACA_API_KEY"] = old_key
                if old_secret is None:
                    os.environ.pop("ALPACA_SECRET_KEY", None)
                else:
                    os.environ["ALPACA_SECRET_KEY"] = old_secret
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["raw_output"], "{}")

        def test_ai_review_reports_missing_key_cleanly(self) -> None:
            old_key = os.environ.get("OPENAI_API_KEY")
            old_enabled = os.environ.get("ENABLE_AI_REVIEW")
            try:
                os.environ["ENABLE_AI_REVIEW"] = "true"
                os.environ.pop("OPENAI_API_KEY", None)
                result = ai_review(force_refresh=True)
            finally:
                if old_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_key
                if old_enabled is None:
                    os.environ.pop("ENABLE_AI_REVIEW", None)
                else:
                    os.environ["ENABLE_AI_REVIEW"] = old_enabled
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_type"], "missing_key")
            self.assertEqual(result["error"], AI_REVIEW_ERROR_MISSING_KEY)

        def test_ai_review_success_uses_structured_schema(self) -> None:
            old_key = os.environ.get("OPENAI_API_KEY")
            old_model = os.environ.get("OPENAI_MODEL")
            old_enabled = os.environ.get("ENABLE_AI_REVIEW")
            original_call = globals()["call_openai_analysis_api"]
            calls: List[Dict[str, Any]] = []

            def fake_call(api_key: str, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
                calls.append({"api_key": api_key, "model": model, "payload": payload})
                return {
                    "response_id": "resp_test",
                    "model": model,
                    "status": "completed",
                    "parsed": self.valid_ai_review(confidence=140),
                    "raw_output": "{}",
                }

            try:
                os.environ["OPENAI_API_KEY"] = "test-key"
                os.environ["OPENAI_MODEL"] = "test-model"
                os.environ["ENABLE_AI_REVIEW"] = "true"
                globals()["call_openai_analysis_api"] = fake_call
                result = ai_review(force_refresh=True)
            finally:
                globals()["call_openai_analysis_api"] = original_call
                if old_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_key
                if old_model is None:
                    os.environ.pop("OPENAI_MODEL", None)
                else:
                    os.environ["OPENAI_MODEL"] = old_model
                if old_enabled is None:
                    os.environ.pop("ENABLE_AI_REVIEW", None)
                else:
                    os.environ["ENABLE_AI_REVIEW"] = old_enabled

            self.assertTrue(result["ok"])
            self.assertEqual(result["model"], "test-model")
            self.assertEqual(result["ai_review"]["timing"], "Good")
            self.assertEqual(result["ai_review"]["direction_label"], "Correct")
            self.assertEqual(result["ai_review"]["confidence"], 100)
            self.assertEqual(len(calls), 1)
            self.assertIn("watchlist", calls[0]["payload"])

        def test_validate_ai_review_clamps_confidence(self) -> None:
            review = self.valid_ai_review(confidence=140)
            validated = validate_ai_review(review)
            self.assertIsNotNone(validated)
            self.assertEqual(validated["confidence"], 100)

        def test_ai_review_disabled_does_not_call_openai(self) -> None:
            old_key = os.environ.get("OPENAI_API_KEY")
            old_enabled = os.environ.get("ENABLE_AI_REVIEW")
            original_call = globals()["call_openai_analysis_api"]
            called = {"value": False}

            def fake_call(api_key: str, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
                called["value"] = True
                return {"parsed": self.valid_ai_review(), "raw_output": "{}"}

            try:
                os.environ["OPENAI_API_KEY"] = "test-key"
                os.environ["ENABLE_AI_REVIEW"] = "false"
                globals()["call_openai_analysis_api"] = fake_call
                result = ai_review(force_refresh=True)
            finally:
                globals()["call_openai_analysis_api"] = original_call
                if old_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_key
                if old_enabled is None:
                    os.environ.pop("ENABLE_AI_REVIEW", None)
                else:
                    os.environ["ENABLE_AI_REVIEW"] = old_enabled
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], AI_REVIEW_ERROR_DISABLED)
            self.assertFalse(called["value"])

        def test_ai_review_timeout_message_is_clean(self) -> None:
            old_key = os.environ.get("OPENAI_API_KEY")
            old_enabled = os.environ.get("ENABLE_AI_REVIEW")
            original_call = globals()["call_openai_analysis_api"]

            def fake_call(api_key: str, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
                raise requests.Timeout()

            try:
                os.environ["OPENAI_API_KEY"] = "test-key"
                os.environ["ENABLE_AI_REVIEW"] = "true"
                globals()["call_openai_analysis_api"] = fake_call
                result = ai_review(force_refresh=True)
            finally:
                globals()["call_openai_analysis_api"] = original_call
                if old_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_key
                if old_enabled is None:
                    os.environ.pop("ENABLE_AI_REVIEW", None)
                else:
                    os.environ["ENABLE_AI_REVIEW"] = old_enabled
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], AI_REVIEW_ERROR_TIMEOUT)

        def test_ai_review_invalid_json_fallback(self) -> None:
            old_key = os.environ.get("OPENAI_API_KEY")
            old_enabled = os.environ.get("ENABLE_AI_REVIEW")
            original_call = globals()["call_openai_analysis_api"]

            def fake_call(api_key: str, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
                return {"parsed": None, "raw_output": "not-json"}

            try:
                os.environ["OPENAI_API_KEY"] = "test-key"
                os.environ["ENABLE_AI_REVIEW"] = "true"
                globals()["call_openai_analysis_api"] = fake_call
                result = ai_review(force_refresh=True)
            finally:
                globals()["call_openai_analysis_api"] = original_call
                if old_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_key
                if old_enabled is None:
                    os.environ.pop("ENABLE_AI_REVIEW", None)
                else:
                    os.environ["ENABLE_AI_REVIEW"] = old_enabled
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], AI_REVIEW_ERROR_INVALID_JSON)
            self.assertNotIn("not-json", json.dumps(result))

        def test_ai_review_cache_and_force_refresh(self) -> None:
            old_key = os.environ.get("OPENAI_API_KEY")
            old_enabled = os.environ.get("ENABLE_AI_REVIEW")
            original_call = globals()["call_openai_analysis_api"]
            call_count = {"count": 0}

            def fake_call(api_key: str, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
                call_count["count"] += 1
                return {
                    "response_id": f"resp_{call_count['count']}",
                    "model": model,
                    "status": "completed",
                    "parsed": self.valid_ai_review(),
                    "raw_output": "{}",
                }

            try:
                os.environ["OPENAI_API_KEY"] = "test-key"
                os.environ["ENABLE_AI_REVIEW"] = "true"
                globals()["call_openai_analysis_api"] = fake_call
                first = ai_review(force_refresh=True)
                second = ai_review()
                third = ai_review(force_refresh=True)
            finally:
                globals()["call_openai_analysis_api"] = original_call
                if old_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_key
                if old_enabled is None:
                    os.environ.pop("ENABLE_AI_REVIEW", None)
                else:
                    os.environ["ENABLE_AI_REVIEW"] = old_enabled

            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertFalse(third["cached"])
            self.assertEqual(call_count["count"], 2)

        def test_ai_review_payload_respects_env_limits(self) -> None:
            old_alerts = os.environ.get("AI_REVIEW_MAX_ALERTS")
            old_rows = os.environ.get("AI_REVIEW_MAX_WATCHLIST_ROWS")
            try:
                os.environ["AI_REVIEW_MAX_ALERTS"] = "1"
                os.environ["AI_REVIEW_MAX_WATCHLIST_ROWS"] = "2"
                payload = build_ai_review_payload({
                    "watchlist": [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}],
                    "recent_alerts": [{"symbol": "A"}, {"symbol": "B"}],
                    "scanner_status": {"running": True},
                })
            finally:
                if old_alerts is None:
                    os.environ.pop("AI_REVIEW_MAX_ALERTS", None)
                else:
                    os.environ["AI_REVIEW_MAX_ALERTS"] = old_alerts
                if old_rows is None:
                    os.environ.pop("AI_REVIEW_MAX_WATCHLIST_ROWS", None)
                else:
                    os.environ["AI_REVIEW_MAX_WATCHLIST_ROWS"] = old_rows
            self.assertEqual(len(payload["watchlist"]), 2)
            self.assertEqual(len(payload["recent_alerts"]), 1)

        def test_ai_review_does_not_mutate_dashboard_state(self) -> None:
            old_key = os.environ.get("OPENAI_API_KEY")
            old_enabled = os.environ.get("ENABLE_AI_REVIEW")
            original_call = globals()["call_openai_analysis_api"]
            with STATE.lock:
                before = STATE.snapshot()

            def fake_call(api_key: str, model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
                return {"parsed": self.valid_ai_review(), "raw_output": "{}"}

            try:
                os.environ["OPENAI_API_KEY"] = "test-key"
                os.environ["ENABLE_AI_REVIEW"] = "true"
                globals()["call_openai_analysis_api"] = fake_call
                result = ai_review({"scanner_status": {"running": True}}, force_refresh=True)
                with STATE.lock:
                    after = STATE.snapshot()
            finally:
                globals()["call_openai_analysis_api"] = original_call
                if old_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = old_key
                if old_enabled is None:
                    os.environ.pop("ENABLE_AI_REVIEW", None)
                else:
                    os.environ["ENABLE_AI_REVIEW"] = old_enabled
            self.assertTrue(result["ok"])
            self.assertEqual(before["running"], after["running"])
            self.assertEqual(before["scan_count"], after["scan_count"])
            self.assertEqual(before["last_alert_count"], after["last_alert_count"])

        def test_dashboard_contains_structured_ai_review_card(self) -> None:
            self.assertIn(AI_REVIEW_DISCLAIMER, INDEX_HTML)
            for label in (
                "Timing",
                "Direction Label",
                "Missed Setup",
                "Rule Strictness",
                "Risk Level",
                "Confidence",
                "Suggested Tuning",
                "Plain-English Summary",
                "What To Watch Next",
                "Do Not Chase Warning",
            ):
                self.assertIn(label, INDEX_HTML)

        def test_clear_resets_dashboard_and_output_files(self) -> None:
            old_config_path = STATE.config_path
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                config = scanner_app.load_config(None)
                config["outputs"]["csv_log"] = str(temp_path / "alerts.csv")
                config["outputs"]["jsonl_log"] = str(temp_path / "alerts.jsonl")
                config["outputs"]["state_file"] = str(temp_path / "scanner_state.json")
                config_path = temp_path / "config.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                Path(config["outputs"]["csv_log"]).write_text("old,data\n", encoding="utf-8")
                Path(config["outputs"]["jsonl_log"]).write_text('{"symbol":"OLD"}\n', encoding="utf-8")
                Path(config["outputs"]["state_file"]).write_text(
                    json.dumps({"last_alert_times": {"OLD:test": "2026-05-23T12:00:00+00:00"}}),
                    encoding="utf-8",
                )

                with STATE.lock:
                    STATE.config_path = config_path
                    STATE.running = True
                    STATE.started_at = iso_now()
                    STATE.last_scan_at = iso_now()
                    STATE.last_alert_count = 3
                    STATE.scan_count = 2
                    STATE.last_symbol_count = 1
                    STATE.last_discovery_count = 4
                    STATE.symbol_rows = [{"symbol": "OLD", "score": 80}]

                try:
                    snapshot = clear_dashboard_data()
                    self.assertFalse(snapshot["running"])
                    self.assertEqual(snapshot["scan_count"], 0)
                    self.assertEqual(snapshot["last_alert_count"], 0)
                    self.assertEqual(load_symbol_rows(), [])
                    self.assertFalse(Path(config["outputs"]["jsonl_log"]).exists())
                    csv_lines = Path(config["outputs"]["csv_log"]).read_text(encoding="utf-8").splitlines()
                    self.assertEqual(csv_lines[0].split(",")[0], "timestamp")
                    state = json.loads(Path(config["outputs"]["state_file"]).read_text(encoding="utf-8"))
                    self.assertEqual(state, {"last_alert_times": {}})
                finally:
                    with STATE.lock:
                        STATE.config_path = old_config_path
                        STATE.stop_event.clear()

    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(DashboardTests)
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        raise SystemExit(run_tests())
    raise SystemExit(main())
